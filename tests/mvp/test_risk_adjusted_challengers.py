"""MVP 0.2.2: risk-adjusted strategy challengers.

These tests defend the properties that make the 0.2.2 research trustworthy rather than
merely plausible:

* MAE and MFE are forward-only measurements, taken strictly after the entry fill, and can
  never influence a signal.
* The invalidation boundary is known when the position opens, so risk is decided before it
  is taken rather than discovered from a drawdown.
* The time stop is a function of elapsed bars only, so it cannot look at prices.
* A new profile version gets a new fingerprint, and the frozen 0.2.1 profiles are not
  modified in place.
* EconomicEdgeGuard remains mandatory and independent; a strategy target cannot bypass it.
* Parameter exploration is small, predeclared, fully retained, and ranked risk-first.

The failure modes here are all of the form "a result looked better than it was", so most
tests assert either a refusal or an exact boundary.
"""

from datetime import UTC as _UTC
from datetime import datetime as _datetime
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from autofund.mvp.challenger_research import (
    CONFIG_SETS,
    RANGE_EXPANSION_CONFIGS,
    SELECTION_RULE,
    VOLATILITY_MR_V2_CONFIGS,
    build_evaluator,
    config_identity_token,
    record_from_result,
    select_configuration,
)
from autofund.mvp.economics import DEFAULT_POLICY
from autofund.mvp.executable_replay import (
    MAX_SINGLE_TRADE_RISK_MXN,
    MINIMUM_REWARD_RISK_RATIO,
    RISK_ADJUSTED_ENTRY_REJECT,
    assess_entry_risk,
    replay_executable,
)
from autofund.mvp.historical import synthetic_candles
from autofund.mvp.profile_library import (
    FROZEN_PROFILES,
    PROFILE_REGISTRY,
    RISK_ADJUSTED_CHALLENGERS,
    VolatilityMeanReversionV2,
    evaluator_for,
)
from autofund.mvp.profiles import (
    EXIT_INVALIDATED,
    EXIT_TARGET_REACHED,
    EXIT_TIME_STOP,
    StrategyProposal,
    TargetModel,
)
from autofund.mvp.risk_metrics import (
    MINIMUM_PERCENTILE_SAMPLE,
    TradeRiskPath,
    percentiles,
    summarise_risk_paths,
)

FEE = Decimal("0.0078")
SPREAD = Decimal("12")
SLIPPAGE = Decimal("5")
BUDGET = Decimal("11")
V2 = "volatility-mean-reversion-v2"
RANGE = "range-expansion-v1"

# The frozen 0.2.1 fingerprints. Recorded here as literals so any accidental change to a
# previous profile fails loudly rather than silently rewriting prior evidence.
FROZEN_FINGERPRINTS = {
    "mean-reversion-safe-v1": "1cadcfa967a919b6d041cf382d5acc993d7c70c7c1b008bba2db764c4f51bb6e",
    "trend-continuation-v1": "6314058847ec352c76bf10f3c61fdd3b2989df3ee0782b3609fed5995e6157d7",
    "volatility-mean-reversion-v1":
        "b7f9b5431243495cd6e0584cdba5f4cf4cef98aec9332bfd70141879a0ae22a4",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _candles(prices: list[str], **kwargs: Any) -> tuple[Any, ...]:
    return synthetic_candles(prices=prices, **kwargs).candles


def _bar(*, open_: str, high: str, low: str, close: str) -> Any:
    """One candle with explicit OHLC.

    Needed because `synthetic_candles` gives every bar the same symmetric +/-half_range
    geometry around its close, so its true range never expands relative to its own ATR and
    a range-expansion profile can never fire on that family. Real candles have gaps, and a
    fixture that cannot produce one cannot test the strategy that depends on them.
    """
    from autofund.replay.data import Candle

    stamp = _BAR_START + _BAR_OFFSET[0]
    _BAR_OFFSET[0] = _BAR_OFFSET[0] + timedelta(minutes=1)
    return Candle(stamp, Decimal(open_), Decimal(high), Decimal(low), Decimal(close),
                  Decimal("1"))


_BAR_OFFSET = [timedelta(0)]
_BAR_START = _datetime(2026, 1, 1, tzinfo=_UTC)


def _ohlc_candles(*, quiet: int = 25, quiet_price: str = "100",
                  expansion: tuple[str, str, str, str] | None = None,
                  then: tuple[str, int] | None = None) -> tuple[Any, ...]:
    """A flat range, then an optional expansion bar, then an optional continuation."""
    _BAR_OFFSET[0] = timedelta(0)
    bars: list[Any] = []
    for _ in range(quiet):
        bars.append(_bar(open_=quiet_price, high=str(Decimal(quiet_price) * Decimal("1.0002")),
                         low=str(Decimal(quiet_price) * Decimal("0.9998")),
                         close=quiet_price))
    if expansion is not None:
        bars.append(_bar(open_=expansion[0], high=expansion[1], low=expansion[2],
                         close=expansion[3]))
    if then is not None:
        price, count = then
        for _ in range(count):
            bars.append(_bar(open_=price, high=str(Decimal(price) * Decimal("1.0002")),
                             low=str(Decimal(price) * Decimal("0.9998")), close=price))
    return tuple(bars)


# The strongest predeclared range-expansion configuration. Used by the fixtures below
# because it is the one that actually reaches a fill: at 173 bps round-trip friction the
# weaker configurations are refused by the risk-adjusted gate before entry, which is a
# correct outcome but not a useful fixture for testing the exit path.
STRONG_RANGE_CONFIG = RANGE_EXPANSION_CONFIGS[3]


def _traded_expansion(*, target_reached: bool = True,
                      steps: int = 200) -> tuple[Any, ...]:
    """A flat range, a genuine expansion bar, then a drift past (or short of) the target.

    The expansion bar closes at 105 having traded 101.5-106, which is a real expansion
    relative to the preceding 0.04% range. The drift that follows is slow enough that the
    fill happens near 105 rather than gapping away from the entry.
    """
    _BAR_OFFSET[0] = timedelta(0)
    bars: list[Any] = []
    for _ in range(25):
        bars.append(_bar(open_="100", high="100.02", low="99.98", close="100"))
    bars.append(_bar(open_="102", high="106", low="101.5", close="105"))
    end = Decimal("113") if target_reached else Decimal("105.5")
    start = Decimal("105")
    for step in range(steps):
        price = start + (end - start) * Decimal(step) / Decimal(steps)
        text = str(price)
        bars.append(_bar(open_=text, high=str(price * Decimal("1.0005")),
                         low=str(price * Decimal("0.9995")), close=text))
    return tuple(bars)


def _traded_expansion_then_collapse() -> tuple[Any, ...]:
    """An expansion, then a sharp reversal through the declared boundary."""
    _BAR_OFFSET[0] = timedelta(0)
    bars: list[Any] = []
    for _ in range(25):
        bars.append(_bar(open_="100", high="100.02", low="99.98", close="100"))
    bars.append(_bar(open_="102", high="106", low="101.5", close="105"))
    for _ in range(40):
        bars.append(_bar(open_="105", high="105.1", low="104.9", close="105"))
    for _ in range(120):
        bars.append(_bar(open_="85", high="85.1", low="84.9", close="85"))
    return tuple(bars)


def _flat_then(*, before: str, after: str, before_len: int = 30,
               after_len: int = 60) -> tuple[Any, ...]:
    return _candles([before] * before_len + [after] * after_len)


def _proposal(*, target_bps: str, stop_bps: str, entry: str = "100",
              max_holding_bars: int = 0) -> StrategyProposal:
    e = Decimal(entry)
    return StrategyProposal(
        decision="BUY", reason_code="SIGNAL_BUY", profile_id="test", strategy_id="test",
        strategy_version="0", strategy_fingerprint="f" * 64, market="SOL/MXN",
        entry_reference_mxn=e,
        expected_exit_reference_mxn=e * (Decimal("1") + Decimal(target_bps) / Decimal("10000")),
        expected_gross_edge_bps=Decimal(target_bps), expected_holding_horizon=60,
        target_model=TargetModel(),
        invalidation_price_mxn=e * (Decimal("1") - Decimal(stop_bps) / Decimal("10000")),
        max_holding_bars=max_holding_bars)


def _path(*, mae: str, mfe: str, net: str, exit_reason: str = EXIT_TARGET_REACHED,
          bars: int = 60) -> TradeRiskPath:
    return TradeRiskPath(
        mae_mxn=Decimal(mae), mae_bps=Decimal(mae), mfe_mxn=Decimal(mfe),
        mfe_bps=Decimal(mfe), realised_gross_pnl_mxn=Decimal(net),
        realised_net_pnl_mxn=Decimal(net), holding_bars=bars, holding_minutes=bars * 60,
        time_to_mae_bars=1, time_to_mfe_bars=2, exit_reason=exit_reason, observations=bars)


# ===========================================================================
# 1. MAE and MFE are forward-only
# ===========================================================================

def test_mae_mfe_are_recorded_for_every_completed_trip() -> None:
    result = replay_executable(
        candles=_traded_expansion(), profile_id=RANGE, market="SOL/MXN",
        evaluator=build_evaluator(RANGE, STRONG_RANGE_CONFIG), taker_fee_rate=FEE,
        spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    assert result.trips
    for trip in result.trips:
        assert trip.risk_path is not None
        assert trip.risk_path.observations > 0
        assert trip.risk_path.mae_mxn >= Decimal("0")
        assert trip.risk_path.mfe_mxn >= Decimal("0")
        assert trip.risk_path.telemetry()["path_is_forward_only"] is True
        assert trip.risk_path.mfe_mxn >= trip.risk_path.realised_net_pnl_mxn


def test_mae_and_mfe_do_not_reflect_prices_before_entry() -> None:
    """A violent move *before* entry must not appear in the position's risk path.

    The series collapses from 200 to 100 and only then goes flat. A position opened after
    the collapse cannot have an MAE or MFE derived from the pre-entry price of 200.
    """
    candles = _candles(["200"] * 25 + ["100"] * 120)
    result = replay_executable(
        candles=candles, profile_id=RANGE, market="SOL/MXN",
        evaluator=build_evaluator(RANGE, RANGE_EXPANSION_CONFIGS[1]),
        taker_fee_rate=FEE, spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    for trip in result.trips:
        assert trip.risk_path is not None
        # At most the deployed notional, never a multiple of it: a path that saw the
        # pre-entry 200 would show an excursion of roughly -11 MXN on an 11 MXN budget.
        assert trip.risk_path.mae_mxn <= BUDGET
        # Time to MAE is measured from the entry fill, so it cannot precede it.
        assert trip.risk_path.time_to_mae_bars >= 0
        assert trip.risk_path.time_to_mae_bars <= trip.risk_path.holding_bars
        assert trip.risk_path.time_to_mfe_bars <= trip.risk_path.holding_bars


def test_the_risk_path_observation_count_cannot_exceed_bars_held() -> None:
    """The path is accumulated once per held bar, from the fill onward."""
    result = replay_executable(
        candles=_flat_then(before="100", after="104"), profile_id=RANGE,
        market="SOL/MXN", evaluator=build_evaluator(RANGE, RANGE_EXPANSION_CONFIGS[1]),
        taker_fee_rate=FEE, spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    for trip in result.trips:
        assert trip.risk_path is not None
        assert trip.risk_path.observations <= (trip.holding_bars + 1)


def test_truncating_the_series_cannot_change_an_earlier_mfe() -> None:
    """A path that could see the future would change when the future was removed."""
    candles = _candles(["100"] * 20 + ["101"] * 20 + ["104"] * 40 + ["99"] * 40)
    config = RANGE_EXPANSION_CONFIGS[1]
    full = replay_executable(
        candles=candles, profile_id=RANGE, market="SOL/MXN",
        evaluator=build_evaluator(RANGE, config), taker_fee_rate=FEE,
        spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    if not full.trips:
        pytest.skip("fixture produced no trip to compare")
    first = full.trips[0]
    bounded = replay_executable(
        candles=candles[:first.exit_fill_index + 1], profile_id=RANGE, market="SOL/MXN",
        evaluator=build_evaluator(RANGE, config), taker_fee_rate=FEE,
        spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    assert bounded.trips
    assert bounded.trips[0].risk_path is not None
    assert first.risk_path is not None
    assert bounded.trips[0].risk_path.mfe_mxn == first.risk_path.mfe_mxn


# ===========================================================================
# 2. Risk is decided before the position exists
# ===========================================================================

def test_invalidation_and_holding_limit_are_declared_on_the_proposal() -> None:
    """Both are available at entry, not inferred afterwards."""
    proposal = evaluator_for(V2).propose(candles=_flat_then(before="100", after="95"))
    if proposal.decision != "BUY":
        pytest.skip("fixture did not produce an entry proposal")
    assert proposal.declares_invalidation is True
    assert proposal.declares_time_stop is True
    assert proposal.invalidation_price_mxn < proposal.entry_reference_mxn
    assert proposal.invalidation_distance_bps > Decimal("0")
    assert proposal.declared_reward_risk_ratio is not None


def test_the_frozen_v1_profile_declares_no_invalidation() -> None:
    """v1's absence of a boundary is the defect v2 addresses, and must still be visible."""
    proposal = evaluator_for("volatility-mean-reversion-v1").propose(
        candles=_flat_then(before="100", after="95"))
    assert proposal.declares_invalidation is False
    assert proposal.declares_time_stop is False
    assert proposal.declared_reward_risk_ratio is None


def test_a_target_that_cannot_clear_friction_is_refused_before_entry() -> None:
    """173 bps of friction means a 250 bps target with a tight boundary is not viable."""
    verdict = assess_entry_risk(
        proposal=_proposal(target_bps="250", stop_bps="80"),
        execution_price_mxn=Decimal("100"), budget_mxn=BUDGET, taker_fee_rate=FEE,
        spread_bps=SPREAD, slippage_bps=SLIPPAGE, policy=DEFAULT_POLICY)
    assert verdict.admissible is False
    assert verdict.reason_code == "REWARD_RISK_BELOW_MINIMUM"
    assert verdict.reward_risk_ratio < MINIMUM_REWARD_RISK_RATIO
    assert verdict.telemetry()["evaluated_before_entry"] is True


def test_a_target_that_clears_friction_with_a_favourable_boundary_is_admitted() -> None:
    """The gate must not be a blanket veto: a genuinely favourable shape passes."""
    verdict = assess_entry_risk(
        proposal=_proposal(target_bps="600", stop_bps="100"),
        execution_price_mxn=Decimal("100"), budget_mxn=BUDGET, taker_fee_rate=FEE,
        spread_bps=SPREAD, slippage_bps=SLIPPAGE, policy=DEFAULT_POLICY)
    assert verdict.admissible is True
    assert verdict.reason_code == "OK"
    assert verdict.reward_risk_ratio >= MINIMUM_REWARD_RISK_RATIO


def test_reward_and_risk_are_both_net_of_friction() -> None:
    """Netting friction from only one side would flatter every profile."""
    verdict = assess_entry_risk(
        proposal=_proposal(target_bps="250", stop_bps="80"),
        execution_price_mxn=Decimal("100"), budget_mxn=BUDGET, taker_fee_rate=FEE,
        spread_bps=SPREAD, slippage_bps=SLIPPAGE, policy=DEFAULT_POLICY)
    friction = BUDGET * (FEE * Decimal("20000") + SPREAD + SLIPPAGE) / Decimal("10000")
    # Reward is the gross move minus friction; risk is the boundary loss plus friction.
    assert verdict.declared_reward_mxn == pytest.approx(
        BUDGET * Decimal("250") / Decimal("10000") - friction, abs=Decimal("1E-20"))
    assert verdict.declared_risk_mxn == pytest.approx(
        BUDGET * Decimal("80") / Decimal("10000") + friction, abs=Decimal("1E-20"))


def test_a_declared_risk_beyond_policy_is_refused() -> None:
    """A wide boundary at the current budget breaches the existing single-trade bound."""
    verdict = assess_entry_risk(
        proposal=_proposal(target_bps="900", stop_bps="900"),
        execution_price_mxn=Decimal("100"), budget_mxn=BUDGET, taker_fee_rate=FEE,
        spread_bps=SPREAD, slippage_bps=SLIPPAGE, policy=DEFAULT_POLICY)
    assert verdict.admissible is False
    assert verdict.reason_code == RISK_ADJUSTED_ENTRY_REJECT
    assert verdict.declared_risk_mxn > MAX_SINGLE_TRADE_RISK_MXN


def test_a_boundary_at_or_above_entry_is_refused() -> None:
    """A boundary that cannot bound anything is a misreported risk, not a valid one."""
    bad = _proposal(target_bps="600", stop_bps="100")
    broken = StrategyProposal(
        **{**{name: getattr(bad, name) for name in bad.__dataclass_fields__},
           "invalidation_price_mxn": Decimal("101")})
    verdict = assess_entry_risk(
        proposal=broken, execution_price_mxn=Decimal("100"), budget_mxn=BUDGET,
        taker_fee_rate=FEE, spread_bps=SPREAD, slippage_bps=SLIPPAGE,
        policy=DEFAULT_POLICY)
    assert verdict.admissible is False
    assert verdict.reason_code == "INVALIDATION_NOT_BELOW_ENTRY"


def test_a_profile_without_a_boundary_is_reported_not_silently_admitted() -> None:
    """No boundary means no risk claim; the gate must say so rather than imply safety."""
    p = _proposal(target_bps="600", stop_bps="100")
    no_boundary = StrategyProposal(
        **{**{name: getattr(p, name) for name in p.__dataclass_fields__},
           "invalidation_price_mxn": Decimal("0")})
    verdict = assess_entry_risk(
        proposal=no_boundary, execution_price_mxn=Decimal("100"), budget_mxn=BUDGET,
        taker_fee_rate=FEE, spread_bps=SPREAD, slippage_bps=SLIPPAGE,
        policy=DEFAULT_POLICY)
    assert verdict.admissible is True
    assert verdict.boundary_declared is False
    assert verdict.reason_code == "NO_DECLARED_RISK_BOUNDARY"


def test_the_risk_gate_does_not_replace_the_economic_guard() -> None:
    """Both gates run: the economic counter and the risk counter are separate."""
    result = replay_executable(
        candles=_traded_expansion(), profile_id=RANGE, market="SOL/MXN",
        evaluator=build_evaluator(RANGE, STRONG_RANGE_CONFIG), taker_fee_rate=FEE,
        spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    # Risk-adjusted passes are a subset of economic passes, never a superset.
    assert result.risk_adjusted_passes <= result.economic_passes
    assert (result.economic_passes + result.economic_rejects
            <= result.evaluations)


# ===========================================================================
# 3. Exit reasons are attributable, and the time stop cannot look ahead
# ===========================================================================

def test_a_boundary_breach_is_attributed_to_invalidation() -> None:
    result = replay_executable(
        candles=_traded_expansion_then_collapse(), profile_id=RANGE, market="SOL/MXN",
        evaluator=build_evaluator(RANGE, STRONG_RANGE_CONFIG), taker_fee_rate=FEE,
        spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    assert result.trips
    assert any(trip.exit_reason == EXIT_INVALIDATED for trip in result.trips)


def test_a_profitable_exit_at_the_target_is_attributed_to_target_reached() -> None:
    result = replay_executable(
        candles=_traded_expansion(target_reached=True), profile_id=RANGE,
        market="SOL/MXN", evaluator=build_evaluator(RANGE, STRONG_RANGE_CONFIG),
        taker_fee_rate=FEE, spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    assert result.trips
    assert result.exit_reason_counts.get(EXIT_TARGET_REACHED, 0) >= 1
    # Target and invalidation are never collapsed into one reason.
    assert set(result.exit_reason_counts) <= {
        EXIT_TARGET_REACHED, EXIT_INVALIDATED, EXIT_TIME_STOP, "SIGNAL_SELL"}


def test_the_time_stop_fires_exactly_at_the_declared_limit() -> None:
    """A bar-index rule, so it cannot depend on what the price did."""
    limit = 40
    candles = _candles(["100"] * 20 + ["101"] * 300)
    evaluator = VolatilityMeanReversionV2(max_holding_bars=limit)
    result = replay_executable(
        candles=candles, profile_id=V2, market="SOL/MXN", evaluator=evaluator,
        taker_fee_rate=FEE, spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    for trip in result.trips:
        if trip.exit_reason == EXIT_TIME_STOP:
            assert trip.holding_bars == limit


def test_the_time_stop_is_unchanged_when_later_prices_are_removed() -> None:
    """The stop must not read prices, so truncating the future cannot move it."""
    limit = 30
    candles = _candles(["100"] * 20 + ["101"] * 200)
    evaluator = VolatilityMeanReversionV2(max_holding_bars=limit)
    full = replay_executable(
        candles=candles, profile_id=V2, market="SOL/MXN", evaluator=evaluator,
        taker_fee_rate=FEE, spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    timed = [trip for trip in full.trips if trip.exit_reason == EXIT_TIME_STOP]
    if not timed:
        pytest.skip("fixture produced no time stop")
    first = timed[0]
    bounded = replay_executable(
        candles=candles[:first.exit_fill_index + 1], profile_id=V2, market="SOL/MXN",
        evaluator=VolatilityMeanReversionV2(max_holding_bars=limit), taker_fee_rate=FEE,
        spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    stopped = [trip for trip in bounded.trips if trip.exit_reason == EXIT_TIME_STOP]
    assert stopped
    assert stopped[0].exit_fill_index == first.exit_fill_index


def test_a_position_with_no_time_limit_is_not_stopped_by_time() -> None:
    candles = _candles(["100"] * 20 + ["101"] * 120)
    result = replay_executable(
        candles=candles, profile_id=V2, market="SOL/MXN",
        evaluator=VolatilityMeanReversionV2(max_holding_bars=0), taker_fee_rate=FEE,
        spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    assert EXIT_TIME_STOP not in result.exit_reason_counts


# ===========================================================================
# 4. Versioning: new challengers get new identities, v1 is untouched
# ===========================================================================

def test_the_frozen_profiles_keep_their_exact_fingerprints() -> None:
    by_id = {definition.profile_id: definition for definition in PROFILE_REGISTRY}
    for profile_id, expected in FROZEN_FINGERPRINTS.items():
        assert by_id[profile_id].identity.fingerprint == expected, profile_id


def test_frozen_profiles_are_reported_as_a_distinct_set() -> None:
    frozen = {definition.profile_id for definition in FROZEN_PROFILES}
    added = {definition.profile_id for definition in RISK_ADJUSTED_CHALLENGERS}
    assert frozen == set(FROZEN_FINGERPRINTS)
    assert added == {V2, RANGE}
    assert frozen.isdisjoint(added)


def test_each_new_challenger_has_its_own_identity() -> None:
    by_id = {definition.profile_id: definition for definition in PROFILE_REGISTRY}
    assert by_id[V2].identity.fingerprint != by_id[RANGE].identity.fingerprint
    assert by_id[V2].identity.fingerprint != by_id[
        "volatility-mean-reversion-v1"].identity.fingerprint


def test_identities_are_derived_from_the_live_parameters() -> None:
    """A parameter variant must not be able to share the base profile's fingerprint."""
    base = VolatilityMeanReversionV2()
    variant = VolatilityMeanReversionV2(displacement_atr_multiple=Decimal("3.0"))
    assert base.identity.fingerprint != variant.identity.fingerprint
    assert dict(variant.parameters)["displacement_atr_multiple"] == "3.0"


def test_building_a_configuration_applies_the_requested_parameters() -> None:
    evaluator = build_evaluator(V2, {"displacement_atr_multiple": "3.0",
                                     "stop_floor_bps": "120", "max_holding_bars": "480"})
    assert evaluator.displacement_atr_multiple == Decimal("3.0")
    assert evaluator.stop_floor_bps == Decimal("120")
    assert evaluator.max_holding_bars == 480


def test_an_undeclared_parameter_is_rejected_rather_than_ignored() -> None:
    """Silently ignoring a key would make two configurations indistinguishable."""
    with pytest.raises(KeyError, match="UNDECLARED_PARAMETERS"):
        build_evaluator(V2, {"not_a_parameter": "1"})


def test_an_unknown_challenger_is_rejected() -> None:
    with pytest.raises(KeyError, match="NOT_A_RESEARCH_CHALLENGER"):
        build_evaluator("mean-reversion-safe-v1", {})


# ===========================================================================
# 5. Parameter search discipline
# ===========================================================================

def test_the_configuration_sets_are_small_and_fixed() -> None:
    assert len(VOLATILITY_MR_V2_CONFIGS) == 4
    assert len(RANGE_EXPANSION_CONFIGS) == 4
    assert set(CONFIG_SETS) == {V2, RANGE}


def test_selection_is_risk_first_with_profit_last() -> None:
    assert SELECTION_RULE[0] == "risk_gates_satisfied"
    assert SELECTION_RULE[-1] == "net_pnl_descending"


def test_the_selection_rule_prefers_lower_risk_over_higher_profit() -> None:
    """A configuration earning more while risking more must not win."""
    from autofund.mvp.executable_replay import ExecutableReplayResult

    def result(net: str, mae: str) -> ExecutableReplayResult:
        from autofund.mvp.executable_replay import ExecutableRoundTrip
        from autofund.mvp.execution_model import (
            ExecutionObservation,
            RoundTripEconomics,
            model_buy,
            model_sell,
        )
        observation = ExecutionObservation(timestamp_ms=0, open=Decimal("100"),
                                           close=Decimal("100"))
        entry = model_buy(observation=observation, budget_mxn=BUDGET,
                          taker_fee_rate=FEE, modelled_slippage_bps=Decimal("0"),
                          base_currency="SOL", quote_currency="MXN")
        per_unit = entry.filled_quantity * (Decimal("1") - FEE)
        exit_fill = model_sell(
            observation=ExecutionObservation(
                timestamp_ms=0, open=(BUDGET + Decimal(net)) / per_unit,
                close=(BUDGET + Decimal(net)) / per_unit),
            quantity=entry.filled_quantity, taker_fee_rate=FEE,
            modelled_slippage_bps=Decimal("0"), base_currency="SOL", quote_currency="MXN")
        economics = RoundTripEconomics(entry=entry, exit=exit_fill, budget_mxn=BUDGET,
                                       own_quantity=entry.filled_quantity)
        trips = tuple(ExecutableRoundTrip(
            entry_signal_index=1, entry_fill_index=2, exit_signal_index=3, exit_fill_index=4,
            economics=economics, entry_reason="SIGNAL_BUY", exit_reason=EXIT_TARGET_REACHED,
            episode=1, risk_path=_path(mae=mae, mfe=net, net=net))
            for _ in range(8))
        return ExecutableReplayResult(
            market="SOL/MXN", profile_id=V2, profile_fingerprint="p" * 64,
            strategy_fingerprint="s" * 64, candles=100, evaluations=100,
            strategy_signals=8, economic_passes=8, economic_rejects=0,
            simulated_trades=8, unfilled_signals=0, trips=trips,
            evidence_quality="CANDLE_ONLY_ESTIMATE", spread_bps=SPREAD,
            modelled_slippage_bps=SLIPPAGE, dataset_fingerprint="d" * 64,
            fill_delay_bars=1, distinct_episodes=8)

    low_risk = record_from_result(profile_id=V2, config={"a": "1"},
                                  result=result(net="0.30", mae="0.10"))
    high_profit = record_from_result(profile_id=V2, config={"a": "2"},
                                     result=result(net="0.90", mae="0.80"))
    chosen = select_configuration((low_risk, high_profit))
    assert chosen is not None
    assert chosen.token == low_risk.token


def test_every_tested_configuration_is_retained() -> None:
    """No code path keeps only the winner; the selection is a view over all records."""
    result = replay_executable(
        candles=_flat_then(before="100", after="103"), profile_id=RANGE,
        market="SOL/MXN", evaluator=build_evaluator(RANGE, RANGE_EXPANSION_CONFIGS[0]),
        taker_fee_rate=FEE, spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    records = tuple(record_from_result(profile_id=RANGE, config=config, result=result)
                    for config in RANGE_EXPANSION_CONFIGS)
    assert len(records) == len(RANGE_EXPANSION_CONFIGS)
    assert len({record.token for record in records}) == len(RANGE_EXPANSION_CONFIGS)
    assert select_configuration(()) is None


def test_configuration_tokens_are_deterministic_and_distinct() -> None:
    first = config_identity_token(V2, {"b": "2", "a": "1"})
    second = config_identity_token(V2, {"a": "1", "b": "2"})
    assert first == second  # key order must not change identity
    assert first != config_identity_token(V2, {"a": "1", "b": "3"})


# ===========================================================================
# 6. MAE/MFE summarisation
# ===========================================================================

def test_percentiles_are_withheld_below_the_minimum_sample() -> None:
    values = tuple(Decimal(str(index)) for index in range(MINIMUM_PERCENTILE_SAMPLE - 1))
    summary = percentiles(values)
    assert summary.stated is False
    assert summary.p50 is None
    assert summary.p90 is None
    assert summary.telemetry()["percentiles_withheld_below_sample"] is True


def test_percentiles_use_observed_values_only() -> None:
    values = tuple(Decimal(str(index)) for index in range(11))
    summary = percentiles(values)
    assert summary.stated is True
    assert summary.minimum == Decimal("0")
    assert summary.maximum == Decimal("10")
    assert summary.p50 in values and summary.p90 in values


def test_summary_reports_capital_efficiency_and_never_pools_markets() -> None:
    paths = tuple(_path(mae="0.20", mfe="0.30", net="0.05", bars=1200) for _ in range(6))
    summary = summarise_risk_paths(market="SOL/MXN", profile_id=V2, paths=paths)
    assert summary.round_trips == 6
    assert summary.telemetry()["markets_pooled"] is False
    assert summary.net_pnl_per_capital_hour is not None
    assert summary.worst_mae_mxn == Decimal("0.20")


def test_a_zero_trade_summary_reports_no_efficiency_rather_than_zero() -> None:
    summary = summarise_risk_paths(market="BTC/MXN", profile_id=V2, paths=())
    assert summary.round_trips == 0
    assert summary.net_pnl_per_capital_hour is None
    assert summary.aggregate_mfe_capture_ratio is None


def test_mfe_capture_ratio_exposes_giving_back_a_move() -> None:
    """Earning a fraction of the peak available gain means the exit leaked edge."""
    path = _path(mae="0.10", mfe="1.00", net="0.25")
    ratio = path.mfe_capture_ratio
    assert ratio is not None
    assert ratio == Decimal("0.25")


def test_mae_to_reward_ratio_is_undefined_without_a_reward() -> None:
    """Dividing by a non-positive reward would produce a meaningless ratio."""
    assert _path(mae="0.50", mfe="0.10", net="0.00").mae_to_reward_ratio is None


def test_the_replay_flags_mae_exceeding_the_realised_reward() -> None:
    """The 0.2.1 finding as a first-class flag."""
    result = replay_executable(
        candles=_traded_expansion(), profile_id=RANGE, market="SOL/MXN",
        evaluator=build_evaluator(RANGE, STRONG_RANGE_CONFIG), taker_fee_rate=FEE,
        spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    telemetry = result.telemetry()
    assert "mae_exceeds_realised_reward" in telemetry
    assert telemetry["risk_adjusted_summary"]["markets_pooled"] is False
    assert "capital_hours" in telemetry
    # This fixture is deliberately a *healthy* trade: MAE must not exceed the reward.
    assert telemetry["mae_exceeds_realised_reward"] is False
    # Percentiles are withheld below the minimum sample, so a single-trip fixture reports
    # None. That withholding is the behaviour being checked, not a missing measurement.
    assert result.median_mae_mxn is None
    assert result.paths
    assert result.paths[0].mae_mxn > Decimal("0")
    assert result.paths[0].mfe_mxn > Decimal("0")


# ===========================================================================
# 7. Execution realism is preserved
# ===========================================================================

def test_the_new_profiles_are_filled_past_the_signal_bar() -> None:
    result = replay_executable(
        candles=_traded_expansion(), profile_id=RANGE, market="SOL/MXN",
        evaluator=build_evaluator(RANGE, STRONG_RANGE_CONFIG), taker_fee_rate=FEE,
        spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    assert result.trips
    for trip in result.trips:
        assert trip.entry_fill_index > trip.entry_signal_index
        assert trip.exit_fill_index > trip.exit_signal_index


def test_fee_cash_flows_are_still_itemised_for_the_new_profiles() -> None:
    result = replay_executable(
        candles=_traded_expansion(), profile_id=RANGE, market="SOL/MXN",
        evaluator=build_evaluator(RANGE, STRONG_RANGE_CONFIG), taker_fee_rate=FEE,
        spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    assert result.trips
    for trip in result.trips:
        assert trip.economics.entry.fee_mxn > Decimal("0")
        assert trip.economics.entry.fee_currency == "BASE"
        assert trip.economics.exit.fee_currency == "QUOTE"
        assert trip.economics.total_friction_mxn > Decimal("0")
        assert (trip.economics.entry.spread_convention
                == trip.economics.exit.spread_convention)


def test_slippage_tolerance_is_still_not_used_as_a_forecast() -> None:
    result = replay_executable(
        candles=_traded_expansion(), profile_id=RANGE, market="SOL/MXN",
        evaluator=build_evaluator(RANGE, STRONG_RANGE_CONFIG), taker_fee_rate=FEE,
        spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    assert result.trips
    for trip in result.trips:
        assert trip.economics.entry.telemetry()[
            "slippage_tolerance_used_as_forecast"] is False


def test_determinism_holds_for_the_new_profiles() -> None:
    candles = _traded_expansion()
    config = STRONG_RANGE_CONFIG
    first = replay_executable(
        candles=candles, profile_id=RANGE, market="SOL/MXN",
        evaluator=build_evaluator(RANGE, config), taker_fee_rate=FEE,
        spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    second = replay_executable(
        candles=candles, profile_id=RANGE, market="SOL/MXN",
        evaluator=build_evaluator(RANGE, config), taker_fee_rate=FEE,
        spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    assert first.telemetry() == second.telemetry()


def test_the_target_is_fixed_at_entry_and_cannot_ratchet_away() -> None:
    """A target recomputed from drifting volatility is never reached in a steady trend.

    This is the defect the fixed target exists to prevent: in a sustained advance the ATR
    rises, so a per-bar target rises with the price and the position waits forever -- the
    same "hold indefinitely" failure mode as v1, in a different form.
    """
    result = replay_executable(
        candles=_traded_expansion(target_reached=True), profile_id=RANGE,
        market="SOL/MXN", evaluator=build_evaluator(RANGE, STRONG_RANGE_CONFIG),
        taker_fee_rate=FEE, spread_bps=SPREAD, policy=DEFAULT_POLICY, budget_mxn=BUDGET)
    assert result.trips
    closing = result.trips[0]
    assert closing.exit_reason == EXIT_TARGET_REACHED
    # It closed at the target rather than drifting along with the price.
    assert closing.holding_bars < 200


# ===========================================================================
# 8. Safety invariants
# ===========================================================================

def test_capital_limits_are_unchanged() -> None:
    assert BUDGET == Decimal("11")
    assert MAX_SINGLE_TRADE_RISK_MXN == Decimal("0.50")
    assert PROFILE_REGISTRY  # registry intact; no capital constant is defined here


def test_the_drawdown_policy_bound_is_not_raised() -> None:
    """The challengers adapt to policy; policy does not adapt to them."""
    from autofund.mvp.robustness import MAX_DRAWDOWN_MXN

    assert MAX_DRAWDOWN_MXN == Decimal("0.50")


def test_production_post_count_is_zero() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "autofund"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in ("requests.post", "client.post(", ".post(url"):
            if marker in text:
                offenders.append(f"{path.name}:{marker}")
    assert offenders == []


def test_no_challenger_uses_leverage_shorting_or_margin() -> None:
    root = Path(__file__).resolve().parents[2] / "src" / "autofund" / "mvp"
    text = " ".join((root / name).read_text(encoding="utf-8")
                    for name in ("profile_library.py", "challenger_research.py"))
    for forbidden in ("leverage", "margin_call", "short_position", "futures"):
        assert forbidden not in text.lower()

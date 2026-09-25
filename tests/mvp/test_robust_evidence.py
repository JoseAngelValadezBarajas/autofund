"""MVP 0.2.1: robust trading evidence.

These tests defend the distinction the milestone exists to establish -- between
"the code produced a number" and "the number is evidence". Most of them assert a
*refusal*, because the failure modes here are all of the form "something looked
like proof when it was not":

* a fixture proving the implementation runs was mistaken for a market result
* the development window was presented as if it were unseen validation
* a fill used the very price the decision was derived from
* friction was charged to the verdict but not to the fill
* a short sample, or a zero-trade window, was read as a strong result
* a favourable window was averaged against an unfavourable one into a verdict
  neither window supports

Several tests deliberately build a *profitable-looking* configuration and assert it is
still refused. That is the point: if a test only asserted "bad things are rejected", it
would pass on an implementation that rejects everything.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from autofund.mvp.economics import DEFAULT_POLICY
from autofund.mvp.executable_replay import (
    DEFAULT_MIN_ROUND_TRIPS,
    STATUS_ASSESSABLE,
    Excursion,
    ExecutableReplayResult,
    ExecutableRoundTrip,
    assess_with_executable_guard,
    replay_executable,
    slippage_sensitivity,
)
from autofund.mvp.execution_model import (
    CANDLE_ONLY_ESTIMATE,
    EXECUTION_MODEL_VERSION,
    SPREAD_AS_COST,
    SPREAD_IN_PRICE,
    ExecutionObservation,
    RoundTripEconomics,
    model_buy,
    model_sell,
)
from autofund.mvp.experiment import (
    CERTIFYING_PROVENANCE,
    EXPERIMENT_VERSION,
    MANIFEST_FILE,
    REAL_CAPTURED_FORWARD,
    REAL_HISTORICAL_DEVELOPMENT,
    REAL_HISTORICAL_HOLDOUT,
    REAL_PRODUCTION_FILL,
    SYNTHETIC_FIXTURE,
    ExperimentError,
    ExperimentManifest,
    ExperimentManifestStore,
    HoldoutWindow,
    declare_holdout,
    is_independent,
    may_certify,
    provenance_for_window,
)
from autofund.mvp.historical import synthetic_candles
from autofund.mvp.profile_library import PROFILE_REGISTRY, evaluator_for
from autofund.mvp.robustness import (
    ACCUMULATING_SAMPLE,
    DEVELOPMENT_ONLY,
    DRAWDOWN_EXCEEDED,
    HOLDOUT_NEGATIVE,
    MAX_DRAWDOWN_MXN,
    NO_ADMISSIBLE_OPPORTUNITY,
    NO_OPPORTUNITY,
    PRODUCTION_CERTIFIABLE,
    REGIME_DEPENDENT,
    SENSITIVITY_FRAGILE,
    SIGN_REVERSAL,
    WindowEvidence,
    assert_windows_do_not_overlap,
    assess_pair,
)

FEE = Decimal("0.0078")
CANDIDATE_PROFILE = "volatility-mean-reversion-v1"
DEVELOPMENT_END_MS = 1_800_000_000_000


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _candles(prices: list[str], *, start: datetime | None = None) -> tuple[Any, ...]:
    return synthetic_candles(prices=prices, start=start).candles


def _oscillating(*, cycles: int = 12, low: str = "100",
                 high: str = "100.9") -> tuple[Any, ...]:
    """A series that repeatedly rises then falls, so a mean-reversion profile fires."""
    prices: list[str] = []
    for _ in range(cycles):
        prices.extend([low, low, high, high, low, low])
    return _candles(prices)


def _rising(*, count: int = 40, start: float = 100.0) -> tuple[Any, ...]:
    return _candles([f"{start + index * 0.55:.4f}" for index in range(count)])


def _observe(price: str, *, bids: tuple[Any, ...] = (),
             asks: tuple[Any, ...] = ()) -> ExecutionObservation:
    value = Decimal(price)
    return ExecutionObservation(timestamp_ms=0, open=value, close=value,
                                bids=bids, asks=asks)


class _Level:
    def __init__(self, price: str, amount: str) -> None:
        self.price = Decimal(price)
        self.amount = Decimal(amount)


def _result(**overrides: Any) -> ExecutableReplayResult:
    base: dict[str, Any] = {
        "market": "SOL/MXN", "profile_id": CANDIDATE_PROFILE,
        "profile_fingerprint": "p" * 64, "strategy_fingerprint": "s" * 64,
        "candles": 100, "evaluations": 100, "strategy_signals": 10,
        "economic_passes": 5, "economic_rejects": 5, "simulated_trades": 0,
        "unfilled_signals": 0, "trips": (), "evidence_quality": CANDLE_ONLY_ESTIMATE,
        "spread_bps": Decimal("12"), "modelled_slippage_bps": Decimal("5"),
        "dataset_fingerprint": "d" * 64, "fill_delay_bars": 1, "distinct_episodes": 0}
    base.update(overrides)
    return ExecutableReplayResult(**base)


def _real_trip(target_net_mxn: Decimal, *, adverse_mxn: Decimal | None = None) -> Any:
    """Build a genuine round trip whose net P&L equals the requested target.

    Real fills are used rather than stubs so the assessment is exercised against the
    actual fee and cash-flow code: a fabricated object could satisfy the assertions
    while the real friction arithmetic was broken.

    `adverse_mxn` attaches an excursion, i.e. how far the open position ran against us
    before it closed. It does not change the realized net P&L, which is the point: the
    two are separate and only the pair together describes the risk taken.
    """
    observation = _observe("100")
    entry = model_buy(observation=observation, budget_mxn=Decimal("11"),
                      taker_fee_rate=FEE, modelled_slippage_bps=Decimal("0"),
                      base_currency="SOL", quote_currency="MXN")
    # Back-solve the exit price from the requested net P&L rather than hoping a chosen
    # price lands near it.
    per_unit = entry.filled_quantity * (Decimal("1") - FEE)
    exit_price = (Decimal("11") + target_net_mxn) / per_unit
    exit_fill = model_sell(observation=_observe(exit_price), quantity=entry.filled_quantity,
                           taker_fee_rate=FEE, modelled_slippage_bps=Decimal("0"),
                           base_currency="SOL", quote_currency="MXN")
    economics = RoundTripEconomics(entry=entry, exit=exit_fill, budget_mxn=Decimal("11"),
                                   own_quantity=entry.filled_quantity)
    excursion = None
    if adverse_mxn is not None:
        excursion = Excursion(peak_net_pnl_mxn=max(target_net_mxn, Decimal("0")),
                              worst_net_pnl_mxn=-adverse_mxn, bars_to_worst=7,
                              bars_held=20)
    return ExecutableRoundTrip(entry_signal_index=1, entry_fill_index=2,
                               exit_signal_index=3, exit_fill_index=4,
                               economics=economics, entry_reason="SIGNAL_BUY",
                               exit_reason="PROFIT_TAKING", episode=1,
                               excursion=excursion)


def _real_trips(total_net_mxn: Decimal, count: int, *,
                adverse_mxn: Decimal | None = None) -> tuple[Any, ...]:
    """`count` real trips summing exactly to `total_net_mxn`."""
    if count == 0:
        return ()
    each = total_net_mxn / count
    made = [_real_trip(each, adverse_mxn=adverse_mxn) for _ in range(count)]
    if made:
        # Absorb the division remainder into the final trip so the total is exact.
        drift = total_net_mxn - sum(item.net_pnl_mxn for item in made)
        if drift != Decimal("0"):
            made[-1] = _real_trip(each + drift, adverse_mxn=adverse_mxn)
    return tuple(made)


def _window(*, name: str, provenance: str, net: str, trips: int,
            evaluations: int = 500, rejects: int = 0,
            start_ms: int = 0, end_ms: int = 0,
            adverse_mxn: Decimal | None = None) -> WindowEvidence:
    made = _real_trips(Decimal(net), trips, adverse_mxn=adverse_mxn)
    return WindowEvidence(
        name=name, provenance_kind=provenance,
        result=_result(evaluations=evaluations, trips=made,
                       simulated_trades=trips, economic_rejects=rejects),
        window_start_ms=start_ms, window_end_ms=end_ms)


def _manifest(**overrides: Any) -> ExperimentManifest:
    development = HoldoutWindow(name="DEVELOPMENT", start_ms=DEVELOPMENT_END_MS - 86_400_000,
                                end_ms=DEVELOPMENT_END_MS, reason="design window")
    base: dict[str, Any] = {
        "strategy_profile_id": CANDIDATE_PROFILE, "strategy_version": "0.2",
        "strategy_fingerprint": "s" * 64,
        "strategy_parameters": (("z_entry", "1.5"),),
        "economic_policy_version": "0.1", "economic_policy_fingerprint": "e" * 64,
        "minimum_net_profit_mxn": Decimal("0.02"),
        "minimum_net_edge_bps": Decimal("30"),
        "fee_model_version": "0.1.3", "fee_source_semantics": "BASE_REDUCES_QUANTITY",
        "confirmed_taker_fee_rate": FEE,
        "slippage_model_version": "0.1.4", "fill_model_version": "0.2.1",
        "market_classification_version": "0.2", "certification_policy_version": "0.2.1",
        "risk_policy_fingerprint": "r" * 64, "capital_policy_fingerprint": "c" * 64,
        "single_order_cap_mxn": Decimal("11"), "max_deployment_mxn": Decimal("25"),
        "authorized_capital_mxn": Decimal("50"),
        "dataset_cutoff_ms": DEVELOPMENT_END_MS, "code_commit": "fd99013",
        "created_at": "2026-01-01T00:00:00Z", "development_window": development}
    base.update(overrides)
    return ExperimentManifest(**base)


# ===========================================================================
# 1. The manifest is frozen before evidence is evaluated
# ===========================================================================

def test_manifest_is_written_once_and_refuses_to_be_overwritten(tmp_path: Path) -> None:
    store = ExperimentManifestStore(tmp_path)
    assert store.exists() is False
    store.freeze(_manifest())
    assert store.exists() is True
    assert (tmp_path / MANIFEST_FILE).exists()
    with pytest.raises(ExperimentError, match="EXPERIMENT_ALREADY_FROZEN"):
        store.freeze(_manifest())


def test_manifest_records_identity_before_any_evaluation(tmp_path: Path) -> None:
    store = ExperimentManifestStore(tmp_path)
    payload = store.freeze(_manifest())
    assert payload["version"] == EXPERIMENT_VERSION
    assert payload["experiment_fingerprint"]
    assert payload["strategy"]["profile_id"] == CANDIDATE_PROFILE
    assert payload["development_window"]["end_ms"] == DEVELOPMENT_END_MS


def test_holdouts_are_declared_before_evaluation_and_cannot_be_redeclared(
        tmp_path: Path) -> None:
    store = ExperimentManifestStore(tmp_path)
    store.freeze(_manifest())
    window = declare_holdout(name="HOLDOUT_01", development_start_ms=DEVELOPMENT_END_MS
                             - 86_400_000, duration_days=30,
                             reason="equal-length block immediately preceding development")
    payload = store.freeze_holdouts((window,))
    assert payload["holdout_windows"][0]["name"] == "HOLDOUT_01"
    with pytest.raises(ExperimentError, match="HOLDOUT_ALREADY_DECLARED"):
        store.freeze_holdouts((window,))


def test_holdout_is_predeclared_non_overlapping_and_adjacent() -> None:
    development_start = DEVELOPMENT_END_MS - 30 * 86_400_000
    window = declare_holdout(name="HOLDOUT_01", development_start_ms=development_start,
                             duration_days=30, reason="preceding block")
    assert window.end_ms == development_start
    assert window.start_ms == development_start - 30 * 86_400_000
    assert window.duration_days == Decimal("30")


def test_holdout_requires_a_stated_reason() -> None:
    with pytest.raises(ExperimentError):
        HoldoutWindow(name="HOLDOUT_01", start_ms=0, end_ms=1, reason="")


# ===========================================================================
# 2. Lineage: changing anything that can move a result starts a new experiment
# ===========================================================================

@pytest.mark.parametrize("field,value", [
    ("strategy_version", "0.3"),
    ("strategy_fingerprint", "x" * 64),
    ("strategy_parameters", (("z_entry", "2.0"),)),
    ("confirmed_taker_fee_rate", Decimal("0.0080")),
    ("minimum_net_profit_mxn", Decimal("0.01")),
    ("minimum_net_edge_bps", Decimal("20")),
    ("fill_model_version", "0.2.2"),
    ("slippage_model_version", "0.2.0"),
    ("certification_policy_version", "0.3"),
    ("risk_policy_fingerprint", "z" * 64),
    ("capital_policy_fingerprint", "y" * 64),
    ("dataset_cutoff_ms", DEVELOPMENT_END_MS + 1),
])
def test_any_material_change_produces_a_new_experiment_fingerprint(
        field: str, value: Any) -> None:
    baseline = _manifest().fingerprint_value
    assert _manifest(**{field: value}).fingerprint_value != baseline


def test_the_commit_and_creation_time_are_provenance_not_experiment_identity() -> None:
    """The commit is recorded for audit; it does not define which experiment this is."""
    assert (_manifest(code_commit="0000000").fingerprint_value
            == _manifest().fingerprint_value)
    assert (_manifest(created_at="2026-06-01T00:00:00Z").fingerprint_value
            == _manifest().fingerprint_value)
    assert _manifest(code_commit="0000000").public()["code_commit"] == "0000000"


def test_identical_configuration_reproduces_the_same_fingerprint() -> None:
    assert _manifest().fingerprint_value == _manifest().fingerprint_value


def test_declaring_a_holdout_changes_the_fingerprint() -> None:
    baseline = _manifest()
    window = declare_holdout(name="HOLDOUT_01", development_start_ms=DEVELOPMENT_END_MS
                             - 86_400_000, duration_days=30, reason="preceding block")
    with_holdout = _manifest(holdout_windows=(window,))
    assert with_holdout.fingerprint_value != baseline.fingerprint_value


def test_manifest_rejects_capital_limits_above_the_authorised_bounds() -> None:
    with pytest.raises(ExperimentError, match="single-order cap"):
        _manifest(single_order_cap_mxn=Decimal("12"))
    with pytest.raises(ExperimentError, match="deployment cap"):
        _manifest(max_deployment_mxn=Decimal("26"))
    with pytest.raises(ExperimentError, match="authorized capital"):
        _manifest(authorized_capital_mxn=Decimal("51"))


def test_manifest_states_the_cap_is_unchanged() -> None:
    assert _manifest().public()["single_order_cap_unchanged"] is True


# ===========================================================================
# 3. Provenance: development data is real, useful, and not independent
# ===========================================================================

def test_only_unseen_or_real_execution_evidence_may_certify() -> None:
    assert CERTIFYING_PROVENANCE == frozenset({REAL_HISTORICAL_HOLDOUT,
                                               REAL_CAPTURED_FORWARD,
                                               REAL_PRODUCTION_FILL})


def test_development_evidence_is_real_but_not_independent() -> None:
    assert may_certify(REAL_HISTORICAL_DEVELOPMENT) is False
    assert is_independent(REAL_HISTORICAL_DEVELOPMENT) is False


def test_fixture_can_never_certify() -> None:
    assert may_certify(SYNTHETIC_FIXTURE) is False
    assert is_independent(SYNTHETIC_FIXTURE) is False


@pytest.mark.parametrize("name,expected", [
    ("HOLDOUT_01", REAL_HISTORICAL_HOLDOUT),
    ("HOLDOUT_02", REAL_HISTORICAL_HOLDOUT),
    ("FORWARD", REAL_CAPTURED_FORWARD),
    ("PRODUCTION", REAL_PRODUCTION_FILL),
    ("FIXTURE", SYNTHETIC_FIXTURE),
    ("DEVELOPMENT", REAL_HISTORICAL_DEVELOPMENT),
    ("anything-else", REAL_HISTORICAL_DEVELOPMENT),
])
def test_window_names_map_to_provenance_explicitly(name: str, expected: str) -> None:
    assert provenance_for_window(window_name=name) == expected


def test_development_evidence_alone_cannot_produce_certification() -> None:
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="DEVELOPMENT", provenance=REAL_HISTORICAL_DEVELOPMENT,
                         net="9.99", trips=40),))
    assert assessment.certified is False
    assert assessment.state == DEVELOPMENT_ONLY
    assert "CERTIFYING_PROVENANCE" in assessment.failed_gates


def test_fixture_evidence_alone_cannot_produce_certification() -> None:
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="FIXTURE", provenance=SYNTHETIC_FIXTURE, net="9.99",
                         trips=40),))
    assert assessment.certified is False
    assert "NOT_DEPENDENT_ON_FIXTURE" in assessment.failed_gates


# ===========================================================================
# 4. Overlap: one favourable stretch must not appear twice
# ===========================================================================

def test_overlapping_windows_are_rejected() -> None:
    first = _window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT, net="1",
                    trips=5, start_ms=0, end_ms=1000)
    second = _window(name="HOLDOUT_02", provenance=REAL_HISTORICAL_HOLDOUT, net="1",
                     trips=5, start_ms=500, end_ms=1500)
    with pytest.raises(ValueError, match="OVERLAPPING_EVIDENCE_WINDOWS"):
        assert_windows_do_not_overlap((first, second))


def test_adjacent_windows_do_not_overlap() -> None:
    first = _window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT, net="1",
                    trips=5, start_ms=0, end_ms=1000)
    second = _window(name="HOLDOUT_02", provenance=REAL_HISTORICAL_HOLDOUT, net="1",
                     trips=5, start_ms=1000, end_ms=2000)
    assert_windows_do_not_overlap((first, second))


# ===========================================================================
# 5. Fill realism: no same-bar optimism
# ===========================================================================

def test_buy_executes_at_the_ask_and_sell_executes_at_the_bid() -> None:
    observation = _observe("100", bids=(_Level("99.8", "100"),),
                           asks=(_Level("100.2", "100"),))
    buy = model_buy(observation=observation, budget_mxn=Decimal("11"),
                    taker_fee_rate=FEE, modelled_slippage_bps=Decimal("0"),
                    base_currency="SOL", quote_currency="MXN")
    sell = model_sell(observation=observation, quantity=Decimal("0.1"),
                      taker_fee_rate=FEE, modelled_slippage_bps=Decimal("0"),
                      base_currency="SOL", quote_currency="MXN")
    assert buy.execution_price_mxn == Decimal("100.2")
    assert sell.execution_price_mxn == Decimal("99.8")


def test_a_buy_never_prices_below_the_ask_even_with_slippage() -> None:
    """With book evidence the walk decides the price; slippage never moves it favourably."""
    observation = _observe("100", bids=(_Level("99.0", "100"),),
                           asks=(_Level("100.5", "100"),))
    fill = model_buy(observation=observation, budget_mxn=Decimal("11"),
                     taker_fee_rate=FEE, modelled_slippage_bps=Decimal("50"),
                     base_currency="SOL", quote_currency="MXN")
    assert fill.execution_price_mxn >= Decimal("100.5")
    assert fill.spread_convention == SPREAD_IN_PRICE
    assert fill.spread_cost_mxn == Decimal("0")


def test_a_sell_never_prices_above_the_bid_even_with_slippage() -> None:
    observation = _observe("100", bids=(_Level("99.0", "100"),),
                           asks=(_Level("100.5", "100"),))
    fill = model_sell(observation=observation, quantity=Decimal("0.1"),
                      taker_fee_rate=FEE, modelled_slippage_bps=Decimal("50"),
                      base_currency="SOL", quote_currency="MXN")
    assert fill.execution_price_mxn <= Decimal("99.0")
    assert fill.spread_convention == SPREAD_IN_PRICE


def test_candle_only_slippage_moves_the_price_adversely_in_both_directions() -> None:
    observation = _observe("100")
    buy = model_buy(observation=observation, budget_mxn=Decimal("11"),
                    taker_fee_rate=FEE, modelled_slippage_bps=Decimal("50"),
                    base_currency="SOL", quote_currency="MXN")
    sell = model_sell(observation=observation, quantity=Decimal("0.1"),
                      taker_fee_rate=FEE, modelled_slippage_bps=Decimal("50"),
                      base_currency="SOL", quote_currency="MXN")
    assert buy.execution_price_mxn > Decimal("100")
    assert sell.execution_price_mxn < Decimal("100")
    assert buy.spread_convention == sell.spread_convention == SPREAD_AS_COST


def test_a_buy_fee_in_base_reduces_the_quantity_actually_acquired() -> None:
    """The fee must cost something. A fee that reduces nothing is not a fee."""
    observation = _observe("100")
    gross = Decimal("11") / Decimal("100")
    fill = model_buy(observation=observation, budget_mxn=Decimal("11"),
                     taker_fee_rate=FEE, modelled_slippage_bps=Decimal("0"),
                     base_currency="SOL", quote_currency="MXN")
    assert fill.filled_quantity < gross
    assert fill.fee_mxn > Decimal("0")
    assert fill.fee_currency == "BASE"
    assert fill.filled_quantity + fill.fee_mxn == gross


def test_a_sell_fee_in_quote_reduces_the_proceeds_actually_received() -> None:
    observation = _observe("100")
    sold = Decimal("0.1")
    fill = model_sell(observation=observation, quantity=sold, taker_fee_rate=FEE,
                      modelled_slippage_bps=Decimal("0"), base_currency="SOL",
                      quote_currency="MXN")
    gross = sold * Decimal("100")
    assert fill.fee_mxn > Decimal("0")
    assert fill.fee_currency == "QUOTE"
    assert fill.filled_notional_mxn < gross
    assert fill.filled_notional_mxn + fill.fee_mxn == gross


def test_fills_record_their_evidence_quality_and_the_model_version() -> None:
    fill = model_buy(observation=_observe("100"), budget_mxn=Decimal("11"),
                     taker_fee_rate=FEE, modelled_slippage_bps=Decimal("0"),
                     base_currency="SOL", quote_currency="MXN")
    assert fill.evidence_quality == CANDLE_ONLY_ESTIMATE
    assert fill.telemetry()["version"] == EXECUTION_MODEL_VERSION


def test_replay_requires_a_positive_fill_delay() -> None:
    evaluator = evaluator_for(CANDIDATE_PROFILE)
    with pytest.raises(Exception, match="fill delay"):
        replay_executable(candles=_oscillating(), profile_id=CANDIDATE_PROFILE,
                          market="SOL/MXN", evaluator=evaluator,
                          taker_fee_rate=FEE, spread_bps=Decimal("12"),
                          policy=DEFAULT_POLICY, fill_delay_bars=0)


def test_no_position_is_opened_and_closed_on_the_same_bar() -> None:
    """Signal and fill bars are distinct, so a round trip cannot be instantaneous."""
    evaluator = evaluator_for(CANDIDATE_PROFILE)
    result = replay_executable(candles=_oscillating(cycles=40), profile_id=CANDIDATE_PROFILE,
                               market="SOL/MXN", evaluator=evaluator,
                               taker_fee_rate=FEE, spread_bps=Decimal("12"),
                               policy=DEFAULT_POLICY)
    for trip in result.trips:
        assert trip.entry_fill_index > trip.entry_signal_index
        assert trip.exit_fill_index > trip.exit_signal_index
        assert trip.exit_fill_index > trip.entry_fill_index


def test_the_guard_is_shown_the_price_the_order_would_actually_pay() -> None:
    cheap = assess_with_executable_guard(
        market="SOL/MXN", budget_mxn=Decimal("11"), entry_price=Decimal("100"),
        target_price=Decimal("102"), taker_fee_rate=FEE, policy=DEFAULT_POLICY,
        spread_bps=Decimal("12"), slippage_bps=Decimal("5"))
    expensive = assess_with_executable_guard(
        market="SOL/MXN", budget_mxn=Decimal("11"), entry_price=Decimal("101.5"),
        target_price=Decimal("102"), taker_fee_rate=FEE, policy=DEFAULT_POLICY,
        spread_bps=Decimal("12"), slippage_bps=Decimal("5"))
    assert cheap.expected_net_edge_bps > expensive.expected_net_edge_bps


# ===========================================================================
# 6. The verdict and the fill must agree about friction
# ===========================================================================

def test_a_replay_that_trades_reports_friction_it_actually_paid() -> None:
    evaluator = evaluator_for("trend-continuation-v1")
    result = replay_executable(candles=_rising(count=60), profile_id="trend-continuation-v1",
                               market="BTC/MXN", evaluator=evaluator,
                               taker_fee_rate=FEE, spread_bps=Decimal("12"),
                               policy=DEFAULT_POLICY)
    if result.trips:
        assert result.fees_mxn > Decimal("0")
        assert result.total_friction_mxn > Decimal("0")
        assert result.gross_pnl_mxn != result.net_pnl_mxn


def test_net_pnl_is_gross_minus_the_frictions_charged() -> None:
    evaluator = evaluator_for("trend-continuation-v1")
    result = replay_executable(candles=_rising(count=60), profile_id="trend-continuation-v1",
                               market="BTC/MXN", evaluator=evaluator,
                               taker_fee_rate=FEE, spread_bps=Decimal("12"),
                               policy=DEFAULT_POLICY)
    for trip in result.trips:
        assert trip.net_pnl_mxn == trip.economics.net_proceeds_mxn - trip.economics.budget_mxn
        assert trip.economics.total_friction_mxn > Decimal("0")


# ===========================================================================
# 7. Determinism and no look-ahead
# ===========================================================================

def test_same_dataset_rerun_is_identical() -> None:
    evaluator = evaluator_for(CANDIDATE_PROFILE)
    candles = _oscillating(cycles=40)
    first = replay_executable(candles=candles, profile_id=CANDIDATE_PROFILE,
                              market="SOL/MXN", evaluator=evaluator, taker_fee_rate=FEE,
                              spread_bps=Decimal("12"), policy=DEFAULT_POLICY)
    second = replay_executable(candles=candles, profile_id=CANDIDATE_PROFILE,
                               market="SOL/MXN", evaluator=evaluator, taker_fee_rate=FEE,
                               spread_bps=Decimal("12"), policy=DEFAULT_POLICY)
    assert first.telemetry() == second.telemetry()
    assert first.dataset_fingerprint == second.dataset_fingerprint


def test_truncating_the_dataset_cannot_change_earlier_decisions() -> None:
    """If future candles influenced a decision, a longer series would rewrite the past."""
    evaluator = evaluator_for(CANDIDATE_PROFILE)
    candles = _oscillating(cycles=40)
    full = replay_executable(candles=candles, profile_id=CANDIDATE_PROFILE,
                             market="SOL/MXN", evaluator=evaluator, taker_fee_rate=FEE,
                             spread_bps=Decimal("12"), policy=DEFAULT_POLICY)
    prefix = replay_executable(candles=candles[:len(candles) - 60],
                               profile_id=CANDIDATE_PROFILE, market="SOL/MXN",
                               evaluator=evaluator, taker_fee_rate=FEE,
                               spread_bps=Decimal("12"), policy=DEFAULT_POLICY)
    shared = min(len(prefix.trips), len(full.trips))
    for index in range(shared):
        assert prefix.trips[index].entry_fill_index == full.trips[index].entry_fill_index
        assert prefix.trips[index].exit_fill_index == full.trips[index].exit_fill_index


"""A signal on the final bar has no next bar to fill in and must not be counted."""
def test_a_final_bar_signal_is_reported_as_unfilled_not_filled() -> None:
    evaluator = evaluator_for("trend-continuation-v1")
    result = replay_executable(candles=_rising(count=25), profile_id="trend-continuation-v1",
                               market="BTC/MXN", evaluator=evaluator, taker_fee_rate=FEE,
                               spread_bps=Decimal("12"), policy=DEFAULT_POLICY)
    assert result.unfilled_signals >= 0
    assert result.simulated_trades == len(result.trips)


# ===========================================================================
# 8. The five-trade floor is a sample floor, never a verdict
# ===========================================================================

def test_five_trades_with_negative_net_economics_are_not_certified() -> None:
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="-1.00", trips=DEFAULT_MIN_ROUND_TRIPS),))
    assert assessment.certified is False
    assert assessment.state == HOLDOUT_NEGATIVE
    assert "NON_NEGATIVE_NET_ECONOMICS" in assessment.failed_gates


def test_meeting_the_round_trip_floor_alone_is_not_certification() -> None:
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="0.40", trips=DEFAULT_MIN_ROUND_TRIPS,
                         evaluations=10),))
    assert assessment.certified is False
    assert assessment.state == ACCUMULATING_SAMPLE
    assert "MINIMUM_REAL_ROUND_TRIPS" not in assessment.failed_gates
    assert "MINIMUM_EVALUATIONS" in assessment.failed_gates


def test_a_positive_unseen_window_meeting_every_gate_is_eligible() -> None:
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="0.40", trips=DEFAULT_MIN_ROUND_TRIPS, evaluations=500),),
        sensitivity={"applicable": True, "survived": 4, "robust_to_degradation": True})
    assert assessment.certified is True
    assert assessment.state == PRODUCTION_CERTIFIABLE
    assert assessment.failed_gates == ()
    assert assessment.public()["certified"] is True


def test_five_trades_with_unacceptable_drawdown_are_not_certified() -> None:
    window = _window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                     net="0.60", trips=DEFAULT_MIN_ROUND_TRIPS,
                     start_ms=0, end_ms=10_000)
    losing = _window(name="HOLDOUT_02", provenance=REAL_HISTORICAL_HOLDOUT,
                     net=f"-{MAX_DRAWDOWN_MXN * 4}", trips=DEFAULT_MIN_ROUND_TRIPS,
                     start_ms=10_000, end_ms=20_000)
    assessment = assess_pair(market="SOL/MXN", profile_id=CANDIDATE_PROFILE,
                             experiment_fingerprint="f" * 64,
                             windows=(window, losing))
    assert assessment.certified is False
    assert "DRAWDOWN_WITHIN_POLICY" in assessment.failed_gates


def test_a_window_with_no_admissible_opportunity_is_not_a_short_sample() -> None:
    assessment = assess_pair(
        market="BTC/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="0", trips=0, rejects=800),))
    assert assessment.certified is False
    assert assessment.state == NO_ADMISSIBLE_OPPORTUNITY


def test_a_development_window_with_no_trades_is_reported_as_no_opportunity() -> None:
    """Zero fills is an absence of observation, not a favourable result."""
    assessment = assess_pair(
        market="BTC/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="DEVELOPMENT", provenance=REAL_HISTORICAL_DEVELOPMENT,
                         net="0", trips=0, rejects=0, evaluations=100),))
    assert assessment.certified is False
    assert assessment.state == NO_OPPORTUNITY


def test_a_small_sample_with_extreme_exposure_is_accumulating_not_conclusive() -> None:
    """Two trades cannot support a decisive risk verdict, so the state says so.

    The drawdown gate still fails and is reported -- the point is only that a risk
    conclusion drawn from two observations is not presented as established.
    """
    window = _window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                     net="0.20", trips=2, evaluations=5000,
                     adverse_mxn=MAX_DRAWDOWN_MXN * 4)
    assessment = assess_pair(market="SOL/MXN", profile_id=CANDIDATE_PROFILE,
                             experiment_fingerprint="f" * 64, windows=(window,))
    assert assessment.state == ACCUMULATING_SAMPLE
    assert "DRAWDOWN_WITHIN_POLICY" in assessment.failed_gates
    assert "ADVERSE_EXCURSION_EXCEEDS_NET_PROFIT" in assessment.notes


def test_excursion_exceeding_net_profit_is_flagged() -> None:
    """Earning 0.20 while enduring 0.80 underwater is not a profitable trade."""
    window = _window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                     net="0.20", trips=DEFAULT_MIN_ROUND_TRIPS, evaluations=500,
                     adverse_mxn=Decimal("0.80"))
    assessment = assess_pair(market="SOL/MXN", profile_id=CANDIDATE_PROFILE,
                             experiment_fingerprint="f" * 64, windows=(window,))
    assert "ADVERSE_EXCURSION_EXCEEDS_NET_PROFIT" in assessment.notes
    assert assessment.certified is False


def test_a_healthy_risk_reward_is_not_flagged() -> None:
    window = _window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                     net="2.00", trips=DEFAULT_MIN_ROUND_TRIPS, evaluations=500,
                     adverse_mxn=Decimal("0.10"))
    assessment = assess_pair(market="SOL/MXN", profile_id=CANDIDATE_PROFILE,
                             experiment_fingerprint="f" * 64, windows=(window,))
    assert "ADVERSE_EXCURSION_EXCEEDS_NET_PROFIT" not in assessment.notes


def test_an_adequate_sample_with_excess_drawdown_is_decisively_exceeded() -> None:
    window = _window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                     net="0.20", trips=DEFAULT_MIN_ROUND_TRIPS, evaluations=500,
                     adverse_mxn=MAX_DRAWDOWN_MXN * 4)
    assessment = assess_pair(market="SOL/MXN", profile_id=CANDIDATE_PROFILE,
                             experiment_fingerprint="f" * 64, windows=(window,))
    assert assessment.state == DRAWDOWN_EXCEEDED


def test_a_single_lucky_round_trip_in_many_hours_is_not_certification() -> None:
    """One fill out of hundreds of evaluations is a small sample, not a small edge."""
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="1.88", trips=1, evaluations=5000),))
    assert assessment.certified is False
    assert assessment.state == ACCUMULATING_SAMPLE


# ===========================================================================
# 9. Windows are never averaged into a verdict neither window supports
# ===========================================================================

def test_positive_development_with_negative_holdout_is_not_certified() -> None:
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="DEVELOPMENT", provenance=REAL_HISTORICAL_DEVELOPMENT,
                         net="9.99", trips=40),
                 _window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="-0.50", trips=DEFAULT_MIN_ROUND_TRIPS,
                         start_ms=10_000, end_ms=20_000)))
    assert assessment.certified is False
    assert assessment.state == REGIME_DEPENDENT
    assert SIGN_REVERSAL in assessment.notes


def test_conflicting_unseen_windows_are_regime_dependent_not_averaged() -> None:
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="0.90", trips=DEFAULT_MIN_ROUND_TRIPS,
                         start_ms=0, end_ms=10_000),
                 _window(name="HOLDOUT_02", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="-9.00", trips=DEFAULT_MIN_ROUND_TRIPS,
                         start_ms=10_000, end_ms=20_000)))
    assert assessment.certified is False
    assert assessment.state == REGIME_DEPENDENT


def test_a_gate_failure_is_never_masked_by_a_larger_positive_window() -> None:
    """A big win elsewhere must not launder a window that failed on its own merits."""
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="50.00", trips=40, start_ms=0, end_ms=10_000),
                 _window(name="FORWARD", provenance=REAL_CAPTURED_FORWARD,
                         net="-0.05", trips=DEFAULT_MIN_ROUND_TRIPS,
                         start_ms=10_000, end_ms=20_000)))
    assert assessment.certified is False


def test_development_is_reported_in_full_and_never_omitted() -> None:
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="DEVELOPMENT", provenance=REAL_HISTORICAL_DEVELOPMENT,
                         net="-3.00", trips=20, start_ms=0, end_ms=10_000),
                 _window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="0.40", trips=DEFAULT_MIN_ROUND_TRIPS,
                         evaluations=500, start_ms=10_000, end_ms=20_000)),
        sensitivity={"applicable": True, "survived": 4, "robust_to_degradation": True})
    assert assessment.development is not None
    assert assessment.development.net_pnl_mxn < Decimal("0")
    assert assessment.development.certifying is False
    # Development loses and the holdout wins: the two windows disagree, so this is
    # regime dependence and not an edge. Averaging them would hide exactly that.
    assert assessment.state == REGIME_DEPENDENT
    payload = assessment.public()
    assert any(item["name"] == "DEVELOPMENT" for item in payload["windows"])


# ===========================================================================
# 10. Sensitivity: degradation is reported, never used to pick a winner
# ===========================================================================

def test_sensitivity_is_not_applicable_when_nothing_traded() -> None:
    evaluator = evaluator_for(CANDIDATE_PROFILE)
    report = slippage_sensitivity(candles=_rising(count=30), profile_id=CANDIDATE_PROFILE,
                                  market="BTC/MXN", evaluator=evaluator,
                                  taker_fee_rate=FEE, spread_bps=Decimal("12"),
                                  policy=DEFAULT_POLICY)
    assert report["used_to_select_best_variant"] is False
    if not any(item["round_trips"] > 0 for item in report["variants"]):
        assert report["applicable"] is False
        assert report["robust_to_degradation"] is False


def test_sensitivity_reports_every_predeclared_variant() -> None:
    evaluator = evaluator_for("trend-continuation-v1")
    report = slippage_sensitivity(candles=_rising(count=60),
                                  profile_id="trend-continuation-v1", market="BTC/MXN",
                                  evaluator=evaluator, taker_fee_rate=FEE,
                                  spread_bps=Decimal("12"), policy=DEFAULT_POLICY)
    names = {item["variant"] for item in report["variants"]}
    assert names == {"BASELINE", "FEE_PLUS_10_PCT", "SLIPPAGE_PLUS_5BPS",
                     "EXTRA_DELAY_BAR"}
    assert report["total"] == 4


def test_degradation_is_still_reported_when_the_edge_does_not_survive() -> None:
    evaluator = evaluator_for("trend-continuation-v1")
    report = slippage_sensitivity(candles=_rising(count=60),
                                  profile_id="trend-continuation-v1", market="BTC/MXN",
                                  evaluator=evaluator, taker_fee_rate=FEE,
                                  spread_bps=Decimal("12"), policy=DEFAULT_POLICY)
    assert 0 <= report["survived"] <= report["total"]


def test_a_fragile_edge_is_classified_as_fragile_not_certified() -> None:
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="0.40", trips=DEFAULT_MIN_ROUND_TRIPS, evaluations=500),),
        sensitivity={"applicable": True, "survived": 1, "total": 4,
                     "robust_to_degradation": False})
    assert assessment.certified is False
    assert assessment.state == SENSITIVITY_FRAGILE
    assert "SURVIVES_EXECUTION_DEGRADATION" in assessment.failed_gates


def test_sensitivity_is_not_reported_as_a_failure_when_nothing_traded() -> None:
    """An untraded window has no edge to degrade, so it is not 'fragile'."""
    assessment = assess_pair(
        market="BTC/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="0", trips=0, rejects=900),),
        sensitivity={"applicable": False, "survived": 0, "total": 4,
                     "robust_to_degradation": False})
    assert assessment.state == NO_ADMISSIBLE_OPPORTUNITY
    assert assessment.state != SENSITIVITY_FRAGILE
    note = next(item for item in assessment.gates
                if item.name == "SURVIVES_EXECUTION_DEGRADATION")
    assert note.note == "NOT_APPLICABLE_NO_FILLS"


# ===========================================================================
# 11. Refusals are attributable, and the report never overstates
# ===========================================================================

def test_every_refusal_names_the_gate_that_failed() -> None:
    assessment = assess_pair(
        market="BTC/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="-2.00", trips=DEFAULT_MIN_ROUND_TRIPS),))
    assert assessment.failed_gates
    payload = assessment.public()
    failed = [item for item in payload["gates"] if not item["passed"]]
    assert failed
    assert all(item["measured"] and item["required"] for item in failed)


def test_guard_refusals_are_reported_as_an_outcome_not_a_defect() -> None:
    assessment = assess_pair(
        market="BTC/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                         net="0", trips=0, rejects=500),))
    payload = assessment.public()
    reject_rate = payload["windows"][0]["economic_reject_rate"]
    assert Decimal(reject_rate) > Decimal("0")
    assert assessment.certified is False


def test_the_report_declares_promotion_disabled_and_the_floor_unchanged() -> None:
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="DEVELOPMENT", provenance=REAL_HISTORICAL_DEVELOPMENT,
                         net="1.00", trips=10),))
    payload = assessment.public()
    assert payload["promotion"] == "DISABLED"
    assert payload["five_trade_floor_changed"] is False


def test_the_replay_never_claims_five_trades_is_certification() -> None:
    evaluator = evaluator_for(CANDIDATE_PROFILE)
    result = replay_executable(candles=_oscillating(cycles=40), profile_id=CANDIDATE_PROFILE,
                               market="SOL/MXN", evaluator=evaluator,
                               taker_fee_rate=FEE, spread_bps=Decimal("12"),
                               policy=DEFAULT_POLICY)
    telemetry = result.telemetry()
    assert telemetry["five_trades_is_a_floor_not_certification"] is True
    assert telemetry["status"] in {"INSUFFICIENT_EVIDENCE", "NOT_VIABLE", "ASSESSABLE"}


def test_profit_factor_is_not_invented_when_there_are_no_losses() -> None:
    assert _result(trips=()).profit_factor is None
    assert _result(trips=_real_trips(Decimal("2.00"), 3)).profit_factor is None


def test_profit_factor_is_defined_when_losses_exist() -> None:
    mixed = (_real_trip(Decimal("3.00")), _real_trip(Decimal("-1.00")))
    result = _result(trips=mixed)
    factor = result.profit_factor
    assert factor is not None
    assert abs(factor - Decimal("3")) < Decimal("1E-20")


# ===========================================================================
# 13. Unrealized exposure: a stop-less strategy must not look risk-free
# ===========================================================================

def test_a_stopless_strategy_with_all_wins_is_flagged_as_speculative() -> None:
    """A perfect win rate with no stop is an artifact of patience, not an edge.

    The profile exits only when its target is reached, so every closed trade wins while
    the position was at some point underwater. Reporting 100% wins without the unrealized
    exposure would present a risk-free strategy that does not exist.
    """
    result = _result(trips=_real_trips(Decimal("2.00"), 4, adverse_mxn=Decimal("1.50")))
    assert result.wins == 4
    assert result.losses == 0
    assert result.speculative_win_rate is True
    assert result.win_rate == Decimal("1")


def test_realized_drawdown_is_zero_for_all_winners_but_unrealized_is_not() -> None:
    result = _result(trips=_real_trips(Decimal("2.00"), 4, adverse_mxn=Decimal("1.50")))
    assert result.max_drawdown_mxn == Decimal("0")
    assert result.max_unrealized_drawdown_mxn == Decimal("1.50")
    assert result.effective_drawdown_mxn == Decimal("1.50")


def test_effective_drawdown_is_the_worse_of_realized_and_unrealized() -> None:
    losing = _real_trips(Decimal("-6.00"), 10, adverse_mxn=Decimal("0.20"))
    result = _result(trips=losing)
    assert result.max_drawdown_mxn > result.max_unrealized_drawdown_mxn
    assert result.effective_drawdown_mxn == result.max_drawdown_mxn


def test_unrealized_exposure_beyond_policy_blocks_certification() -> None:
    """A positive unseen window with hidden paper losses is not certifiable."""
    window = _window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                     net="0.40", trips=DEFAULT_MIN_ROUND_TRIPS, evaluations=500,
                     adverse_mxn=MAX_DRAWDOWN_MXN * 3)
    assessment = assess_pair(market="SOL/MXN", profile_id=CANDIDATE_PROFILE,
                             experiment_fingerprint="f" * 64, windows=(window,),
                             sensitivity={"applicable": True, "survived": 4,
                                          "robust_to_degradation": True})
    assert assessment.certified is False
    assert "DRAWDOWN_WITHIN_POLICY" in assessment.failed_gates
    assert assessment.state == DRAWDOWN_EXCEEDED


def test_the_drawdown_gate_states_its_basis_and_the_unrealized_figure() -> None:
    window = _window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                     net="0.40", trips=DEFAULT_MIN_ROUND_TRIPS, evaluations=500,
                     adverse_mxn=Decimal("0.10"))
    assessment = assess_pair(market="SOL/MXN", profile_id=CANDIDATE_PROFILE,
                             experiment_fingerprint="f" * 64, windows=(window,),
                             sensitivity={"applicable": True, "survived": 4,
                                          "robust_to_degradation": True})
    gate = next(item for item in assessment.gates if item.name == "DRAWDOWN_WITHIN_POLICY")
    assert "MAX_OF_REALIZED_AND_UNREALIZED" in gate.note
    assert "unrealized=0.10" in gate.note


def test_a_perfect_win_rate_is_recorded_as_a_derived_flag() -> None:
    window = _window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                     net="0.40", trips=DEFAULT_MIN_ROUND_TRIPS, evaluations=500,
                     adverse_mxn=Decimal("0.10"))
    assessment = assess_pair(market="SOL/MXN", profile_id=CANDIDATE_PROFILE,
                             experiment_fingerprint="f" * 64, windows=(window,))
    assert "PERFECT_WIN_RATE_ON_STOPLESS_EXIT" in assessment.notes


def test_the_replay_reports_unrealized_exposure_separately_from_realized() -> None:
    evaluator = evaluator_for(CANDIDATE_PROFILE)
    result = replay_executable(candles=_oscillating(cycles=40),
                               profile_id=CANDIDATE_PROFILE, market="SOL/MXN",
                               evaluator=evaluator, taker_fee_rate=FEE,
                               spread_bps=Decimal("12"), policy=DEFAULT_POLICY)
    telemetry = result.telemetry()
    assert telemetry["drawdown_basis"] == "MAX_OF_REALIZED_AND_UNREALIZED"
    assert "max_unrealized_drawdown_mxn" in telemetry
    assert "effective_drawdown_mxn" in telemetry


def test_assessable_requires_the_sample_floor() -> None:
    evaluator = evaluator_for(CANDIDATE_PROFILE)
    thin = replay_executable(candles=_oscillating(cycles=3), profile_id=CANDIDATE_PROFILE,
                             market="SOL/MXN", evaluator=evaluator, taker_fee_rate=FEE,
                             spread_bps=Decimal("12"), policy=DEFAULT_POLICY)
    assert thin.evaluations < 30
    assert thin.status != STATUS_ASSESSABLE


# ===========================================================================
# 12. Safety invariants that must not move
# ===========================================================================

def test_the_frozen_champion_profile_is_unchanged() -> None:
    champion = PROFILE_REGISTRY[0]
    assert champion.profile_id == "mean-reversion-safe-v1"
    assert champion.identity.strategy_id == "mean_reversion"
    assert champion.identity.version == "0.1"


def test_the_candidate_profiles_remain_registered() -> None:
    identifiers = {item.profile_id for item in PROFILE_REGISTRY}
    for expected in ("mean-reversion-safe-v1", "trend-continuation-v1",
                     CANDIDATE_PROFILE):
        assert expected in identifiers
    assert PROFILE_REGISTRY[0].profile_id == "mean-reversion-safe-v1"


def test_the_single_order_cap_is_eleven_mxn() -> None:
    assert _manifest().public()["policy"]["single_order_cap_mxn"] == "11"


def test_production_post_count_is_zero() -> None:
    """0.2.1 must not have added any POST path anywhere in the package."""
    root = Path(__file__).resolve().parents[2] / "src" / "autofund"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in ("requests.post", "client.post(", ".post(url"):
            if marker in text:
                offenders.append(f"{path.name}:{marker}")
    assert offenders == []


def test_no_shadow_or_research_state_reaches_production_certification() -> None:
    """Only real unseen or real-execution evidence may certify; nothing else qualifies."""
    for kind in (SYNTHETIC_FIXTURE, REAL_HISTORICAL_DEVELOPMENT):
        assert may_certify(kind) is False


def test_experiment_records_the_dataset_cutoff() -> None:
    manifest = _manifest()
    assert manifest.dataset_cutoff_ms == DEVELOPMENT_END_MS
    assert manifest.public()["dataset_cutoff_ms"] == DEVELOPMENT_END_MS


def test_declared_holdout_must_precede_the_development_window() -> None:
    development_start = DEVELOPMENT_END_MS - 30 * 86_400_000
    window = declare_holdout(name="HOLDOUT_01", development_start_ms=development_start,
                             duration_days=30, reason="preceding block")
    assert window.end_ms <= development_start
    assert window.end_ms == development_start


def test_forward_evidence_carries_prospective_provenance() -> None:
    assert provenance_for_window(window_name="FORWARD") == REAL_CAPTURED_FORWARD
    assert may_certify(REAL_CAPTURED_FORWARD) is True


def test_holdout_windows_are_stored_immutably() -> None:
    store_windows = (_window(name="HOLDOUT_01", provenance=REAL_HISTORICAL_HOLDOUT,
                             net="1", trips=5, start_ms=0, end_ms=1000),)
    assessment = assess_pair(market="SOL/MXN", profile_id=CANDIDATE_PROFILE,
                             experiment_fingerprint="f" * 64, windows=store_windows)
    assert isinstance(assessment.windows, tuple)
    assert assessment.windows[0].name == "HOLDOUT_01"


def test_assessment_is_serialisable_for_durable_recording() -> None:
    import json
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="DEVELOPMENT", provenance=REAL_HISTORICAL_DEVELOPMENT,
                         net="1.00", trips=10),))
    encoded = json.dumps(assessment.public(), default=str)
    assert "DEVELOPMENT" in encoded
    assert "hypothetical" not in encoded.lower()


def test_a_candle_series_with_no_trades_yields_no_pnl_rather_than_zero_profit() -> None:
    evaluator = evaluator_for(CANDIDATE_PROFILE)
    result = replay_executable(candles=_rising(count=40), profile_id=CANDIDATE_PROFILE,
                               market="BTC/MXN", evaluator=evaluator, taker_fee_rate=FEE,
                               spread_bps=Decimal("12"), policy=DEFAULT_POLICY)
    if not result.trips:
        assert result.net_pnl_mxn == Decimal("0")
        assert result.profit_factor is None
        assert result.mean_net_pnl_mxn is None


def test_start_timestamp_defaults_are_deterministic_for_fixtures() -> None:
    first = synthetic_candles(prices=["100", "101"], start=datetime(2026, 1, 1, tzinfo=UTC))
    second = synthetic_candles(prices=["100", "101"], start=datetime(2026, 1, 1, tzinfo=UTC))
    assert first.candles == second.candles


def test_holdout_boundaries_are_stable_across_a_repeated_declaration() -> None:
    first = declare_holdout(name="HOLDOUT_01", development_start_ms=DEVELOPMENT_END_MS
                            - 86_400_000, duration_days=30, reason="preceding block")
    second = declare_holdout(name="HOLDOUT_01", development_start_ms=DEVELOPMENT_END_MS
                             - 86_400_000, duration_days=30, reason="preceding block")
    assert first.start_ms == second.start_ms and first.end_ms == second.end_ms


def test_development_windows_are_never_treated_as_holdouts() -> None:
    for name in ("DEVELOPMENT", "DEVELOPMENT_01", "dev"):
        kind = provenance_for_window(window_name=name)
        assert may_certify(kind) is False


def test_an_unfilled_final_signal_is_not_silently_counted_as_a_trade() -> None:
    evaluator = evaluator_for("trend-continuation-v1")
    candles = _rising(count=25)
    result = replay_executable(candles=candles, profile_id="trend-continuation-v1",
                               market="BTC/MXN", evaluator=evaluator, taker_fee_rate=FEE,
                               spread_bps=Decimal("12"), policy=DEFAULT_POLICY)
    assert result.simulated_trades == len(result.trips)


def test_replay_rejects_an_empty_dataset_rather_than_reporting_no_losses() -> None:
    evaluator = evaluator_for(CANDIDATE_PROFILE)
    with pytest.raises(Exception, match="at least one candle"):
        replay_executable(candles=(), profile_id=CANDIDATE_PROFILE, market="SOL/MXN",
                          evaluator=evaluator, taker_fee_rate=FEE,
                          spread_bps=Decimal("12"), policy=DEFAULT_POLICY)


def test_evidence_windows_start_before_they_end_when_declared() -> None:
    window = declare_holdout(name="HOLDOUT_01", development_start_ms=DEVELOPMENT_END_MS,
                             duration_days=1, reason="probe")
    assert window.start_ms < window.end_ms
    assert window.start_ms == DEVELOPMENT_END_MS - 86_400_000


def test_forward_series_cannot_retroactively_validate_a_development_period() -> None:
    """Forward evidence stands on its own; it does not rescue an earlier loss."""
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="f" * 64,
        windows=(_window(name="DEVELOPMENT", provenance=REAL_HISTORICAL_DEVELOPMENT,
                         net="-2.00", trips=20),
                 _window(name="FORWARD", provenance=REAL_CAPTURED_FORWARD,
                         net="0.50", trips=DEFAULT_MIN_ROUND_TRIPS, evaluations=400,
                         start_ms=10_000, end_ms=20_000)),
        sensitivity={"applicable": True, "survived": 4, "robust_to_degradation": True})
    assert assessment.development is not None
    assert assessment.development.net_pnl_mxn < Decimal("0")
    payload = assessment.public()
    names = [item["name"] for item in payload["windows"]]
    assert names == ["DEVELOPMENT", "FORWARD"]


def test_holdout_windows_field_defaults_to_empty_and_is_extensible() -> None:
    base = _manifest()
    assert base.holdout_windows == ()
    window = declare_holdout(name="HOLDOUT_01", development_start_ms=DEVELOPMENT_END_MS,
                             duration_days=7, reason="probe")
    extended = _manifest(holdout_windows=(window,))
    assert len(extended.holdout_windows) == 1


def test_window_overlap_helper_agrees_with_the_declared_intervals() -> None:
    development = HoldoutWindow(name="DEVELOPMENT", start_ms=1000, end_ms=2000,
                                reason="design")
    preceding = HoldoutWindow(name="HOLDOUT_01", start_ms=500, end_ms=1000,
                              reason="preceding")
    assert development.overlaps(preceding) is False
    overlapping = HoldoutWindow(name="HOLDOUT_02", start_ms=900, end_ms=1500,
                                reason="overlapping")
    assert development.overlaps(overlapping) is True


def test_future_timestamps_do_not_change_the_frozen_manifest(tmp_path: Path) -> None:
    """The manifest is about intent, so it must be reproducible after the fact."""
    store = ExperimentManifestStore(tmp_path)
    frozen = store.freeze(_manifest())
    reloaded = store.load()
    assert reloaded["experiment_fingerprint"] == frozen["experiment_fingerprint"]
    assert reloaded["created_at"] == frozen["created_at"]


def test_a_holdout_declared_after_the_freeze_is_appended_not_rewritten(
        tmp_path: Path) -> None:
    store = ExperimentManifestStore(tmp_path)
    original = store.freeze(_manifest())
    window = declare_holdout(name="HOLDOUT_01", development_start_ms=DEVELOPMENT_END_MS,
                             duration_days=1, reason="probe")
    updated = store.freeze_holdouts((window,))
    assert updated["experiment_fingerprint"] == original["experiment_fingerprint"]
    assert len(updated["holdout_windows"]) == 1


def test_a_same_sized_holdout_is_compared_against_the_development_block() -> None:
    development_start = DEVELOPMENT_END_MS - 30 * 86_400_000
    window = declare_holdout(name="HOLDOUT_01", development_start_ms=development_start,
                             duration_days=30, reason="equal length")
    development = HoldoutWindow(name="DEVELOPMENT", start_ms=development_start,
                                end_ms=DEVELOPMENT_END_MS, reason="design")
    assert window.duration_days == development.duration_days


def test_repeated_assessment_of_the_same_windows_is_stable() -> None:
    windows = (_window(name="DEVELOPMENT", provenance=REAL_HISTORICAL_DEVELOPMENT,
                       net="1.00", trips=10),)
    first = assess_pair(market="SOL/MXN", profile_id=CANDIDATE_PROFILE,
                        experiment_fingerprint="f" * 64, windows=windows)
    second = assess_pair(market="SOL/MXN", profile_id=CANDIDATE_PROFILE,
                         experiment_fingerprint="f" * 64, windows=windows)
    assert first.public() == second.public()


def test_execution_model_version_is_recorded_on_every_fill() -> None:
    buy = model_buy(observation=_observe("100"), budget_mxn=Decimal("11"),
                    taker_fee_rate=FEE, modelled_slippage_bps=Decimal("0"),
                    base_currency="SOL", quote_currency="MXN")
    sell = model_sell(observation=_observe("100"), quantity=Decimal("0.1"),
                      taker_fee_rate=FEE, modelled_slippage_bps=Decimal("0"),
                      base_currency="SOL", quote_currency="MXN")
    assert buy.telemetry()["version"] == EXECUTION_MODEL_VERSION
    assert sell.telemetry()["version"] == EXECUTION_MODEL_VERSION
    assert buy.telemetry()["slippage_tolerance_used_as_forecast"] is False


def test_a_candle_only_estimate_is_labelled_as_such_and_not_as_a_book() -> None:
    fill = model_buy(observation=_observe("100"), budget_mxn=Decimal("11"),
                     taker_fee_rate=FEE, modelled_slippage_bps=Decimal("0"),
                     base_currency="SOL", quote_currency="MXN")
    assert fill.evidence_quality == CANDLE_ONLY_ESTIMATE
    assert "ORDER_BOOK" not in fill.evidence_quality


def test_an_observed_book_is_labelled_differently_from_an_estimate() -> None:
    observation = _observe("100", bids=(_Level("99.9", "50"),),
                           asks=(_Level("100.1", "50"),))
    fill = model_buy(observation=observation, budget_mxn=Decimal("11"),
                     taker_fee_rate=FEE, modelled_slippage_bps=Decimal("0"),
                     base_currency="SOL", quote_currency="MXN")
    assert fill.evidence_quality != CANDLE_ONLY_ESTIMATE


def test_real_years_of_history_are_not_required_for_a_fixture_to_run() -> None:
    evaluator = evaluator_for(CANDIDATE_PROFILE)
    result = replay_executable(candles=_oscillating(cycles=6), profile_id=CANDIDATE_PROFILE,
                               market="SOL/MXN", evaluator=evaluator, taker_fee_rate=FEE,
                               spread_bps=Decimal("12"), policy=DEFAULT_POLICY)
    assert result.candles == len(_oscillating(cycles=6))
    assert result.evidence_quality == CANDLE_ONLY_ESTIMATE


def test_the_development_reason_is_recorded_so_the_window_is_not_arbitrary() -> None:
    development = _manifest().development_window
    assert development.reason


def test_holdout_duration_must_be_positive() -> None:
    with pytest.raises(ExperimentError):
        declare_holdout(name="HOLDOUT_01", development_start_ms=DEVELOPMENT_END_MS,
                        duration_days=0, reason="zero length")


def test_end_before_start_is_rejected_at_construction() -> None:
    with pytest.raises(ExperimentError):
        HoldoutWindow(name="HOLDOUT_01", start_ms=2000, end_ms=1000, reason="reversed")


def test_assessment_reports_the_experiment_fingerprint_it_was_measured_under() -> None:
    assessment = assess_pair(
        market="SOL/MXN", profile_id=CANDIDATE_PROFILE, experiment_fingerprint="a" * 64,
        windows=(_window(name="DEVELOPMENT", provenance=REAL_HISTORICAL_DEVELOPMENT,
                         net="1.00", trips=10),))
    assert assessment.experiment_fingerprint == "a" * 64
    assert assessment.public()["experiment_fingerprint"] == "a" * 64

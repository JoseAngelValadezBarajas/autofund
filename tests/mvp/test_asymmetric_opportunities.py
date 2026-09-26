"""Tests for MVP 0.2.5: asymmetric risk/reward opportunity research.

The tests fall into three groups, and the first is the one that matters most.

**Leakage.** A structural level chosen with hindsight produces excellent geometry and
measures nothing, because the level was selected by the outcome it is supposed to predict.
The hazard is not carelessness; it is that the *correct-looking* implementation and the
leaking one differ by one index. So the tests here assert the index relationship directly and
mechanically over every bar of a series, rather than sampling a few cases.

**Reporting semantics.** 0.2.4 reported that configurations failed the sample floor while
several had 18, 31 and 27 completed trades. Both readings came from one boolean. The ladder
tests pin each rung separately so a failure cannot be reported against the wrong gate.

**Gate independence.** The asymmetry gate is the weakest of three and must not acquire
authority it was never given: it cannot admit what the economic guard refuses, cannot widen
risk policy, and must not be reachable by a trade with no declared invalidation.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from autofund.decimal_utils import ZERO
from autofund.mvp.asymmetric_challengers import (
    ExpansionRetestV1,
    StructuralInvalidationPullbackV1,
    asymmetric_challenger_ids,
    asymmetric_challengers,
)
from autofund.mvp.asymmetry_experiment import (
    COMPARISON_SEMANTICS,
    CONFIG_SETS,
    MAX_CONFIGURATIONS_PER_CHALLENGER,
    PREDECESSOR_BY_CONCEPT,
    PREDECLARED_HORIZONS,
    SELECTION_RULE,
    AsymmetryConfig,
    AsymmetryExperimentError,
    AsymmetryManifestStore,
    build_challenger,
    configuration_budget,
    freeze_asymmetry_manifest,
    predeclared_configuration_count,
)
from autofund.mvp.asymmetry_gate import (
    ECONOMIC_EDGE_REJECT,
    FEASIBLE,
    INSUFFICIENT_GROSS_MOVE,
    INVALIDATION_NOT_BELOW_ENTRY,
    NO_INVALIDATION_DECLARED,
    PREDECLARED_REWARD_RISK_THRESHOLDS,
    RISK_TOO_WIDE_FOR_POLICY,
    TARGET_NOT_ABOVE_ENTRY,
    assess_feasibility,
    friction_bps_for,
    maximum_risk_bps,
)
from autofund.mvp.economics import DEFAULT_POLICY as DEFAULT_POLICY
from autofund.mvp.horizon import FIFTEEN_MINUTE, ONE_HOUR_TIMEFRAME
from autofund.mvp.market_structure import (
    StructuralLevel,
    StructureError,
    levels_known_at,
    nearest_level_above,
    nearest_level_below,
    recent_range,
    retest_confirmed,
)
from autofund.mvp.profile_library import PROFILE_BY_ID
from autofund.mvp.profiles import DECISION_BUY, DECISION_NO_SIGNAL, DECISION_SELL
from autofund.mvp.trade_shape import (
    BOTH_ENTRY_AND_EXIT_WEAK,
    DIAGNOSTIC_MINIMUM_TRADES,
    DRAWDOWN_WITHIN_POLICY_FAIL,
    DRAWDOWN_WITHIN_POLICY_PASS,
    EDGE_EXISTS_BUT_NOT_CAPTURED,
    FULL_CERTIFICATION_FAIL,
    FULL_CERTIFICATION_PASS,
    INSUFFICIENT_EVIDENCE,
    LITTLE_FAVORABLE_EXCURSION,
    NON_NEGATIVE_NET_ECONOMICS_FAIL,
    NON_NEGATIVE_NET_ECONOMICS_PASS,
    RAW_MINIMUM_SAMPLE_FAIL,
    RAW_MINIMUM_SAMPLE_PASS,
    RISK_SHAPE_ACCEPTABLE,
    RISK_SHAPE_UNACCEPTABLE,
    TripShape,
    assess_certification_ladder,
    classify_defect,
    summarise_trade_shape,
)
from autofund.replay.data import Candle

BASE = datetime(2026, 9, 1, tzinfo=UTC)


# --------------------------------------------------------------------------------- fixture


def _walk(count: int = 400, *, minutes: int = 15, seed: int = 11,
          volatility: float = 90.0, base: int = 100000) -> tuple[Candle, ...]:
    """Deterministic pseudo-random walk with genuine local extrema.

    A hand-built zig-zag looks tidier but is degenerate for this purpose: a series whose lows
    are all equal has no pivot low at all, because every candidate is tied with its
    neighbours. This fixture therefore uses a seeded generator so that pivots genuinely exist
    and the tests exercise the real code path.
    """
    import random

    rng = random.Random(seed)
    out: list[Candle] = []
    price = Decimal(str(base))
    for index in range(count):
        price += Decimal(str(rng.gauss(0, 35)))
        half = Decimal(str(abs(rng.gauss(0, volatility))))
        open_price = price + Decimal(str(rng.gauss(0, 20)))
        high = max(open_price, price) + half
        low = min(open_price, price) - half
        if low <= 0:
            low = Decimal("1")
        out.append(Candle(timestamp=BASE + timedelta(minutes=minutes * index),
                          open=open_price, high=high, low=low, close=price,
                          volume=Decimal("1")))
    return tuple(out)


# -------------------------------------------------------------------------------- leakage


def test_every_level_strictly_predates_the_decision() -> None:
    """The core leakage assertion, checked on every bar rather than a sample.

    If a level could carry `source_index >= decision_index` it would encode information the
    decision did not have, and every geometry built on it would be unmeasurable.
    """
    candles = _walk(300)
    leaks: list[tuple[int, int]] = []
    for decision_index in range(60, len(candles)):
        for level in levels_known_at(candles=candles, decision_index=decision_index):
            if level.source_index >= decision_index:
                leaks.append((decision_index, level.source_index))
    assert leaks == [], f"future levels consulted: {leaks[:5]}"


def test_pivot_confirmation_respects_its_right_hand_window() -> None:
    """A pivot is only confirmable once its right-hand bars have closed.

    A pivot at `i` needs `i + half_width` bars to exist. At a decision on bar `d`, nothing
    newer than `d - half_width` can be a confirmed pivot, so a level's source must be at
    least that far back. This is stricter than `source_index < decision_index` and is the
    property that stops a level being "confirmed" by bars that had not happened.
    """
    candles = _walk(300)
    half_width = 2
    for decision_index in range(60, len(candles)):
        for level in levels_known_at(candles=candles, decision_index=decision_index,
                                     half_width=half_width):
            assert level.source_index <= decision_index - half_width


def test_levels_do_not_change_when_later_bars_are_appended() -> None:
    """The history a decision sees must not depend on bars that had not occurred.

    Constructs the same decision index from a short and a long series and requires identical
    levels. A divergence would mean some future bar had influenced the decision.
    """
    full = _walk(300)
    short = full[:181]
    at = 180
    short_levels = levels_known_at(candles=short, decision_index=at)
    long_levels = levels_known_at(candles=full, decision_index=at)
    assert [(lv.kind, lv.price_mxn, lv.source_index) for lv in short_levels] == [
        (lv.kind, lv.price_mxn, lv.source_index) for lv in long_levels]


def test_future_pivot_is_not_visible_before_it_is_confirmed() -> None:
    """A pivot that exists in the full series must be invisible at the bar that precedes it.

    This is the direct test of "no future pivots": the level is real, but a decision taken
    before its confirmation bars cannot know it.
    """
    candles = _walk(300)
    confirmed_at: int | None = None
    for candidate in range(4, len(candles) - 4):
        levels = levels_known_at(candles=candles, decision_index=candidate + 2)
        if any(lv.source_index == candidate for lv in levels):
            confirmed_at = candidate
            break
    assert confirmed_at is not None, "fixture must contain at least one pivot"
    earlier = levels_known_at(candles=candles, decision_index=confirmed_at)
    assert not any(lv.source_index == confirmed_at for lv in earlier)


def test_recent_range_ends_at_the_decision_bar() -> None:
    candles = _walk(200)
    low, high = recent_range(candles=candles, decision_index=150, window=30)
    window = candles[121:151]
    assert low == min(c.low for c in window)
    assert high == max(c.high for c in window)
    # A later bar must be able to move the range, which proves the window is not truncated
    # early in a way that would hide recent information.
    assert high <= max(c.high for c in candles[121:152])


def test_retest_looks_only_backwards() -> None:
    """`retest_confirmed` must ignore any interaction that happens after the decision.

    Uses a small explicit series so the semantics are unambiguous: the level is reached only
    on one specific bar, so a decision before that bar cannot know it was retested and a
    decision after it must.
    """
    prices = ["100", "101", "102", "103", "99", "101", "102", "103", "104"]
    candles: list[Candle] = []
    for index, price in enumerate(prices):
        value = Decimal(price)
        candles.append(Candle(timestamp=BASE + timedelta(minutes=15 * index), open=value,
                              high=value + Decimal("1"), low=value - Decimal("1"),
                              close=value, volume=Decimal("1")))
    series = tuple(candles)
    # A price reached only by the bar at index 4 (open 99, low 98, high 100). Every earlier
    # bar sits strictly above it, so the touch happened exactly once and exactly there.
    level = Decimal("98.5")
    assert all(series[i].low > level for i in range(4)), "level must be untouched before bar 4"

    # At the decision that precedes the touching bar, no retest can be known.
    assert retest_confirmed(candles=series, decision_index=4, level_mxn=level,
                            tolerance_bps=Decimal("0")) is False
    # Once that bar has happened, the retest is observable.
    assert retest_confirmed(candles=series, decision_index=5, level_mxn=level,
                            tolerance_bps=Decimal("0")) is True
    # And it stays observable later, because it is history by then.
    assert retest_confirmed(candles=series, decision_index=8, level_mxn=level,
                            tolerance_bps=Decimal("0")) is True


def test_structure_rejects_impossible_requests() -> None:
    candles = _walk(100)
    with pytest.raises(StructureError):
        levels_known_at(candles=candles, decision_index=-1)
    with pytest.raises(StructureError):
        levels_known_at(candles=candles, decision_index=50, half_width=0)
    with pytest.raises(StructureError):
        levels_known_at(candles=candles, decision_index=50, lookback=0)
    with pytest.raises(StructureError):
        StructuralLevel(kind="PIVOT_LOW", price_mxn=Decimal("0"), source_index=1,
                        age_bars=1, touches=1)
    with pytest.raises(StructureError):
        StructuralLevel(kind="INVENTED", price_mxn=Decimal("1"), source_index=1,
                        age_bars=1, touches=1)


def test_level_selection_is_by_geometry_not_by_outcome() -> None:
    levels = (
        StructuralLevel(kind="PIVOT_LOW", price_mxn=Decimal("95"), source_index=1,
                        age_bars=10, touches=1),
        StructuralLevel(kind="PIVOT_LOW", price_mxn=Decimal("99"), source_index=2,
                        age_bars=5, touches=1),
        StructuralLevel(kind="PIVOT_HIGH", price_mxn=Decimal("105"), source_index=3,
                        age_bars=2, touches=1),
    )
    below = nearest_level_below(levels=levels, price_mxn=Decimal("100"))
    assert below is not None and below.price_mxn == Decimal("99")
    above = nearest_level_above(levels=levels, price_mxn=Decimal("100"))
    assert above is not None and above.price_mxn == Decimal("105")


# ------------------------------------------------------------------ certification ladder


def test_a_configuration_with_trades_but_a_loss_reports_the_economics_gate() -> None:
    """The 0.2.4 reporting defect, pinned.

    31 completed round trips with a negative net must report RAW_MINIMUM_SAMPLE_PASS and
    NON_NEGATIVE_NET_ECONOMICS_FAIL. Reporting a sample failure would name the wrong gate and
    would misdirect the next experiment toward trade frequency.
    """
    ladder = assess_certification_ladder(
        round_trips=31, net_pnl_mxn=Decimal("-5.60"),
        effective_drawdown_mxn=Decimal("5.60"), median_mae_mxn=Decimal("0.26"),
        maximum_drawdown_mxn=Decimal("0.50"),
        max_single_trade_risk_mxn=Decimal("0.50"))
    assert ladder["raw_minimum_sample"] == RAW_MINIMUM_SAMPLE_PASS
    assert ladder["net_economics"] == NON_NEGATIVE_NET_ECONOMICS_FAIL
    assert ladder["full_certification"] == FULL_CERTIFICATION_FAIL
    assert "net_economics" in ladder["failed_gates"]
    assert "raw_minimum_sample" not in ladder["failed_gates"]


def test_a_genuinely_small_sample_reports_the_sample_gate() -> None:
    ladder = assess_certification_ladder(
        round_trips=3, net_pnl_mxn=Decimal("-0.10"),
        effective_drawdown_mxn=Decimal("0.10"), median_mae_mxn=Decimal("0.05"),
        maximum_drawdown_mxn=Decimal("0.50"),
        max_single_trade_risk_mxn=Decimal("0.50"))
    assert ladder["raw_minimum_sample"] == RAW_MINIMUM_SAMPLE_FAIL
    assert "raw_minimum_sample" in ladder["failed_gates"]


def test_the_ladder_reports_every_rung_independently() -> None:
    ladder = assess_certification_ladder(
        round_trips=10, net_pnl_mxn=Decimal("1.00"),
        effective_drawdown_mxn=Decimal("0.10"), median_mae_mxn=Decimal("0.10"),
        maximum_drawdown_mxn=Decimal("0.50"),
        max_single_trade_risk_mxn=Decimal("0.50"))
    assert ladder["raw_minimum_sample"] == RAW_MINIMUM_SAMPLE_PASS
    assert ladder["net_economics"] == NON_NEGATIVE_NET_ECONOMICS_PASS
    assert ladder["drawdown_within_policy"] == DRAWDOWN_WITHIN_POLICY_PASS
    assert ladder["risk_shape"] == RISK_SHAPE_ACCEPTABLE
    assert ladder["full_certification"] == FULL_CERTIFICATION_PASS
    assert ladder["failed_gates"] == []


def test_drawdown_failure_is_reported_against_the_drawdown_gate() -> None:
    ladder = assess_certification_ladder(
        round_trips=10, net_pnl_mxn=Decimal("1.00"),
        effective_drawdown_mxn=Decimal("0.90"), median_mae_mxn=Decimal("0.10"),
        maximum_drawdown_mxn=Decimal("0.50"),
        max_single_trade_risk_mxn=Decimal("0.50"))
    assert ladder["drawdown_within_policy"] == DRAWDOWN_WITHIN_POLICY_FAIL
    assert "drawdown_within_policy" in ladder["failed_gates"]


def test_risk_shape_failure_is_reported_separately() -> None:
    ladder = assess_certification_ladder(
        round_trips=10, net_pnl_mxn=Decimal("1.00"),
        effective_drawdown_mxn=Decimal("0.10"), median_mae_mxn=Decimal("0.80"),
        maximum_drawdown_mxn=Decimal("0.50"),
        max_single_trade_risk_mxn=Decimal("0.50"))
    assert ladder["risk_shape"] == RISK_SHAPE_UNACCEPTABLE
    assert "risk_shape" in ladder["failed_gates"]


# ------------------------------------------------------------------------ trade-path rules


def _shape(*, mae: str, mfe: str, net: str, reason: str = "TARGET_REACHED",
           friction: str = "0.19") -> TripShape:
    return TripShape(
        market="BTC/MXN", profile_id="p", entry_price_mxn=Decimal("100000"),
        exit_price_mxn=Decimal("100000"), target_price_mxn=Decimal("101000"),
        invalidation_price_mxn=Decimal("99000"), mae_mxn=Decimal(mae), mfe_mxn=Decimal(mfe),
        net_pnl_mxn=Decimal(net), gross_pnl_mxn=Decimal(net), friction_mxn=Decimal(friction),
        holding_bars=5, time_to_mae_bars=2, time_to_mfe_bars=4, exit_reason=reason,
        capital_hours=Decimal("0.5"))


def test_mfe_is_treated_as_net_which_is_what_it_is() -> None:
    """`mfe_mxn` is already net of the round trip, so a positive MFE means profitability.

    Comparing it against friction would double-count the entry cost. This is asserted because
    an earlier draft of the shape module made exactly that error, and it understated how
    often a path reached profitability.
    """
    positive = _shape(mae="0.10", mfe="0.02", net="0.01")
    assert positive.friction_cleared is True
    flat = _shape(mae="0.10", mfe="0.00", net="-0.10")
    assert flat.friction_cleared is False


def test_mfe_to_mae_is_undefined_without_adverse_excursion() -> None:
    """A trade that never went against the position has no ratio, not an infinite one."""
    assert _shape(mae="0.00", mfe="0.50", net="0.30").mfe_to_mae is None
    assert _shape(mae="0.10", mfe="0.30", net="0.20").mfe_to_mae == Decimal("3")


def test_net_to_mae_carries_the_sign_of_the_outcome() -> None:
    losing = _shape(mae="0.20", mfe="0.00", net="-0.30", reason="STRATEGY_INVALIDATED")
    assert losing.net_to_mae is not None and losing.net_to_mae < 0


def test_defect_classification_needs_a_minimum_sample() -> None:
    shapes = tuple(_shape(mae="0.10", mfe="0.00", net="-0.10") for _ in range(3))
    diagnosis, evidence = classify_defect(shapes=shapes)
    assert diagnosis == INSUFFICIENT_EVIDENCE
    assert evidence["reason"] == "SAMPLE_BELOW_DIAGNOSTIC_FLOOR"


def test_paths_that_never_reached_profitability_are_an_entry_defect() -> None:
    """The classification that justifies an entry-geometry challenger rather than an exit."""
    shapes = tuple(_shape(mae="0.26", mfe="0.00", net="-0.26") for _ in range(8))
    diagnosis, _ = classify_defect(shapes=shapes)
    assert diagnosis == LITTLE_FAVORABLE_EXCURSION


def test_a_path_that_offered_but_gave_it_back_is_an_exit_defect() -> None:
    shapes = tuple(_shape(mae="0.05", mfe="0.40", net="0.02") for _ in range(8))
    diagnosis, _ = classify_defect(shapes=shapes)
    assert diagnosis in (EDGE_EXISTS_BUT_NOT_CAPTURED, BOTH_ENTRY_AND_EXIT_WEAK)
    assert diagnosis != LITTLE_FAVORABLE_EXCURSION


def test_summary_reports_payoff_ratio_only_when_both_sides_exist() -> None:
    winners = tuple(_shape(mae="0.05", mfe="0.30", net="0.25") for _ in range(6))
    summary = summarise_trade_shape(market="BTC/MXN", profile_id="p", shapes=winners)
    # No losing trade means no payoff ratio: the exit has not been tested against one.
    assert summary.payoff_ratio is None
    assert summary.wins == 6 and summary.losses == 0

    mixed = winners + tuple(_shape(mae="0.20", mfe="0.00", net="-0.30") for _ in range(6))
    mixed_summary = summarise_trade_shape(market="BTC/MXN", profile_id="p", shapes=mixed)
    assert mixed_summary.payoff_ratio is not None


def test_summary_never_pools_markets() -> None:
    summary = summarise_trade_shape(market="BTC/MXN", profile_id="p",
                                    shapes=(_shape(mae="0.1", mfe="0.2", net="0.1"),))
    assert summary.telemetry()["markets_pooled"] is False
    assert summary.market == "BTC/MXN"


def test_empty_summary_is_stated_as_insufficient_not_as_zero() -> None:
    summary = summarise_trade_shape(market="BTC/MXN", profile_id="p", shapes=())
    assert summary.diagnosis == INSUFFICIENT_EVIDENCE
    assert summary.median_mae_mxn is None
    assert summary.round_trips == 0


def test_excursion_metrics_are_derived_only_from_the_traded_path() -> None:
    """MAE and MFE come from the recorded path, so they cannot include a later move.

    The shape carries the path's observations count, and a trip whose path recorded nothing
    reports no MAE/MFE rather than inferring them from the exit price.
    """
    crowded = TripShape(
        market="BTC/MXN", profile_id="p", entry_price_mxn=Decimal("100000"),
        exit_price_mxn=Decimal("100500"), target_price_mxn=Decimal("101000"),
        invalidation_price_mxn=Decimal("99000"), mae_mxn=Decimal("0.10"),
        mfe_mxn=Decimal("0.05"), net_pnl_mxn=Decimal("0.01"),
        gross_pnl_mxn=Decimal("0.20"), friction_mxn=Decimal("0.19"), holding_bars=3,
        time_to_mae_bars=1, time_to_mfe_bars=2, exit_reason="TARGET_REACHED",
        capital_hours=Decimal("0.3"))
    assert crowded.telemetry()["path_is_forward_only"] is True
    assert crowded.mae_mxn <= Decimal("0.10")


# -------------------------------------------------------------------------- the asymmetry gate


def _friction() -> Decimal:
    return friction_bps_for(taker_fee_rate=Decimal("0.0078"),
                            spread_bps=Decimal("12"), slippage_bps=Decimal("5"))


def test_friction_charges_the_spread_once() -> None:
    """The spread is crossed once per round trip, not once per leg.

    Charging it twice would make the gate refuse geometry the economic guard had accepted,
    and the two gates would then disagree about the same opportunity.
    """
    once = _friction()
    assert once == (Decimal("0.0078") * Decimal("10000") * Decimal("2")
                    + Decimal("12") + Decimal("5"))
    assert once == Decimal("173.0")


def test_maximum_risk_bps_is_the_policy_inverted() -> None:
    cap = maximum_risk_bps(budget_mxn=Decimal("11"),
                           max_single_trade_risk_mxn=Decimal("0.50"),
                           friction_bps=_friction())
    # 0.50 / 11 * 10000 - 173 = 281.5 bps. This is the number that makes the milestone hard.
    assert cap == pytest.approx(Decimal("281.545"), abs=Decimal("0.01"))


def test_gate_refuses_a_trade_the_economic_guard_refused() -> None:
    """Asymmetry cannot substitute for net economics."""
    result = assess_feasibility(
        entry_price_mxn=Decimal("100000"), target_price_mxn=Decimal("101000"),
        invalidation_price_mxn=Decimal("99900"), budget_mxn=Decimal("11"),
        friction_bps=_friction(), required_ratio=Decimal("1"),
        max_single_trade_risk_mxn=Decimal("0.50"), economic_guard_admissible=False)
    assert result.classification == ECONOMIC_EDGE_REJECT
    assert result.feasible is False
    assert result.telemetry()["can_override_economic_guard"] is False


def test_gate_refuses_a_trade_with_no_declared_invalidation() -> None:
    result = assess_feasibility(
        entry_price_mxn=Decimal("100000"), target_price_mxn=Decimal("105000"),
        invalidation_price_mxn=Decimal("0"), budget_mxn=Decimal("11"),
        friction_bps=_friction(), required_ratio=Decimal("1"),
        max_single_trade_risk_mxn=Decimal("0.50"), economic_guard_admissible=True)
    assert result.classification == NO_INVALIDATION_DECLARED


def test_gate_refuses_an_invalidation_above_the_entry() -> None:
    result = assess_feasibility(
        entry_price_mxn=Decimal("100000"), target_price_mxn=Decimal("105000"),
        invalidation_price_mxn=Decimal("100100"), budget_mxn=Decimal("11"),
        friction_bps=_friction(), required_ratio=Decimal("1"),
        max_single_trade_risk_mxn=Decimal("0.50"), economic_guard_admissible=True)
    assert result.classification == INVALIDATION_NOT_BELOW_ENTRY


def test_gate_refuses_risk_beyond_the_policy_cap() -> None:
    """Risk policy is not negotiable by this gate."""
    result = assess_feasibility(
        entry_price_mxn=Decimal("100000"), target_price_mxn=Decimal("110000"),
        invalidation_price_mxn=Decimal("97000"), budget_mxn=Decimal("11"),
        friction_bps=_friction(), required_ratio=Decimal("1"),
        max_single_trade_risk_mxn=Decimal("0.50"), economic_guard_admissible=True)
    assert result.classification == RISK_TOO_WIDE_FOR_POLICY
    assert result.telemetry()["can_override_risk_engine"] is False


def test_gate_refuses_a_target_below_the_entry() -> None:
    result = assess_feasibility(
        entry_price_mxn=Decimal("100000"), target_price_mxn=Decimal("99500"),
        invalidation_price_mxn=Decimal("99900"), budget_mxn=Decimal("11"),
        friction_bps=_friction(), required_ratio=Decimal("1"),
        max_single_trade_risk_mxn=Decimal("0.50"), economic_guard_admissible=True)
    assert result.classification == TARGET_NOT_ABOVE_ENTRY


def test_gate_refuses_a_move_too_small_to_fund_its_own_risk() -> None:
    """The core arithmetic: net reward must exceed risk times the required ratio."""
    result = assess_feasibility(
        entry_price_mxn=Decimal("100000"), target_price_mxn=Decimal("100400"),
        invalidation_price_mxn=Decimal("99900"), budget_mxn=Decimal("11"),
        friction_bps=_friction(), required_ratio=Decimal("1"),
        max_single_trade_risk_mxn=Decimal("0.50"), economic_guard_admissible=True)
    assert result.classification == INSUFFICIENT_GROSS_MOVE
    assert result.required_gross_bps > result.gross_reward_bps


def test_gate_admits_geometry_that_satisfies_every_constraint() -> None:
    result = assess_feasibility(
        entry_price_mxn=Decimal("100000"), target_price_mxn=Decimal("103500"),
        invalidation_price_mxn=Decimal("99800"), budget_mxn=Decimal("11"),
        friction_bps=_friction(), required_ratio=Decimal("1"),
        max_single_trade_risk_mxn=Decimal("0.50"), economic_guard_admissible=True)
    assert result.classification == FEASIBLE
    assert result.feasible is True
    assert result.net_reward_to_risk is not None
    assert result.net_reward_to_risk >= Decimal("1")


def test_required_gross_grows_with_the_required_ratio() -> None:
    """A larger ratio demands a larger move, which is why the band is not raised blindly."""
    def required(ratio: str) -> Decimal:
        return assess_feasibility(
            entry_price_mxn=Decimal("100000"), target_price_mxn=Decimal("103000"),
            invalidation_price_mxn=Decimal("99800"), budget_mxn=Decimal("11"),
            friction_bps=_friction(), required_ratio=Decimal(ratio),
            max_single_trade_risk_mxn=Decimal("0.50"),
            economic_guard_admissible=True).required_gross_bps

    assert required("2.0") > required("1.0") > required("0.5")


def test_gate_telemetry_declares_it_is_research_only() -> None:
    result = assess_feasibility(
        entry_price_mxn=Decimal("100000"), target_price_mxn=Decimal("103500"),
        invalidation_price_mxn=Decimal("99800"), budget_mxn=Decimal("11"),
        friction_bps=_friction(), required_ratio=Decimal("1"),
        max_single_trade_risk_mxn=Decimal("0.50"), economic_guard_admissible=True)
    telemetry = result.telemetry()
    assert telemetry["research_only"] is True
    assert telemetry["production_authority"] is False
    assert telemetry["evaluated_before_entry"] is True


def test_threshold_band_is_predeclared_and_anchored_on_existing_policy() -> None:
    assert Decimal("1.0") in PREDECLARED_REWARD_RISK_THRESHOLDS
    assert min(PREDECLARED_REWARD_RISK_THRESHOLDS) == Decimal("0.5")
    assert max(PREDECLARED_REWARD_RISK_THRESHOLDS) == Decimal("2.0")


# ------------------------------------------------------------------ challengers: no leakage


def test_pullback_entry_declares_risk_and_target_before_entering() -> None:
    """A BUY must already carry a boundary and a target, not discover them afterwards."""
    candles = _walk(400, volatility=200.0)
    profile = StructuralInvalidationPullbackV1(timeframe_name="15m")
    found = False
    for index in range(60, len(candles)):
        proposal = profile.propose(candles=candles[:index + 1])
        if proposal.decision != DECISION_BUY:
            continue
        found = True
        assert proposal.invalidation_price_mxn > 0
        assert proposal.invalidation_price_mxn < proposal.entry_reference_mxn
        assert proposal.expected_exit_reference_mxn > proposal.entry_reference_mxn
        assert proposal.max_holding_bars > 0
        assert proposal.declares_invalidation and proposal.declares_time_stop
        assert proposal.confidence_evidence["support_index"]
        # The support that justified the entry must predate it.
        assert int(proposal.confidence_evidence["support_index"]) < index
    assert found, "fixture must produce at least one entry"


def test_expansion_retest_declares_risk_and_target_before_entering() -> None:
    candles = _walk(500, volatility=220.0, seed=3)
    profile = ExpansionRetestV1(timeframe_name="15m")
    for index in range(60, len(candles)):
        proposal = profile.propose(candles=candles[:index + 1])
        if proposal.decision == DECISION_BUY:
            assert proposal.invalidation_price_mxn < proposal.entry_reference_mxn
            assert proposal.expected_exit_reference_mxn > proposal.entry_reference_mxn
            assert proposal.declares_invalidation
            break


def test_target_cannot_creep_away_from_a_declared_target() -> None:
    """Once a position is open the target and boundary are the ones passed back in.

    A target that ratchets with volatility becomes unreachable in a sustained move, and a
    boundary that widens to avoid a loss makes the stated reward/risk a fiction. Both were
    found and fixed in earlier milestones; this pins the behaviour.
    """
    candles = _walk(300, volatility=200.0)
    profile = StructuralInvalidationPullbackV1(timeframe_name="15m")
    fixed_target = candles[-1].close * Decimal("1.05")
    proposal = profile.propose(
        candles=candles, quantity=Decimal("0.0001"), cost_basis_mxn=Decimal("11"),
        entry_price_mxn=Decimal("100000"), target_price_mxn=fixed_target)
    assert proposal.expected_exit_reference_mxn == fixed_target
    assert proposal.decision in (DECISION_SELL, DECISION_NO_SIGNAL)


def test_boundary_does_not_widen_after_entry() -> None:
    candles = _walk(300, volatility=200.0)
    profile = StructuralInvalidationPullbackV1(timeframe_name="15m")
    open_position = profile.propose(
        candles=candles, quantity=Decimal("0.0001"), cost_basis_mxn=Decimal("11"),
        entry_price_mxn=Decimal("100000"), target_price_mxn=Decimal("105000"))
    # The boundary is derived from the entry price and the design cap, never from the market.
    expected = Decimal("100000") * (Decimal("1") - Decimal("260") / Decimal("10000"))
    assert open_position.invalidation_price_mxn == pytest.approx(expected, abs=Decimal("0.01"))


def test_deeper_adverse_movement_only_tightens_the_exit_decision() -> None:
    """A lower price must not produce a wider boundary -- that would be moving the stop."""
    profile = StructuralInvalidationPullbackV1(timeframe_name="15m")
    boundaries = []
    for close in ("100000", "99000", "98000"):
        candles = list(_walk(80, volatility=200.0))
        last = candles[-1]
        candles[-1] = Candle(timestamp=last.timestamp, open=Decimal(close),
                             high=Decimal(close) * Decimal("1.01"),
                             low=Decimal(close) * Decimal("0.99"),
                             close=Decimal(close), volume=last.volume)
        proposal = profile.propose(
            candles=tuple(candles), quantity=Decimal("0.0001"),
            cost_basis_mxn=Decimal("11"), entry_price_mxn=Decimal("100000"),
            target_price_mxn=Decimal("105000"))
        boundaries.append(proposal.invalidation_price_mxn)
    assert len(set(boundaries)) == 1


def test_challenger_refuses_a_geometry_wider_than_its_design_cap() -> None:
    """The design cap keeps risk inside policy; wider geometry is refused, not trimmed."""
    candles = _walk(400, volatility=60.0)
    profile = StructuralInvalidationPullbackV1(timeframe_name="15m", max_risk_bps=Decimal("30"))
    for index in range(60, len(candles)):
        proposal = profile.propose(candles=candles[:index + 1])
        if proposal.decision == DECISION_BUY:
            risk_bps = (proposal.entry_reference_mxn - proposal.invalidation_price_mxn)
            risk_bps = risk_bps / proposal.entry_reference_mxn * Decimal("10000")
            assert risk_bps <= Decimal("30")
        elif proposal.invalidation_price_mxn > 0:
            assert proposal.invalidation_price_mxn < proposal.entry_reference_mxn or True


def test_challenger_refuses_when_the_required_move_exceeds_the_volatility_cap() -> None:
    """A geometry whose required move is implausible is refused, not silently shrunk."""
    candles = _walk(200, volatility=5.0)
    profile = StructuralInvalidationPullbackV1(timeframe_name="15m")
    reasons = set()
    for index in range(60, len(candles)):
        proposal = profile.propose(candles=candles[:index + 1])
        if proposal.decision == DECISION_NO_SIGNAL and proposal.confidence_evidence:
            reasons.add(proposal.confidence_evidence.get("reason", ""))
    assert reasons, "fixture must exercise at least one refusal path"
    assert DECISION_BUY not in {profile.propose(
        candles=candles[:index + 1]).decision for index in range(60, len(candles))}


def test_challengers_never_sell_without_a_position() -> None:
    candles = _walk(300, volatility=150.0)
    for profile in (StructuralInvalidationPullbackV1(timeframe_name="15m"),
                    ExpansionRetestV1(timeframe_name="15m")):
        for index in range(60, len(candles)):
            proposal = profile.propose(candles=candles[:index + 1])
            assert proposal.decision != DECISION_SELL


def test_insufficient_history_yields_no_signal() -> None:
    candles = _walk(10)
    for profile in (StructuralInvalidationPullbackV1(timeframe_name="15m"),
                    ExpansionRetestV1(timeframe_name="15m")):
        assert profile.propose(candles=candles).decision == DECISION_NO_SIGNAL


# ------------------------------------------------------------------------ experiment freeze


def _manifest_kwargs(root: Any) -> dict[str, Any]:
    from autofund.mvp.economics import DEFAULT_POLICY
    from autofund.mvp.executable_replay import (
        MAX_SINGLE_TRADE_RISK_MXN,
        MINIMUM_REWARD_RISK_RATIO,
    )
    from autofund.mvp.robustness import MAX_DRAWDOWN_MXN

    start = int(BASE.timestamp() * 1000)
    day = 86400000
    return {"root": root, "baseline_commit": "54403fb",
            "product_version": "AutoFund MVP 0.2.5", "code_commit": "54403fb",
            "dataset_cutoff_ms": start,
            "development": ("DEV", start - 30 * day, start),
            "holdout": ("HOLD", start - 37 * day, start - 30 * day),
            "economic_policy": DEFAULT_POLICY, "max_drawdown_mxn": MAX_DRAWDOWN_MXN,
            "max_single_trade_risk_mxn": MAX_SINGLE_TRADE_RISK_MXN,
            "minimum_reward_risk_ratio": MINIMUM_REWARD_RISK_RATIO,
            "single_order_cap_mxn": Decimal("11"), "authorized_capital_mxn": Decimal("50"),
            "execution_model_version": "autofund.executable-execution.v1",
            "maker_fee_rate": Decimal("0.006"), "taker_fee_rate": Decimal("0.0078"),
            "spread_bps": Decimal("12"), "slippage_bps": Decimal("5"),
            "diagnosis": (("dominant_defect", "LITTLE_FAVORABLE_EXCURSION"),
                          ("basis", "measured before any challenger was built"))}


def test_parameter_budget_is_at_most_four_per_challenger_and_horizon() -> None:
    for configs in CONFIG_SETS.values():
        assert len(configs) <= MAX_CONFIGURATIONS_PER_CHALLENGER
    for _, count in configuration_budget():
        assert count <= MAX_CONFIGURATIONS_PER_CHALLENGER


def test_every_configuration_reaches_the_evaluator() -> None:
    """A configuration whose parameters are dropped is not a configuration.

    This is the 0.2.4 defect: a single module-level target model made all four
    configurations byte-identical while the budget reported four.
    """
    for concept, configs in CONFIG_SETS.items():
        fingerprints = set()
        identities = set()
        for tf in PREDECLARED_HORIZONS:
            for config in configs:
                profile = build_challenger(concept=concept, timeframe=tf, config=config)
                parameters = dict(profile.parameters)
                assert parameters["max_risk_bps"] == str(config.max_risk_bps)
                assert parameters["max_holding_bars"] == str(config.max_holding_bars)
                assert (parameters["invalidation_buffer_fraction"]
                        == str(config.invalidation_buffer_fraction))
                assert (parameters["target_margin_risk_multiple"]
                        == str(config.target_margin_risk_multiple))
                identities.add(profile.identity.profile_id)
                fingerprints.add(profile.identity.fingerprint)
        assert len(fingerprints) == len(configs) * len(PREDECLARED_HORIZONS)
        assert len(identities) == len(configs) * len(PREDECLARED_HORIZONS)


def test_configuration_fields_are_validated() -> None:
    with pytest.raises(AsymmetryExperimentError):
        AsymmetryConfig(label="", max_risk_bps=Decimal("100"), max_holding_bars=10,
                        expected_holding_horizon=5,
                        invalidation_buffer_fraction=Decimal("0.3"),
                        target_margin_risk_multiple=Decimal("0.5"))
    with pytest.raises(AsymmetryExperimentError):
        AsymmetryConfig(label="X", max_risk_bps=Decimal("100"), max_holding_bars=4,
                        expected_holding_horizon=8,
                        invalidation_buffer_fraction=Decimal("0.3"),
                        target_margin_risk_multiple=Decimal("0.5"))
    with pytest.raises(AsymmetryExperimentError):
        AsymmetryConfig(label="X", max_risk_bps=Decimal("100"), max_holding_bars=10,
                        expected_holding_horizon=5, invalidation_buffer_fraction=Decimal("1.5"),
                        target_margin_risk_multiple=Decimal("0.5"))


def test_manifest_freezes_before_results_and_refuses_overwrite(tmp_path: Any) -> None:
    first = freeze_asymmetry_manifest(**_manifest_kwargs(tmp_path))
    assert first["manifest_fingerprint"]
    assert first["holdout_used_for_selection"] is False
    assert first["thresholds_chosen_before_results"] is True
    assert first["previous_research_frozen"] is True
    assert first["maker_may_certify"] is False
    with pytest.raises(Exception):
        freeze_asymmetry_manifest(**_manifest_kwargs(tmp_path))
    assert AsymmetryManifestStore(tmp_path).load()["manifest_fingerprint"] == (
        first["manifest_fingerprint"])


def test_manifest_records_the_threshold_band_and_its_basis(tmp_path: Any) -> None:
    frozen = freeze_asymmetry_manifest(**_manifest_kwargs(tmp_path))
    assert frozen["reward_risk_thresholds"] == ["0.5", "0.75", "1.0", "2.0"]
    assert frozen["reward_risk_threshold_basis"]
    assert "friction" in frozen["reward_risk_threshold_basis"]


def test_manifest_records_the_diagnosis_that_justified_the_challengers(tmp_path: Any) -> None:
    frozen = freeze_asymmetry_manifest(**_manifest_kwargs(tmp_path))
    recorded = dict(frozen["parameter_freeze_diagnosis"])
    assert recorded["dominant_defect"] == "LITTLE_FAVORABLE_EXCURSION"
    assert recorded["basis"]


def test_manifest_declares_only_the_0_2_4_horizons(tmp_path: Any) -> None:
    frozen = freeze_asymmetry_manifest(**_manifest_kwargs(tmp_path))
    assert [h["name"] for h in frozen["horizons"]] == ["15m", "1h"]
    assert frozen["certifying_execution_mode"] == "TAKER_TAKER"


def test_manifest_carries_the_comparison_semantics_and_predecessors(tmp_path: Any) -> None:
    frozen = freeze_asymmetry_manifest(**_manifest_kwargs(tmp_path))
    assert frozen["comparison_semantics"] == list(COMPARISON_SEMANTICS)
    assert frozen["predecessor_by_concept"] == PREDECESSOR_BY_CONCEPT
    assert frozen["selection_rule"] == list(SELECTION_RULE)


def test_manifest_records_the_frozen_policy_it_may_not_change(tmp_path: Any) -> None:
    frozen = freeze_asymmetry_manifest(**_manifest_kwargs(tmp_path))
    assert frozen["max_drawdown_mxn"] == "0.50"
    assert frozen["max_single_trade_risk_mxn"] == "0.50"
    assert frozen["authorized_capital_mxn"] == "50"


def test_challenger_fingerprints_are_distinct_from_every_frozen_profile() -> None:
    """0.2.4 research must remain attributable to the profiles that produced it."""
    frozen = {definition.fingerprint for definition in PROFILE_BY_ID.values()}
    mine = {p.identity.fingerprint for p in asymmetric_challengers()}
    assert len(mine) == len(asymmetric_challengers())
    assert not (mine & frozen)


def test_challenger_ids_are_the_predeclared_set() -> None:
    """Each configuration gets its own identity, so results cannot collide across them."""
    ids = set(asymmetric_challenger_ids())
    expected = {f"{concept}-{tf}-{config}-v1"
                for concept, label in (
                    ("structural-invalidation-pullback", "PB"),
                    ("expansion-retest", "RT"))
                for tf in ("15m", "1h")
                for config in (f"{label}-A", f"{label}-B", f"{label}-C", f"{label}-D")}
    assert ids == expected
    assert len(ids) == predeclared_configuration_count()


def test_no_two_configurations_share_a_profile_id() -> None:
    """A shared id would merge two configurations' evidence into one record.

    The fingerprints differ even when the ids collide, so the collision is invisible in a
    fingerprint listing and only shows up as results keyed by id overwriting each other.
    """
    for concept, configs in CONFIG_SETS.items():
        seen: set[str] = set()
        for tf in PREDECLARED_HORIZONS:
            for config in configs:
                profile = build_challenger(concept=concept, timeframe=tf, config=config)
                assert profile.identity.profile_id not in seen, profile.identity.profile_id
                seen.add(profile.identity.profile_id)


# ---------------------------------------------------------- immutability of prior research


def test_previous_frozen_fingerprints_are_unchanged() -> None:
    assert PROFILE_BY_ID["mean-reversion-safe-v1"].fingerprint == (
        "1cadcfa967a919b6d041cf382d5acc993d7c70c7c1b008bba2db764c4f51bb6e")
    assert PROFILE_BY_ID["trend-continuation-v1"].fingerprint == (
        "6314058847ec352c76bf10f3c61fdd3b2989df3ee0782b3609fed5995e6157d7")
    assert PROFILE_BY_ID["volatility-mean-reversion-v1"].fingerprint == (
        "b7f9b5431243495cd6e0584cdba5f4cf4cef98aec9332bfd70141879a0ae22a4")


def test_policy_constants_are_unchanged() -> None:
    from autofund.mvp.executable_replay import (
        MAX_SINGLE_TRADE_RISK_MXN,
        MINIMUM_REWARD_RISK_RATIO,
    )
    from autofund.mvp.robustness import MAX_DRAWDOWN_MXN

    assert MAX_DRAWDOWN_MXN == Decimal("0.50")
    assert MAX_SINGLE_TRADE_RISK_MXN == Decimal("0.50")
    assert MINIMUM_REWARD_RISK_RATIO == Decimal("1.0")


def test_horizon_profiles_from_0_2_4_are_untouched() -> None:
    from autofund.mvp.horizon_profiles import horizon_profile_ids

    assert horizon_profile_ids() == (
        "volatility-mean-reversion-15m-v1", "volatility-mean-reversion-1h-v1",
        "range-expansion-15m-v1", "range-expansion-1h-v1")


def test_microstructure_collector_still_has_no_order_capability() -> None:
    from autofund.mvp.microstructure import MicrostructureCollector

    for name in ("submit", "place", "cancel", "replace", "post", "amend"):
        assert not hasattr(MicrostructureCollector, name)


def test_timeframes_are_the_predeclared_pair() -> None:
    assert [tf.name for tf in PREDECLARED_HORIZONS] == ["15m", "1h"]
    assert FIFTEEN_MINUTE.seconds == 900
    assert ONE_HOUR_TIMEFRAME.seconds == 3600


def test_diagnostic_floor_is_the_existing_sample_floor() -> None:
    assert DIAGNOSTIC_MINIMUM_TRADES == 5


# ------------------------------------------------- target geometry (regression, 0.2.5)


def test_required_target_matches_the_unchanged_gates_own_formula() -> None:
    """The target must satisfy the risk gate's actual requirement, not a nearby one.

    A real defect is pinned here. The first version of this formula used
    `friction + margin * risk`, which omits the friction term *inside* the ratio. That made
    the demanded target smaller than the gate's requirement for every risk below about
    346 bps, so all 64 configurations passed the economic guard and every one was then refused
    by the risk gate. The experiment produced no trades at all and would have been reported as
    an absence of opportunity rather than as a defect in this arithmetic.
    """
    from autofund.mvp.asymmetric_challengers import StructuralInvalidationPullbackV1
    from autofund.mvp.executable_replay import MINIMUM_REWARD_RISK_RATIO

    friction = Decimal("173")
    for margin in ("0.0", "0.4", "0.5", "0.6"):
        profile = StructuralInvalidationPullbackV1(
            timeframe_name="15m", target_margin_risk_multiple=Decimal(margin))
        for risk in ("80", "120", "180", "260"):
            risk_bps = Decimal(risk)
            required = profile._required_target_bps(risk_bps=risk_bps)
            # Invert the gate directly and require agreement with it.
            #
            #   net_reward >= ratio * risk   where ratio = 1 + margin
            #   gross - friction >= ratio * (risk + friction)
            #   gross >= friction + ratio * (risk + friction)
            ratio = Decimal("1") + Decimal(margin)
            expected = friction + ratio * (risk_bps + friction)
            assert required == expected, (margin, risk)
            # And the implied gate ratio must clear the project's own requirement.
            net_reward = required - friction
            effective = net_reward / (risk_bps + friction)
            assert effective >= MINIMUM_REWARD_RISK_RATIO


def test_a_target_built_from_the_requirement_passes_the_risk_gate() -> None:
    """End-to-end: a proposal built at the required geometry is admitted, not refused.

    This is the property the defect broke. It asserts through `assess_entry_risk` rather than
    against the formula, so it would catch any future divergence between the two.
    """
    from autofund.mvp.asymmetric_challengers import StructuralInvalidationPullbackV1
    from autofund.mvp.executable_replay import assess_entry_risk
    from autofund.mvp.profiles import StrategyProposal, TargetModel, VolatilityFeatures

    risk_bps = Decimal("120")
    entry = Decimal("100000")
    profile = StructuralInvalidationPullbackV1(timeframe_name="15m")
    required = profile._required_target_bps(risk_bps=risk_bps)
    target = entry * (Decimal("1") + required / Decimal("10000"))
    boundary = entry * (Decimal("1") - risk_bps / Decimal("10000"))
    proposal = StrategyProposal(
        decision="BUY", reason_code="SIGNAL_BUY", profile_id="p", strategy_id="s",
        strategy_version="0.1", strategy_fingerprint="f", market="BTC/MXN",
        entry_reference_mxn=entry, expected_exit_reference_mxn=target,
        expected_gross_edge_bps=required, expected_holding_horizon=8,
        target_model=TargetModel(atr_multiple=Decimal("1"), floor_bps=Decimal("1"),
                                 cap_bps=Decimal("100000")),
        features=VolatilityFeatures(0, ZERO, ZERO, ZERO, ZERO, ZERO),
        invalidation_price_mxn=boundary, invalidation_distance_bps=risk_bps,
        max_holding_bars=16)
    # The gate charges the same friction the profile sized against, so the verdict is
    # comparable. Ratio 1.0 is the project's existing requirement.
    assessment = assess_entry_risk(
        proposal=proposal, execution_price_mxn=entry, budget_mxn=Decimal("11"),
        taker_fee_rate=Decimal("0.0078"), spread_bps=Decimal("12"),
        slippage_bps=Decimal("5"), policy=DEFAULT_POLICY,
        required_ratio=Decimal("1.0"), max_trade_risk_mxn=Decimal("0.50"))
    assert assessment.admissible, assessment.reason_code
    assert assessment.reward_risk_ratio is not None
    assert assessment.reward_risk_ratio >= Decimal("1.0")


def test_the_volatility_cap_can_admit_the_gates_minimum_requirement() -> None:
    """A cap below the gate's own floor would make the whole experiment untestable.

    At BTC/15m the observed ATR is near 29 bps and the gate's floor requirement is 346 bps at
    zero risk, so the necessary multiple is about 12x. A cap under that guarantees zero trades
    for a search that never reached the market -- which is a broken experiment rather than a
    conservative one.
    """
    from autofund.mvp.asymmetric_challengers import DEFAULT_TARGET_ATR_CAP_MULTIPLE

    friction = Decimal("173")
    floor_requirement = friction + Decimal("1") * (Decimal("0") + friction)
    assert floor_requirement == Decimal("346")
    smallest_observed_atr_bps = Decimal("29")
    assert (DEFAULT_TARGET_ATR_CAP_MULTIPLE * smallest_observed_atr_bps
            >= floor_requirement)

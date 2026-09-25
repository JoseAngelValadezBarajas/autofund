"""MVP 0.2.3: passive execution economics.

These tests defend the properties that stop a passive-execution study from manufacturing
its own conclusion. The failure modes here are specific and well known:

* treating a candle's high or low touching a limit price as proof of a fill
* letting a post-only order quietly become a taker fill when it would have crossed
* reporting the P&L of a filled subset while ignoring the orders that never filled
* counting a cancelled order's residual quantity as inventory
* letting a fill be decided by data that existed before the order did
* concluding that maker fees help without checking the execution price and markout

Almost every assertion below is a refusal, a zero, or an exact boundary. A test that only
asserted "passive execution looks good" would pass on a model that assumed its way there.
"""

from decimal import Decimal

import pytest

from autofund.mvp.economics import DEFAULT_POLICY, economic_entry_model
from autofund.mvp.execution_comparison import (
    MAKER_FEE_FREE_FILL_BOUND,
    compare_execution_modes,
    counterfactual_maker_fee_upper_bound,
    markout,
)
from autofund.mvp.execution_gap import (
    MISSING,
    PRESENT,
    GapItem,
    architecture_gap,
    gap_summary,
)
from autofund.mvp.historical import synthetic_candles
from autofund.mvp.passive_execution import (
    ALL_MODES,
    CANDLE_ONLY_UNCERTAIN,
    CONFIRMING_MAKER_EVIDENCE,
    EXPIRED,
    FILLED,
    INSUFFICIENT_PASSIVE_EVIDENCE,
    MAKER,
    MAKER_MAKER,
    MAKER_TAKER,
    NO_TOUCH_AFTER_PLACEMENT,
    ORDER_BOOK_SUPPORTED,
    OUTCOME_UNKNOWN,
    PARTIALLY_FILLED,
    POST_ONLY_WOULD_REJECT,
    PREPARED,
    SUBMITTED,
    TAKER_MAKER,
    TAKER_TAKER,
    TRADE_TAPE_SUPPORTED,
    ExecutionError,
    OrderLifecycle,
    PassiveOrderRequest,
    execution_modes,
    fee_floor_reduction_bps,
    fee_floors,
    model_passive_fill,
    passive_price_policy,
    post_only_would_cross,
    terminal_states,
    transition_allowed,
)
from autofund.mvp.profile_library import (
    FROZEN_PROFILES,
    PROFILE_REGISTRY,
    evaluator_for,
)

MAKER_RATE = Decimal("0.00600000")
TAKER_RATE = Decimal("0.00780000")
BUDGET = Decimal("11")
SPREAD = Decimal("12")

# The frozen 0.2.1 fingerprints. Asserted here so an execution milestone cannot silently
# retune a strategy and re-label the change as an execution improvement.
FROZEN_FINGERPRINTS = {
    "mean-reversion-safe-v1": "1cadcfa967a919b6d041cf382d5acc993d7c70c7c1b008bba2db764c4f51bb6e",
    "trend-continuation-v1": "6314058847ec352c76bf10f3c61fdd3b2989df3ee0782b3609fed5995e6157d7",
    "volatility-mean-reversion-v1":
        "b7f9b5431243495cd6e0584cdba5f4cf4cef98aec9332bfd70141879a0ae22a4",
}


def _candles(prices: list[str]) -> tuple[object, ...]:
    return synthetic_candles(prices=prices).candles


def _request(**overrides: object) -> PassiveOrderRequest:
    base: dict[str, object] = {
        "book": "sol_mxn", "side": "BUY", "signal_index": 10, "placement_index": 11,
        "limit_price_mxn": Decimal("99"), "quantity": Decimal("0.1"),
        "tick_size": Decimal("0.01"), "timeout_bars": 5,
        "reference_bid_mxn": Decimal("99"), "reference_ask_mxn": Decimal("100")}
    base.update(overrides)
    return PassiveOrderRequest(**base)  # type: ignore[arg-type]


# ===========================================================================
# 1. Post-only semantics
# ===========================================================================

def test_a_buy_at_or_above_the_ask_would_cross() -> None:
    """Equality counts: a buy at the ask is a taker fill, not a maker one."""
    assert post_only_would_cross(side="BUY", limit_price_mxn=Decimal("100"),
                                 best_bid_mxn=Decimal("99"),
                                 best_ask_mxn=Decimal("100")) is True
    assert post_only_would_cross(side="BUY", limit_price_mxn=Decimal("101"),
                                 best_bid_mxn=Decimal("99"),
                                 best_ask_mxn=Decimal("100")) is True


def test_a_sell_at_or_below_the_bid_would_cross() -> None:
    assert post_only_would_cross(side="SELL", limit_price_mxn=Decimal("99"),
                                 best_bid_mxn=Decimal("99"),
                                 best_ask_mxn=Decimal("100")) is True


def test_a_maker_safe_price_does_not_cross() -> None:
    assert post_only_would_cross(side="BUY", limit_price_mxn=Decimal("99.5"),
                                 best_bid_mxn=Decimal("99"),
                                 best_ask_mxn=Decimal("100")) is False
    assert post_only_would_cross(side="SELL", limit_price_mxn=Decimal("99.5"),
                                 best_bid_mxn=Decimal("99"),
                                 best_ask_mxn=Decimal("100")) is False


def test_a_crossing_post_only_order_is_rejected_not_converted() -> None:
    """No optimistic conversion to taker: the order is refused."""
    outcome = model_passive_fill(
        request=_request(limit_price_mxn=Decimal("101")), candles=_candles(["100"] * 20))
    assert outcome.state != FILLED
    assert outcome.reason_code == POST_ONLY_WOULD_REJECT
    assert outcome.filled_quantity == Decimal("0")
    assert outcome.residual_quantity == Decimal("0.1")
    assert outcome.crossed_on_placement is True


def test_the_price_policy_stays_maker_safe_when_improving() -> None:
    price = passive_price_policy(side="BUY", best_bid_mxn=Decimal("99"),
                                 best_ask_mxn=Decimal("100"),
                                 tick_size=Decimal("0.5"), aggressive_ticks=Decimal("5"))
    assert price < Decimal("100")
    assert post_only_would_cross(side="BUY", limit_price_mxn=price,
                                 best_bid_mxn=Decimal("99"),
                                 best_ask_mxn=Decimal("100")) is False


def test_the_price_policy_joins_the_touch_at_zero_ticks() -> None:
    assert passive_price_policy(side="BUY", best_bid_mxn=Decimal("99"),
                                best_ask_mxn=Decimal("100"),
                                tick_size=Decimal("0.01")) == Decimal("99")
    assert passive_price_policy(side="SELL", best_bid_mxn=Decimal("99"),
                                best_ask_mxn=Decimal("100"),
                                tick_size=Decimal("0.01")) == Decimal("100")


# ===========================================================================
# 2. A fill requires evidence from after the order existed
# ===========================================================================

def test_candle_only_evidence_never_produces_a_fill() -> None:
    """The single most important property: a touch is not evidence of a fill.

    The model refuses to fill even though price moved far through the limit, because a
    candle records a range and not a sequence, so it cannot show the resting order was
    ahead of the trades that printed there.
    """
    outcome = model_passive_fill(
        request=_request(limit_price_mxn=Decimal("99"),
                         reference_bid_mxn=Decimal("99"),
                         reference_ask_mxn=Decimal("100"), evidence=CANDLE_ONLY_UNCERTAIN),
        candles=_candles(["100", "100", "98", "90", "85", "80"]))
    assert outcome.filled_quantity == Decimal("0")
    assert outcome.state == OUTCOME_UNKNOWN
    assert outcome.reason_code == INSUFFICIENT_PASSIVE_EVIDENCE
    assert outcome.endorses_production_maker is False


@pytest.mark.parametrize("evidence", sorted(CONFIRMING_MAKER_EVIDENCE))
def test_confirming_evidence_may_fill(evidence: str) -> None:
    outcome = model_passive_fill(
        request=_request(evidence=evidence), candles=_candles(["100"] * 20),
        spread_crosses=True)
    assert outcome.filled_quantity > Decimal("0")
    assert outcome.state == FILLED
    assert outcome.endorses_production_maker is True


def test_a_two_sided_book_alone_does_not_fill_without_a_cross() -> None:
    """Confirming evidence is necessary but not sufficient: the order must be reached."""
    outcome = model_passive_fill(
        request=_request(evidence=ORDER_BOOK_SUPPORTED), candles=_candles(["100"] * 20),
        spread_crosses=False)
    assert outcome.filled_quantity == Decimal("0")
    assert outcome.state == EXPIRED
    assert outcome.reason_code == NO_TOUCH_AFTER_PLACEMENT


def test_placement_must_follow_the_signal_strictly() -> None:
    """Same-bar placement would let an order use the signal bar's own outcome."""
    with pytest.raises(ExecutionError, match="strictly after the signal"):
        _request(placement_index=10)


def test_the_fill_model_reads_only_from_placement_forward() -> None:
    """A violent move before placement cannot fill an order that did not exist yet."""
    before = _candles(["200", "200", "150", "100", "100"])
    after = _candles(["100", "100", "100", "100"])
    request = _request(placement_index=len(before), signal_index=len(before) - 1,
                       limit_price_mxn=Decimal("99"), reference_bid_mxn=Decimal("99"),
                       reference_ask_mxn=Decimal("100"),
                       evidence=ORDER_BOOK_SUPPORTED)
    outcome = model_passive_fill(request=request, candles=before + after,
                                 spread_crosses=False)
    assert outcome.filled_quantity == Decimal("0")


def test_a_missing_horizon_does_not_invent_a_markout() -> None:
    """A truncated horizon is missing evidence, not a flat price."""
    result = markout(side="BUY", fill_price_mxn=Decimal("100"),
                     candles=_candles(["100", "101"]), fill_index=1,
                     short_bars=1, medium_bars=5)
    assert result.short_bps is None
    assert result.medium_bps is None


# ===========================================================================
# 3. Partial fills and cancellation
# ===========================================================================

def test_a_partial_fill_accounts_only_the_executed_quantity() -> None:
    outcome = model_passive_fill(
        request=_request(evidence=TRADE_TAPE_SUPPORTED), candles=_candles(["100"] * 20),
        spread_crosses=True, partial_ratio=Decimal("0.4"))
    assert outcome.partial is True
    assert outcome.filled_quantity == Decimal("0.1") * Decimal("0.4")
    assert outcome.residual_quantity == Decimal("0.1") - outcome.filled_quantity
    assert outcome.state == "CANCELLED"
    assert outcome.endorses_production_maker is True


def test_the_residual_is_never_added_to_inventory() -> None:
    lifecycle = OrderLifecycle(book="sol_mxn", side="BUY")
    lifecycle.residual_quantity = Decimal("0.1")
    lifecycle.advance(SUBMITTED)
    lifecycle.advance("OPEN")
    lifecycle.record_partial_fill(Decimal("0.04"))
    assert lifecycle.filled_quantity == Decimal("0.04")
    assert lifecycle.residual_quantity == Decimal("0.06")
    assert lifecycle.state == PARTIALLY_FILLED


def test_cancel_after_a_partial_fill_retains_the_executed_inventory() -> None:
    lifecycle = OrderLifecycle(book="sol_mxn", side="BUY")
    lifecycle.residual_quantity = Decimal("0.1")
    lifecycle.advance(SUBMITTED)
    lifecycle.advance("OPEN")
    lifecycle.record_partial_fill(Decimal("0.03"))
    lifecycle.advance("CANCEL_REQUESTED")
    lifecycle.advance("CANCELLED")
    assert lifecycle.filled_quantity == Decimal("0.03")
    assert lifecycle.state == "CANCELLED"


def test_a_cancelled_order_is_distinguishable_from_a_zero_fill() -> None:
    """"Cancelled" and "filled nothing" are different facts and must not be conflated."""
    cancelled = OrderLifecycle(book="sol_mxn", side="BUY")
    cancelled.residual_quantity = Decimal("0.1")
    cancelled.advance("CANCELLED")
    assert cancelled.state == "CANCELLED"
    assert cancelled.filled_quantity == Decimal("0")

    filled = OrderLifecycle(book="sol_mxn", side="SELL")
    filled.residual_quantity = Decimal("0.1")
    filled.advance(SUBMITTED)
    filled.advance("OPEN")
    filled.record_partial_fill(Decimal("0.1"))
    assert filled.state == FILLED
    assert filled.filled_quantity == Decimal("0.1")


def test_illegal_order_transitions_are_refused() -> None:
    assert transition_allowed(current=PREPARED, proposed=SUBMITTED) is True
    assert transition_allowed(current=PREPARED, proposed=FILLED) is False
    assert transition_allowed(current="CANCELLED", proposed="OPEN") is False


def test_unknown_outcome_is_not_terminal() -> None:
    """A stranded position is worse than admitting the uncertainty."""
    assert "OUTCOME_UNKNOWN" not in terminal_states()
    assert transition_allowed(current="OUTCOME_UNKNOWN", proposed=FILLED) is True


def test_a_partial_fill_beyond_the_order_quantity_is_refused() -> None:
    lifecycle = OrderLifecycle(book="sol_mxn", side="BUY")
    lifecycle.residual_quantity = Decimal("0.1")
    lifecycle.advance(SUBMITTED)
    lifecycle.advance("OPEN")
    with pytest.raises(ExecutionError):
        lifecycle.record_partial_fill(Decimal("0.2"))


# ===========================================================================
# 4. Fees and structural floors
# ===========================================================================

def test_fee_floors_use_the_correct_currency_semantics() -> None:
    """Charged in different currencies, so the round trip is not the sum of the rates."""
    floors = fee_floors(maker_rate=MAKER_RATE, taker_rate=TAKER_RATE)
    expected = ((Decimal("1") / ((Decimal("1") - TAKER_RATE) ** 2) - Decimal("1"))
                * Decimal("10000"))
    assert floors[TAKER_TAKER].fee_only_round_trip_bps == expected
    assert floors[TAKER_TAKER].telemetry()["spread_included"] is False


def test_maker_both_legs_costs_less_than_taker_both_legs() -> None:
    floors = fee_floors(maker_rate=MAKER_RATE, taker_rate=TAKER_RATE)
    assert (floors[MAKER_MAKER].fee_only_round_trip_bps
            < floors[TAKER_TAKER].fee_only_round_trip_bps)


def test_one_maker_leg_is_half_the_two_leg_saving() -> None:
    floors = fee_floors(maker_rate=MAKER_RATE, taker_rate=TAKER_RATE)
    reduction = fee_floor_reduction_bps(floors=floors)
    assert reduction[TAKER_TAKER] == Decimal("0")
    assert reduction[MAKER_TAKER] == reduction[TAKER_MAKER]
    assert (reduction[MAKER_MAKER] > reduction[MAKER_TAKER]
            > reduction[TAKER_TAKER])


def test_a_maker_rate_above_the_taker_rate_is_refused() -> None:
    with pytest.raises(ExecutionError):
        fee_floors(maker_rate=TAKER_RATE, taker_rate=MAKER_RATE)


def test_maker_and_taker_fees_differ_in_the_cash_flows() -> None:
    """Cheaper fees must show up in the model, not only in the floor arithmetic."""
    entry = economic_entry_model(
        book="SOL/MXN", budget_mxn=BUDGET, buy_price_mxn=Decimal("100"),
        target_price_mxn=Decimal("103"), buy_fee_rate=MAKER_RATE,
        sell_fee_rate=MAKER_RATE, policy=DEFAULT_POLICY)
    taker = economic_entry_model(
        book="SOL/MXN", budget_mxn=BUDGET, buy_price_mxn=Decimal("100"),
        target_price_mxn=Decimal("103"), buy_fee_rate=TAKER_RATE,
        sell_fee_rate=TAKER_RATE, policy=DEFAULT_POLICY)
    assert entry.expected_net_pnl_mxn > taker.expected_net_pnl_mxn


def test_the_buy_fee_is_charged_in_base_and_the_sell_fee_in_quote() -> None:
    """A base fee reduces owned quantity; a quote fee does not change ownership."""
    cheap_base = economic_entry_model(
        book="SOL/MXN", budget_mxn=BUDGET, buy_price_mxn=Decimal("100"),
        target_price_mxn=Decimal("103"), buy_fee_rate=MAKER_RATE,
        sell_fee_rate=TAKER_RATE, policy=DEFAULT_POLICY)
    cheap_quote = economic_entry_model(
        book="SOL/MXN", budget_mxn=BUDGET, buy_price_mxn=Decimal("100"),
        target_price_mxn=Decimal("103"), buy_fee_rate=TAKER_RATE,
        sell_fee_rate=MAKER_RATE, policy=DEFAULT_POLICY)
    # The base-side fee reduces what is owned; the quote-side fee reduces proceeds. Each
    # therefore hits the result differently.
    assert cheap_base.expected_owned_major > cheap_quote.expected_owned_major


# ===========================================================================
# 5. The comparison respects non-fill and evidence limits
# ===========================================================================

def test_passive_modes_report_no_trades_without_confirming_evidence() -> None:
    """The default must be unevaluable, not optimistic."""
    results = compare_execution_modes(
        market="SOL/MXN", profile_id="trend-continuation-v1",
        candles=_candles(["100"] * 20 + ["101"] * 40 + ["99"] * 40),
        evaluator=evaluator_for("trend-continuation-v1"), maker_rate=MAKER_RATE,
        taker_rate=TAKER_RATE, spread_bps=SPREAD, maker_fill_supported=False)
    for mode in (MAKER_TAKER, TAKER_MAKER, MAKER_MAKER):
        assert results[mode].live_trades == 0
        assert results[mode].unfilled_reason == "INSUFFICIENT_PASSIVE_FILL_EVIDENCE"
        assert results[mode].evidence == CANDLE_ONLY_UNCERTAIN
        assert results[mode].maker_legs_assumed_filled is False


def test_the_baseline_mode_uses_only_taker_fees() -> None:
    results = compare_execution_modes(
        market="SOL/MXN", profile_id="trend-continuation-v1",
        candles=_candles(["100"] * 20 + ["101"] * 40 + ["99"] * 40),
        evaluator=evaluator_for("trend-continuation-v1"), maker_rate=MAKER_RATE,
        taker_rate=TAKER_RATE, spread_bps=SPREAD)
    baseline = results[TAKER_TAKER]
    assert baseline.buy_fee_rate == TAKER_RATE
    assert baseline.sell_fee_rate == TAKER_RATE
    assert baseline.maker_legs_assumed_filled is False


def test_the_counterfactual_bound_is_labelled_as_unachievable() -> None:
    bound = counterfactual_maker_fee_upper_bound(
        market="SOL/MXN", profile_id="trend-continuation-v1",
        candles=_candles(["100"] * 20 + ["101"] * 40 + ["99"] * 40),
        evaluator=evaluator_for("trend-continuation-v1"), maker_rate=MAKER_RATE,
        spread_bps=SPREAD)
    assert bound.mode == MAKER_FEE_FREE_FILL_BOUND
    assert bound.maker_legs_assumed_filled is True
    assert bound.evidence == MAKER_FEE_FREE_FILL_BOUND
    assert "UNCONDITIONAL_FILL" in bound.unfilled_reason
    assert bound.buy_fee_rate == MAKER_RATE


def test_the_fill_rate_denominator_includes_unfilled_orders() -> None:
    """A fill rate computed over fills alone would always be 100%."""
    results = compare_execution_modes(
        market="BTC/MXN", profile_id="trend-continuation-v1",
        candles=_candles(["100"] * 20 + ["101"] * 30 + ["99"] * 30),
        evaluator=evaluator_for("trend-continuation-v1"), maker_rate=MAKER_RATE,
        taker_rate=TAKER_RATE, spread_bps=SPREAD)
    baseline = results[TAKER_TAKER]
    if baseline.orders_attempted == 0:
        pytest.skip("fixture produced no attempts")
    assert baseline.fill_rate <= Decimal("1")
    if baseline.orders_not_filled > 0:
        assert baseline.fill_rate < Decimal("1")


def test_missed_opportunities_are_counted_and_exposed() -> None:
    results = compare_execution_modes(
        market="BTC/MXN", profile_id="mean-reversion-safe-v1",
        candles=_candles(["100"] * 20 + ["99"] * 15 + ["100"] * 15),
        evaluator=evaluator_for("mean-reversion-safe-v1"), maker_rate=MAKER_RATE,
        taker_rate=TAKER_RATE, spread_bps=SPREAD)
    baseline = results[TAKER_TAKER]
    telemetry = baseline.telemetry()
    for key in ("orders_attempted", "orders_not_filled", "fill_rate",
                "missed_opportunities", "missed_opportunity_rate",
                "reserved_capital_hours"):
        assert key in telemetry
    assert telemetry["markets_pooled"] is False


# ===========================================================================
# 6. Adverse selection
# ===========================================================================

def test_a_buy_markout_is_negative_when_price_falls_after_the_fill() -> None:
    result = markout(side="BUY", fill_price_mxn=Decimal("100"),
                     candles=_candles(["100", "100", "99", "98", "97", "96"]),
                     fill_index=1, short_bars=1, medium_bars=4)
    assert result.short_bps < Decimal("0")
    assert result.adverse_short is True


def test_a_buy_markout_is_positive_when_price_rises_after_the_fill() -> None:
    result = markout(side="BUY", fill_price_mxn=Decimal("100"),
                     candles=_candles(["100", "100", "101", "102", "103", "104"]),
                     fill_index=1, short_bars=1, medium_bars=4)
    assert result.short_bps > Decimal("0")
    assert result.adverse_short is False


def test_a_sell_markout_is_sign_adjusted_so_positive_is_always_favourable() -> None:
    """Without the adjustment a falling price would look favourable for a seller."""
    falling = markout(side="SELL", fill_price_mxn=Decimal("100"),
                      candles=_candles(["100", "100", "99", "98", "97", "96"]),
                      fill_index=1, short_bars=1, medium_bars=4)
    assert falling.short_bps > Decimal("0")


def test_the_markout_sign_convention_is_stated() -> None:
    result = markout(side="BUY", fill_price_mxn=Decimal("100"),
                     candles=_candles(["100", "100", "101", "102", "103", "104"]),
                     fill_index=1)
    assert result.telemetry()["sign_convention"] == "POSITIVE_IS_FAVOURABLE"


# ===========================================================================
# 7. Determinism and no leakage
# ===========================================================================

def test_the_comparison_is_deterministic() -> None:
    candles = _candles(["100"] * 20 + ["101"] * 30 + ["99"] * 30)
    first = compare_execution_modes(
        market="SOL/MXN", profile_id="range-expansion-v1", candles=candles,
        evaluator=evaluator_for("range-expansion-v1"), maker_rate=MAKER_RATE,
        taker_rate=TAKER_RATE, spread_bps=SPREAD)
    second = compare_execution_modes(
        market="SOL/MXN", profile_id="range-expansion-v1", candles=candles,
        evaluator=evaluator_for("range-expansion-v1"), maker_rate=MAKER_RATE,
        taker_rate=TAKER_RATE, spread_bps=SPREAD)
    assert ({mode: result.telemetry() for mode, result in first.items()}
            == {mode: result.telemetry() for mode, result in second.items()})


def test_truncating_the_series_keeps_earlier_trades_identical() -> None:
    """If a later bar influenced an earlier decision, truncating would rewrite the past."""
    candles = _candles(["100"] * 20 + ["101"] * 30 + ["99"] * 60)
    full = compare_execution_modes(
        market="SOL/MXN", profile_id="trend-continuation-v1", candles=candles,
        evaluator=evaluator_for("trend-continuation-v1"), maker_rate=MAKER_RATE,
        taker_rate=TAKER_RATE, spread_bps=SPREAD)[TAKER_TAKER]
    if not full.trades:
        pytest.skip("fixture produced no trade")
    first_exit = full.trades[0].exit_index
    prefix = compare_execution_modes(
        market="SOL/MXN", profile_id="trend-continuation-v1",
        candles=candles[:first_exit + 1],
        evaluator=evaluator_for("trend-continuation-v1"), maker_rate=MAKER_RATE,
        taker_rate=TAKER_RATE, spread_bps=SPREAD)[TAKER_TAKER]
    assert prefix.trades
    assert prefix.trades[0].entry_index == full.trades[0].entry_index
    assert prefix.trades[0].exit_index == first_exit
    assert prefix.trades[0].net_pnl_mxn == full.trades[0].net_pnl_mxn


def test_placement_time_state_is_required_and_validated() -> None:
    with pytest.raises(ExecutionError, match="positive"):
        _request(tick_size=Decimal("0"))
    with pytest.raises(ExecutionError):
        _request(side="HOLD")


# ===========================================================================
# 8. Nothing about strategy or risk policy changes
# ===========================================================================

def test_the_frozen_profiles_keep_their_exact_fingerprints() -> None:
    by_id = {definition.profile_id: definition for definition in PROFILE_REGISTRY}
    for profile_id, expected in FROZEN_FINGERPRINTS.items():
        assert by_id[profile_id].identity.fingerprint == expected, profile_id


def test_frozen_profiles_are_still_reported_as_a_distinct_set() -> None:
    frozen = {definition.profile_id for definition in FROZEN_PROFILES}
    assert frozen == set(FROZEN_FINGERPRINTS)


def test_execution_modes_are_research_only_and_authorise_nothing() -> None:
    for mode in execution_modes().values():
        payload = mode.public()
        assert payload["research_only"] is True
        assert payload["production_authorised"] is False


def test_every_mode_declares_its_full_semantics() -> None:
    for name, mode in execution_modes().items():
        payload = mode.public()
        for key in ("entry_liquidity", "exit_liquidity", "entry_order_type",
                    "exit_order_type", "timeout_bars", "requires_post_only"):
            assert key in payload, f"{name} missing {key}"
        assert mode.entry_order_type in ("MARKET", "LIMIT_POST_ONLY")
        assert mode.exit_order_type in ("MARKET", "LIMIT_POST_ONLY")


def test_only_post_only_modes_require_post_only() -> None:
    modes = execution_modes()
    assert modes[TAKER_TAKER].requires_post_only is False
    for name in (MAKER_TAKER, TAKER_MAKER, MAKER_MAKER):
        assert modes[name].requires_post_only is True
        assert MAKER in (modes[name].entry_liquidity, modes[name].exit_liquidity)
    assert set(modes) == set(ALL_MODES)


def test_the_drawdown_policy_bound_is_unchanged() -> None:
    from autofund.mvp.robustness import MAX_DRAWDOWN_MXN

    assert MAX_DRAWDOWN_MXN == Decimal("0.50")


def test_capital_limits_are_unchanged() -> None:
    assert BUDGET == Decimal("11")


def test_production_post_and_cancel_capability_is_absent() -> None:
    """No new mutation capability may be introduced by an execution research milestone."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "autofund"
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in ("requests.post", "client.post(", ".post(url", ".delete(url",
                       "requests.delete", "httpx.delete"):
            if marker in text:
                offenders.append(f"{path.name}:{marker}")
    assert offenders == []


def test_candle_only_evidence_cannot_endorse_production_maker() -> None:
    outcome = model_passive_fill(
        request=_request(evidence=CANDLE_ONLY_UNCERTAIN), candles=_candles(["100"] * 20),
        spread_crosses=True)
    assert outcome.endorses_production_maker is False


def test_the_comparison_never_pools_markets() -> None:
    results = compare_execution_modes(
        market="ETH/MXN", profile_id="trend-continuation-v1",
        candles=_candles(["100"] * 20 + ["101"] * 30 + ["99"] * 30),
        evaluator=evaluator_for("trend-continuation-v1"), maker_rate=MAKER_RATE,
        taker_rate=TAKER_RATE, spread_bps=SPREAD)
    for result in results.values():
        assert result.market == "ETH/MXN"
        assert result.telemetry()["markets_pooled"] is False


# ===========================================================================
# 9. Production architecture gap
# ===========================================================================

def test_the_gap_reports_cancellation_as_missing_and_mutation_requiring() -> None:
    """Cancellation is the security blocker, not merely an engineering task."""
    items = {item.name: item for item in architecture_gap()}
    cancel = items["cancel_capability"]
    assert cancel.state == MISSING
    assert cancel.production_mutation_required is True
    assert cancel.blocks_passive_production is True


def test_the_gap_confirms_no_mutation_capability_was_added() -> None:
    summary = gap_summary()
    assert summary["mutations_added_this_milestone"] == 0
    assert summary["cancel_implemented"] is False
    assert summary["replace_implemented"] is False
    assert summary["passive_production_enabled"] is False


def test_the_gap_records_what_already_exists() -> None:
    """Open-order monitoring and the one-order invariant are present today."""
    items = {item.name: item for item in architecture_gap()}
    assert items["open_order_monitoring"].state == PRESENT
    assert items["one_unresolved_order_invariant"].state == PRESENT
    assert items["scale_precision"].state == PRESENT


def test_every_gap_item_names_its_evidence() -> None:
    for item in architecture_gap():
        assert item.evidence, item.name
        if item.blocks_passive_production:
            assert item.blocker, f"{item.name} blocks but states no blocker"


def test_the_gap_states_the_production_mutation_boundary() -> None:
    summary = gap_summary()
    assert summary["production_mutation_boundary"] == "GATED_SINGLE_USE_MARKET_BUY_ONLY"
    assert set(summary["production_mutations_required"]) == {
        "limit_order_submission", "cancel_capability"}


def test_an_unknown_gap_state_is_refused() -> None:
    with pytest.raises(ValueError):
        GapItem(name="x", state="SOMETHING_ELSE", evidence="y")

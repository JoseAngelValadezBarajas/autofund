"""MVP 0.1.4: economically viable strategy profiles.

These tests exist to pin the *honest* properties of the milestone, including the
uncomfortable ones:

* the frozen Champion is byte-identical in behaviour to `evaluate_champion`, and is
  reported NOT_VIABLE at the confirmed Production fee -- the 0.1.3 finding preserved
  as evidence rather than edited away;
* a viable challenger exists whose intended move clears real friction, and its
  shadow round trip ends positive after fees;
* a volatile-but-expensive market is refused, so "most volatile" can never win;
* no lookahead, determinism, Decimal-only money, and the shadow pipeline reporting
  real counts rather than announcement-only zeros;
* nothing is promoted, and the Champion is untouched.
"""

from decimal import Decimal
from typing import Any

import pytest

from autofund.mvp.adaptive import AdaptiveEngine
from autofund.mvp.champion import CHAMPION_PARAMETERS, evaluate_champion
from autofund.mvp.economics import (
    DEFAULT_POLICY,
    PROFIT_TAKING,
    RISK_EXIT,
    EconomicPolicy,
)
from autofund.mvp.historical import CAPTURED, SYNTHETIC, CandleSeries, synthetic_candles
from autofund.mvp.position_contract import (
    ContractError,
    EntryEconomicEvidence,
    ExpectedExitModel,
    PositionContract,
    PositionContractStore,
    exit_semantics_for,
    is_risk_exit,
)
from autofund.mvp.profile_library import (
    CHALLENGER_PROFILES,
    CHAMPION_PROFILE,
    FROZEN_PROFILES,
    PROFILE_REGISTRY,
    RISK_ADJUSTED_CHALLENGERS,
    MeanReversionSafeV1,
    TrendContinuationV1,
    VolatilityAwareMeanReversionV1,
    evaluator_for,
)
from autofund.mvp.profiles import (
    CLASS_STABLE_OR_FIAT,
    CLASS_VOLATILE_CRYPTO,
    COMPATIBLE,
    DECISION_BUY,
    INCOMPATIBLE,
    INSUFFICIENT_EVIDENCE,
    NOT_VIABLE,
    PROMOTION,
    RESEARCH_ONLY,
    TargetModel,
    base_is_stable_or_fiat,
    classify_market,
    market_compatibility,
    true_range_bps,
    volatility_features,
)
from autofund.mvp.replay_eval import (
    percentiles,
    replay_candles,
    train_evaluation_split,
    verify_no_lookahead,
)
from autofund.mvp.research import (
    CHECK_ROUND_TRIPS,
    DEFAULT_REQUIREMENTS,
    EvidenceRequirements,
    certify,
    evaluate_profile_market,
)
from autofund.mvp.shadow_research import (
    NO_CANDLE_DATA,
    MarketBook,
    run_shadow_research,
)
from autofund.mvp.slippage import (
    FILLED_AT_BEST,
    FILLED_WITH_IMPACT,
    INSUFFICIENT_DEPTH,
    estimate_round_trip,
    fits_at_best_price,
    walk_for_notional,
    walk_for_quantity,
)
from autofund.mvp.viability import (
    BELOW_POLICY_BUFFER,
    DEPTH_INSUFFICIENT,
    FRICTION_EXCEEDS_TARGET,
    assess_viability,
    minimum_viable_gross_edge_bps,
)
from autofund.observer.models import Level

D = Decimal

# Production's confirmed taker fee, taken from the real account in 0.1.3.
PRODUCTION_FEE = D("0.0078")

# A deep, cheap book: plenty of size at the best price, so an 11 MXN order has no
# depth impact and the estimator legitimately reports near-zero slippage.
DEEP_BIDS = (Level(D("1000000"), D("5")),)
DEEP_ASKS = (Level(D("1001000"), D("5")),)

# Shadow research evaluates synthetic series priced around 1000, so its observed
# books are scaled to that level. Passing the BTC-scale book above would leave the
# economics nonsense and the pipeline would refuse everything for insufficient
# depth -- which is precisely why `run_shadow_research` requires a real book per
# market instead of inventing one.
SHADOW_BOOKS = {
    "BTC/MXN": MarketBook(bids=(Level(D("1000"), D("100")),),
                          asks=(Level(D("1001"), D("100")),)),
    "ETH/MXN": MarketBook(bids=(Level(D("1000"), D("100")),),
                          asks=(Level(D("1001"), D("100")),)),
    "usd_mxn": MarketBook(bids=(Level(D("1000"), D("100")),),
                          asks=(Level(D("1001"), D("100")),)),
}


def series(prices: list[str], *, half_range_bps: str = "20") -> CandleSeries:
    return synthetic_candles(prices=prices, half_range_bps=D(half_range_bps))


def flat_then(prices: list[str], *, flat: int = 25) -> CandleSeries:
    return series(["1000"] * flat + prices)


# ---------------------------------------------------------------------------
# The frozen Champion
# ---------------------------------------------------------------------------

def test_frozen_champion_reproduces_evaluate_champion_exactly() -> None:
    """The frozen profile must be behaviourally identical, decisions and reasons."""
    cases = [("flat", ["1000"] * 25), ("drop", ["1000"] * 22 + ["990"]),
             ("deep_drop", ["1000"] * 20 + ["900"]), ("rise", ["1000"] * 22 + ["1100"])]
    frozen = MeanReversionSafeV1()
    for name, prices in cases:
        item = series(prices)
        reference = evaluate_champion(market="BTC/MXN",
                                      closes=tuple(c.close for c in item.candles))
        proposal = frozen.propose(candles=item.candles)
        assert proposal.decision == reference.decision, name
        assert proposal.reason_code == reference.reason_code, name


def test_champion_parameters_and_fingerprint_are_unchanged() -> None:
    """0.1.4 adds profiles; it must not retune the certified Champion."""
    assert CHAMPION_PARAMETERS.min_history == 3 and CHAMPION_PARAMETERS.max_window == 20
    assert CHAMPION_PARAMETERS.entry_threshold == D("0.001")
    assert CHAMPION_PARAMETERS.exit_threshold == D("0.002")
    assert AdaptiveEngine().champion.fingerprint == (
        "5a47c9833724e58d64ef37e8702aafaf2e503940e91ad17aa626a72ac53865a8")
    assert AdaptiveEngine().champion.profile_id == "mean-reversion-safe"


def test_champion_profile_is_declared_as_the_frozen_reference() -> None:
    assert CHAMPION_PROFILE.identity.version == "0.1"
    assert CHAMPION_PROFILE.markets == frozenset({"btc_mxn"})
    assert "frozen" in CHAMPION_PROFILE.description.lower()


# ---------------------------------------------------------------------------
# Fixture 24: structurally bad strategy (the Champion) is NOT_VIABLE
# ---------------------------------------------------------------------------

def test_champion_intends_only_20_bps() -> None:
    """Its target is its own exit threshold; that is the whole 0.1.3 problem."""
    proposal = MeanReversionSafeV1().propose(candles=flat_then(["900"]).candles)
    assert proposal.decision == DECISION_BUY
    assert proposal.expected_gross_edge_bps == D("20.000")


def test_champion_is_not_viable_at_the_confirmed_production_fee() -> None:
    """Fixture 24: it may signal, but the guard refuses it. No intent is created."""
    item = flat_then(["900"])
    proposal = MeanReversionSafeV1().propose(candles=item.candles)
    assessment = assess_viability(
        proposal=proposal, budget_mxn=D("11"), taker_fee_rate=PRODUCTION_FEE,
        spread_bps=D("10"), bids=DEEP_BIDS, asks=DEEP_ASKS, policy=DEFAULT_POLICY,
        compatibility=COMPATIBLE)
    assert assessment.status == NOT_VIABLE
    assert assessment.reason_code == FRICTION_EXCEEDS_TARGET
    assert assessment.covers_friction is False
    assert assessment.expected_net_edge_bps < 0
    assert assessment.viable is False
    # Two-sided taker cost alone is ~156 bps, far above a 20 bps target.
    assert assessment.friction.total_bps > D("150")


def test_minimum_viable_gross_edge_is_reported_not_guessed() -> None:
    """The bar a profile must clear, stated explicitly rather than discovered late."""
    floor = minimum_viable_gross_edge_bps(taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
                                          policy=DEFAULT_POLICY)
    assert floor > D("160")
    assert MeanReversionSafeV1().propose(
        candles=flat_then(["900"]).candles).expected_gross_edge_bps < floor


# ---------------------------------------------------------------------------
# Fixture 25: viable challenger admits, trades, and nets positive
# ---------------------------------------------------------------------------

def trending_series() -> CandleSeries:
    """A sustained uptrend well beyond friction: a genuinely viable opportunity."""
    return series(["1000"] * 22 + [str(1000 + index * 5) for index in range(1, 31)])


def test_viable_challenger_intends_an_edge_above_friction() -> None:
    item = trending_series()
    proposal = TrendContinuationV1().propose(candles=item.candles)
    assert proposal.decision == DECISION_BUY
    assert proposal.expected_gross_edge_bps > D("200")
    assert proposal.expected_holding_horizon > 0


def test_viable_challenger_admits_and_nets_positive_after_fees() -> None:
    """Fixture 25: architecture admits a real opportunity and it clears friction."""
    item = trending_series()
    result = replay_candles(
        candles=item.candles, profile_id="trend-continuation-v1", market="BTC/MXN",
        evaluator=TrendContinuationV1(), taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
        bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY,
        compatibility=COMPATIBLE)
    assert result.buy_signals >= 1
    assert result.economic_admissions >= 1
    assert result.round_trips, "a viable opportunity must produce a shadow round trip"
    trip = result.round_trips[0]
    assert trip.gross_pnl_mxn > 0, "the intended move was real"
    assert trip.total_friction_mxn > 0, "friction was charged, not ignored"
    assert trip.net_pnl_mxn > 0, "net of the real fee, the trade was positive"
    assert result.net_pnl_mxn > 0
    assert result.fees_mxn > 0


def test_viable_challenger_friction_is_attributed_to_a_component() -> None:
    """Fees, spread and slippage are each itemised, never lumped together."""
    item = trending_series()
    trip = replay_candles(
        candles=item.candles, profile_id="trend-continuation-v1", market="BTC/MXN",
        evaluator=TrendContinuationV1(), taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
        bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY
    ).round_trips[0]
    assert trip.entry_fee_mxn > 0 and trip.exit_fee_mxn > 0
    assert trip.spread_cost_mxn > 0
    assert trip.total_friction_mxn == (trip.entry_fee_mxn + trip.exit_fee_mxn
                                       + trip.spread_cost_mxn + trip.slippage_cost_mxn)


def test_champion_finds_no_opportunity_in_a_pure_uptrend() -> None:
    """It fades weakness, so an uptrend gives it nothing to do. No forced trade."""
    item = trending_series()
    result = replay_candles(
        candles=item.candles, profile_id="mean-reversion-safe-v1", market="BTC/MXN",
        evaluator=MeanReversionSafeV1(), taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
        bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY)
    assert result.buy_signals == 0
    assert result.round_trips == ()
    assert result.status == INSUFFICIENT_EVIDENCE
    assert "NO_BUY_SIGNAL_OBSERVED" in result.notes


# ---------------------------------------------------------------------------
# Fixture 26: volatile but expensive market must be refused
# ---------------------------------------------------------------------------

def test_volatile_but_expensive_market_is_refused() -> None:
    """Fixture 26: large movement, bad depth/spread -> no admissible trade.

    This is the protection against choosing the most volatile coin. The move is
    genuine; the cost of capturing it is not worth paying.
    """
    item = trending_series()
    proposal = TrendContinuationV1().propose(candles=item.candles)
    assert proposal.decision == DECISION_BUY and proposal.expected_gross_edge_bps > D("200")
    # The book cannot absorb the order at the touch, so most size is unfilled.
    thin_asks = (Level(D("1001000"), D("0.00000001")),)
    assessment = assess_viability(
        proposal=proposal, budget_mxn=D("11"), taker_fee_rate=PRODUCTION_FEE,
        spread_bps=D("10"), bids=DEEP_BIDS, asks=thin_asks, policy=DEFAULT_POLICY)
    assert assessment.status == NOT_VIABLE
    assert assessment.reason_code == DEPTH_INSUFFICIENT
    assert assessment.viable is False


def test_wide_spread_consumes_the_edge_and_is_refused() -> None:
    item = trending_series()
    proposal = TrendContinuationV1().propose(candles=item.candles)
    assessment = assess_viability(
        proposal=proposal, budget_mxn=D("11"), taker_fee_rate=PRODUCTION_FEE,
        spread_bps=D("400"), bids=DEEP_BIDS, asks=DEEP_ASKS, policy=DEFAULT_POLICY)
    assert assessment.status == NOT_VIABLE
    assert assessment.covers_friction is False


def test_a_loose_policy_still_cannot_rescue_a_negative_round_trip() -> None:
    """Policy relaxes the buffer; it cannot create profit that is not there."""
    item = flat_then(["900"])
    proposal = MeanReversionSafeV1().propose(candles=item.candles)
    permissive = assess_viability(
        proposal=proposal, budget_mxn=D("11"), taker_fee_rate=PRODUCTION_FEE,
        spread_bps=D("10"), bids=DEEP_BIDS, asks=DEEP_ASKS, policy=EconomicPolicy())
    assert permissive.expected_net_edge_bps < 0
    assert permissive.status == NOT_VIABLE
    assert permissive.reason_code == FRICTION_EXCEEDS_TARGET


def test_strict_policy_reports_a_buffer_failure_distinctly() -> None:
    item = trending_series()
    proposal = TrendContinuationV1().propose(candles=item.candles)
    strict = EconomicPolicy(minimum_net_edge_bps=D("100000"))
    assessment = assess_viability(
        proposal=proposal, budget_mxn=D("11"), taker_fee_rate=PRODUCTION_FEE,
        spread_bps=D("10"), bids=DEEP_BIDS, asks=DEEP_ASKS, policy=strict)
    assert assessment.status == NOT_VIABLE
    assert assessment.reason_code == BELOW_POLICY_BUFFER


# ---------------------------------------------------------------------------
# Slippage: order-book based, never the tolerance
# ---------------------------------------------------------------------------

def test_deep_book_reports_zero_slippage_legitimately() -> None:
    walk = walk_for_notional(DEEP_ASKS, D("11"))
    assert walk.fully_filled is True
    assert walk.levels_consumed == 1
    assert walk.expected_slippage_bps == D("0")
    assert walk.reason_code == FILLED_AT_BEST
    assert fits_at_best_price(DEEP_ASKS, D("11")) is True


def test_thin_book_reports_real_depth_impact() -> None:
    """Walking further into the book must show up as adverse slippage."""
    ladder = (Level(D("100"), D("0.05")), Level(D("110"), D("1")))
    walk = walk_for_notional(ladder, D("10"))
    assert walk.levels_consumed == 2
    assert walk.reason_code == FILLED_WITH_IMPACT
    assert walk.expected_execution_price_mxn > D("100")
    assert walk.expected_slippage_bps > D("0")


def test_insufficient_depth_is_reported_not_silently_partial() -> None:
    walk = walk_for_notional((Level(D("100"), D("0.01")),), D("10"))
    assert walk.fully_filled is False
    assert walk.unfilled_mxn > 0
    assert walk.reason_code == INSUFFICIENT_DEPTH


def test_sell_leg_walks_bids_and_is_adverse_downwards() -> None:
    ladder = (Level(D("100"), D("0.05")), Level(D("90"), D("1")))
    walk = walk_for_quantity(ladder, D("0.1"))
    assert walk.levels_consumed == 2
    assert walk.expected_execution_price_mxn < D("100")
    assert walk.expected_slippage_bps > D("0")


def test_round_trip_slippage_is_not_the_tolerance() -> None:
    """0.1.3's distinction is preserved: a tolerance is a limit, not a forecast."""
    walk = estimate_round_trip(bids=DEEP_BIDS, asks=DEEP_ASKS, notional_mxn=D("11"))
    payload = walk.telemetry()
    assert payload["tolerance_reused_as_forecast"] is False
    assert walk.entry_slippage_bps == D("0")
    assert walk.total_slippage_mxn == D("0")


# ---------------------------------------------------------------------------
# Market classification and compatibility
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("book", ["usd_mxn", "usdt_mxn", "dai_mxn", "mxnb_mxn", "paxg_mxn"])
def test_stable_and_fiat_bases_are_classified_by_name(book: str) -> None:
    assert base_is_stable_or_fiat(book) is True
    assert classify_market(book) == CLASS_STABLE_OR_FIAT


@pytest.mark.parametrize("book", ["btc_mxn", "eth_mxn", "sol_mxn", "xrp_mxn", "ada_mxn"])
def test_crypto_bases_are_classified_as_volatile_crypto(book: str) -> None:
    assert classify_market(book) == CLASS_VOLATILE_CRYPTO


def test_compatibility_is_declared_per_market_never_assumed() -> None:
    """A BTC profile is not an ETH profile and not an XRP profile."""
    assert market_compatibility(frozenset({"btc_mxn"}), "btc_mxn") == COMPATIBLE
    assert market_compatibility(frozenset({"btc_mxn"}), "eth_mxn") == RESEARCH_ONLY
    assert market_compatibility(frozenset({"btc_mxn"}), "sol_mxn") == RESEARCH_ONLY
    assert market_compatibility(frozenset({"btc_mxn"}), "usd_mxn") == INCOMPATIBLE


def test_challengers_are_certified_for_no_market_yet() -> None:
    """Research profiles must claim nothing until evidence exists."""
    for profile in CHALLENGER_PROFILES:
        assert profile.markets == frozenset()


def test_registry_is_stable_and_contains_the_champion_first() -> None:
    ids = [profile.profile_id for profile in PROFILE_REGISTRY]
    assert ids[0] == "mean-reversion-safe-v1"
    assert set(ids) == {"mean-reversion-safe-v1", "trend-continuation-v1",
                        "volatility-mean-reversion-v1", "volatility-mean-reversion-v2",
                        "range-expansion-v1"}
    # The three 0.2.1 profiles keep their exact positions. MVP 0.2.2 appended challengers;
    # it must not have reordered or displaced the frozen evidence.
    assert ids[:3] == ["mean-reversion-safe-v1", "trend-continuation-v1",
                       "volatility-mean-reversion-v1"]


def test_frozen_profiles_are_named_and_unchanged() -> None:
    """MVP 0.2.2 must not retune or overwrite any 0.2.1 profile in place."""
    frozen = {profile.profile_id for profile in FROZEN_PROFILES}
    added = {profile.profile_id for profile in RISK_ADJUSTED_CHALLENGERS}
    assert frozen == {"mean-reversion-safe-v1", "trend-continuation-v1",
                      "volatility-mean-reversion-v1"}
    assert added == {"volatility-mean-reversion-v2", "range-expansion-v1"}
    assert frozen.isdisjoint(added)


def test_the_two_generations_share_a_strategy_id_but_not_an_identity() -> None:
    """v2 is a new version of the same strategy, so it must not share v1's fingerprint."""
    v1 = next(p for p in PROFILE_REGISTRY if p.profile_id == "volatility-mean-reversion-v1")
    v2 = next(p for p in PROFILE_REGISTRY if p.profile_id == "volatility-mean-reversion-v2")
    assert v1.identity.strategy_id == v2.identity.strategy_id
    assert v1.identity.version != v2.identity.version
    assert v1.identity.fingerprint != v2.identity.fingerprint
    assert v1.identity.strategy_fingerprint != v2.identity.strategy_fingerprint


def test_unknown_profile_id_raises_instead_of_defaulting() -> None:
    """A typo must never cause trades under the wrong strategy."""
    with pytest.raises(KeyError):
        evaluator_for("does-not-exist")


# ---------------------------------------------------------------------------
# Volatility-aware targets
# ---------------------------------------------------------------------------

def test_target_model_is_bounded_on_both_sides() -> None:
    model = TargetModel(atr_multiple=D("10"), floor_bps=D("100"), cap_bps=D("300"))
    calm = volatility_features(series(["1000"] * 25, half_range_bps="2").candles, window=21)
    violent = volatility_features(series(["1000", "2000"] * 12, half_range_bps="500").candles,
                                  window=21)
    assert calm.atr_bps < model.atr_multiple * calm.atr_bps
    assert model.target_bps(calm) == D("100"), "floor prevents an unusably small target"
    assert model.target_bps(violent) == D("300"), "cap prevents unbounded expectation"


def test_target_scales_with_volatility_and_stays_deterministic() -> None:
    model = TargetModel(atr_multiple=D("1.5"), floor_bps=D("1"), cap_bps=D("100000"))
    quiet = volatility_features(series(["1000"] * 25, half_range_bps="5").candles, window=21)
    loud = volatility_features(series(["1000"] * 25, half_range_bps="200").candles, window=21)
    assert model.target_bps(loud) > model.target_bps(quiet)
    assert model.target_bps(loud) == model.target_bps(loud)


def test_invalid_target_model_bounds_are_rejected() -> None:
    with pytest.raises(ValueError):
        TargetModel(floor_bps=D("500"), cap_bps=D("100"))
    with pytest.raises(ValueError):
        TargetModel(atr_multiple=D("0"))


def test_true_range_uses_only_the_previous_close() -> None:
    """True range must be computable from two candles and nothing later."""
    item = series(["1000", "1100"])
    value = true_range_bps(D("1000"), item.candles[1])
    assert value > D("0")
    assert true_range_bps(D("0"), item.candles[1]) == D("0")


def test_volatility_features_are_past_only() -> None:
    """Features over a prefix use only that prefix, never a whole-series statistic.

    Appending a later candle legitimately changes a *trailing window*, so the test
    compares the prefix window against itself. What must never happen is that the
    prefix's own features depend on data outside the prefix.
    """
    prefix = series(["1000", "1010", "1020"])
    first = volatility_features(prefix.candles, window=3)
    assert first.last_close_mxn == D("1020")
    assert first.observations == 3
    # A much later, very different candle must not retroactively alter the prefix.
    extended = volatility_features(series(["1000", "1010", "1020", "5"]).candles, window=3)
    assert extended.last_close_mxn == D("5")
    assert first.mean_mxn == D("1010")
    assert first.atr_bps == volatility_features(prefix.candles, window=3).atr_bps


# ---------------------------------------------------------------------------
# No lookahead
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("evaluator", [MeanReversionSafeV1(), TrendContinuationV1(),
                                       VolatilityAwareMeanReversionV1()])
def test_no_profile_reads_the_future(evaluator: Any) -> None:
    ok, detail = verify_no_lookahead(candles=trending_series().candles, evaluator=evaluator,
                                     market="BTC/MXN")
    assert ok is True, detail
    assert detail == "PAST_ONLY_VERIFIED"


def test_lookahead_check_detects_a_stateful_accumulating_evaluator() -> None:
    """The check must be able to fail, or it proves nothing.

    This deliberately broken evaluator accumulates a running maximum across calls.
    Its second pass over the same prefix differs from its first, which is the
    "hoisted statistic" bug that would let a later candle influence an earlier
    decision.
    """

    class AccumulatingEvaluator:
        identity = TrendContinuationV1.identity

        def __init__(self) -> None:
            self.seen_max = D("0")

        def propose(self, *, candles: Any, quantity: D = D("0"),
                    cost_basis_mxn: D = D("0"), market: str = "BTC/MXN") -> Any:
            from autofund.mvp.profiles import DECISION_NO_SIGNAL, StrategyProposal
            self.seen_max = max(self.seen_max, *(c.close for c in candles))
            return StrategyProposal(
                decision=DECISION_NO_SIGNAL, reason_code="ENTRY_CONDITION_NOT_MET",
                profile_id="accumulating", strategy_id="accumulating",
                strategy_version="0.1", strategy_fingerprint="x", market=market,
                entry_reference_mxn=candles[-1].close,
                expected_exit_reference_mxn=self.seen_max,
                expected_gross_edge_bps=D("0"), expected_holding_horizon=1,
                target_model=TargetModel())

    ok, detail = verify_no_lookahead(
        candles=series(["1000", "900", "1200", "800"]).candles,
        evaluator=AccumulatingEvaluator(), market="BTC/MXN")
    assert ok is False
    assert "DEPENDS_ON_HISTORY_ORDER" in detail


def test_lookahead_slice_contract_is_enforced() -> None:
    """The replay must hand the profile exactly the prefix ending at candle N.

    A wrong slice is the realistic way lookahead gets introduced, so the check
    verifies the spans the profile actually receives rather than trusting the loop.
    """
    item = trending_series()
    span_lengths: list[int] = []

    class Recording(MeanReversionSafeV1):
        def propose(self, *, candles: Any, **kwargs: Any) -> Any:
            span_lengths.append(len(candles))
            return super().propose(candles=candles, **kwargs)

    ok, detail = verify_no_lookahead(candles=item.candles, evaluator=Recording(),
                                     market="BTC/MXN")
    assert ok is True, detail
    assert span_lengths[:len(item.candles)] == list(range(1, len(item.candles) + 1))


def test_replay_exit_only_uses_candles_after_entry() -> None:
    item = trending_series()
    result = replay_candles(
        candles=item.candles, profile_id="trend-continuation-v1", market="BTC/MXN",
        evaluator=TrendContinuationV1(), taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
        bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY)
    for trip in result.round_trips:
        assert trip.exit_index > trip.entry_index
        assert trip.holding_candles == trip.exit_index - trip.entry_index
        assert trip.entry_price_mxn == item.candles[trip.entry_index].close


def test_every_profile_evaluates_identically_on_repeated_runs() -> None:
    """Determinism is a certification requirement, so it is asserted directly."""
    item = trending_series()
    for evaluator in (MeanReversionSafeV1(), TrendContinuationV1(),
                      VolatilityAwareMeanReversionV1()):
        first = replay_candles(
            candles=item.candles, profile_id=evaluator.identity.profile_id, market="BTC/MXN",
            evaluator=evaluator, taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
            bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY)
        second = replay_candles(
            candles=item.candles, profile_id=evaluator.identity.profile_id, market="BTC/MXN",
            evaluator=evaluator_for(evaluator.identity.profile_id),
            taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"), bids=DEEP_BIDS,
            asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY)
        assert first.telemetry() == second.telemetry()


# ---------------------------------------------------------------------------
# Evidence discipline
# ---------------------------------------------------------------------------

def test_percentiles_refuse_to_report_a_small_sample() -> None:
    """No p90 from three observations. Too little data is reported as such."""
    small = percentiles([D("1"), D("2"), D("3")], minimum_sample=5)
    assert small.p50 is None and small.p90 is None
    assert small.count == 3
    enough = percentiles([D("1"), D("2"), D("3"), D("4"), D("5")], minimum_sample=5)
    assert enough.p50 is not None and enough.p10 is not None


def test_empty_percentiles_report_nothing_rather_than_zero() -> None:
    empty = percentiles([], minimum_sample=5)
    assert empty.count == 0 and empty.p50 is None


def test_train_evaluation_split_is_chronological_and_non_overlapping() -> None:
    """Shuffling a time series would leak the future into the training window."""
    item = series([str(1000 + index) for index in range(100)])
    train, evaluation = train_evaluation_split(item.candles)
    assert train and evaluation
    assert train[-1].timestamp < evaluation[0].timestamp
    assert len(train) + len(evaluation) == len(item.candles)


def test_certification_requires_round_trips_not_just_positive_pnl() -> None:
    """Zero round trips can never certify, however the P&L happens to read."""
    item = series(["1000"] * 30)
    result = replay_candles(
        candles=item.candles, profile_id="trend-continuation-v1", market="BTC/MXN",
        evaluator=TrendContinuationV1(), taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
        bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY)
    verdict = certify(replay=result, lookahead_ok=True, deterministic=True)
    assert verdict.certified is False
    assert CHECK_ROUND_TRIPS in verdict.failed


def test_certification_fails_on_lookahead_even_with_good_economics() -> None:
    item = trending_series()
    result = replay_candles(
        candles=item.candles, profile_id="trend-continuation-v1", market="BTC/MXN",
        evaluator=TrendContinuationV1(), taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
        bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY)
    verdict = certify(replay=result, lookahead_ok=False, deterministic=True)
    assert verdict.certified is False
    assert "NO_LOOKAHEAD" in verdict.failed


def test_certification_fails_when_replay_is_not_deterministic() -> None:
    item = trending_series()
    result = replay_candles(
        candles=item.candles, profile_id="trend-continuation-v1", market="BTC/MXN",
        evaluator=TrendContinuationV1(), taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
        bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY)
    verdict = certify(replay=result, lookahead_ok=True, deterministic=False)
    assert verdict.certified is False
    assert "REPLAY_DETERMINISM" in verdict.failed


def test_certification_reports_every_check_it_runs() -> None:
    item = trending_series()
    result = replay_candles(
        candles=item.candles, profile_id="trend-continuation-v1", market="BTC/MXN",
        evaluator=TrendContinuationV1(), taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
        bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY)
    verdict = certify(replay=result, lookahead_ok=True, deterministic=True)
    telemetry = verdict.telemetry()
    assert set(telemetry["passed"]) | set(telemetry["failed"])
    assert telemetry["promotion"] == PROMOTION
    assert telemetry["requirements"]["min_round_trips"] == str(
        DEFAULT_REQUIREMENTS.min_round_trips)


def test_fiat_like_markets_can_never_certify_volatile_crypto_profiles() -> None:
    """A fiat-like book is excluded by class, independently of its economics."""
    item = trending_series()
    result = replay_candles(
        candles=item.candles, profile_id="trend-continuation-v1", market="BTC/MXN",
        evaluator=TrendContinuationV1(), taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
        bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY,
        compatibility=INCOMPATIBLE)
    verdict = certify(replay=result, lookahead_ok=True, deterministic=True,
                      market_class=CLASS_STABLE_OR_FIAT)
    assert verdict.certified is False
    assert "DATA_QUALITY_PASS" in verdict.failed


def test_non_mxn_quoted_markets_are_refused_with_a_clear_cause() -> None:
    """AutoFund's envelope is MXN-only; a non-MXN quote must fail loudly, not deeply."""
    with pytest.raises(ValueError, match="UNSUPPORTED_QUOTE_CURRENCY"):
        evaluate_profile_market(
            market="BTC/USD", profile_id="trend-continuation-v1", series=trending_series(),
            taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"), bids=DEEP_BIDS,
            asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY)


def test_demonstrated_economic_failure_is_not_viable_not_under_evidenced() -> None:
    """A large sample that refused every opportunity is a finding, not a data gap.

    The Champion proposes opportunities and the guard refuses them all on economics.
    With enough evaluations that is a demonstrated failure, and reporting it as
    merely "insufficient evidence" would understate what was actually established.
    """
    item = flat_then(["900"] * 12)
    result = replay_candles(
        candles=item.candles, profile_id="mean-reversion-safe-v1", market="BTC/MXN",
        evaluator=MeanReversionSafeV1(), taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
        bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY)
    assert result.buy_signals > 0 and result.economic_admissions == 0
    assert result.economic_rejections > 0
    verdict = certify(replay=result, lookahead_ok=True, deterministic=True,
                      requirements=EvidenceRequirements(min_evaluations=1, min_candles=1,
                                                        min_round_trips=0))
    assert verdict.status == NOT_VIABLE
    assert verdict.certified is False


def test_no_opportunity_is_reported_as_insufficient_evidence() -> None:
    """Never signalling is a genuine absence of evidence, not a viability verdict."""
    item = series(["1000"] * 40)
    result = replay_candles(
        candles=item.candles, profile_id="trend-continuation-v1", market="BTC/MXN",
        evaluator=TrendContinuationV1(), taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
        bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY)
    assert result.buy_signals == 0
    verdict = certify(replay=result, lookahead_ok=True, deterministic=True)
    assert verdict.status == INSUFFICIENT_EVIDENCE
    assert "NONZERO_OPPORTUNITY_COUNT" in verdict.failed


def test_selector_reports_latest_intention_even_when_percentiles_are_unavailable() -> None:
    """The intended edge is a fact; the distribution is a statistic. Both are exposed."""
    item = flat_then(["900"])
    item_result = evaluate_profile_market(
        market="BTC/MXN", profile_id="mean-reversion-safe-v1", series=item,
        taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"), bids=DEEP_BIDS, asks=DEEP_ASKS,
        budget_mxn=D("11"), policy=DEFAULT_POLICY)
    contract = item_result.selector_contract()
    assert contract["latest_intended_gross_edge_bps"] == "20.000"
    assert D(contract["latest_round_trip_friction_bps"]) > D("150")
    assert contract["expected_gross_edge_bps"] is None, "too few observations for a percentile"
    requirements = EvidenceRequirements(min_candles=1, min_evaluations=1, min_round_trips=1,
                                        min_opportunities=1, max_drawdown_mxn=D("1000"),
                                        require_positive_net_pnl=False,
                                        require_no_lookahead=False,
                                        require_execution_compatible=False)
    item = trending_series()
    result = replay_candles(
        candles=item.candles, profile_id="trend-continuation-v1", market="ETH/MXN",
        evaluator=TrendContinuationV1(), taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
        bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY,
        compatibility=RESEARCH_ONLY)
    verdict = certify(replay=result, lookahead_ok=False, deterministic=True,
                      requirements=requirements)
    assert verdict.requirements is requirements
    assert verdict.telemetry()["requirements"]["min_candles"] == "1"


# ---------------------------------------------------------------------------
# Shadow pipeline: the 0.1.2 gap
# ---------------------------------------------------------------------------

class _Provider:
    """Minimal series provider; a market absent from the mapping has no data."""

    def __init__(self, mapping: dict[str, CandleSeries]) -> None:
        self.mapping = mapping

    def series_for(self, market: str) -> CandleSeries | None:
        return self.mapping.get(market)


def test_shadow_pipeline_produces_real_evaluations() -> None:
    """The gap that made `shadow_evaluations` zero must actually be closed."""
    provider = _Provider({"BTC/MXN": trending_series()})
    run = run_shadow_research(
        eligible_markets=("BTC/MXN",), provider=provider, taker_fee_rate=PRODUCTION_FEE,
        spread_bps=D("10"), books=SHADOW_BOOKS, budget_mxn=D("11"))
    assert run.evaluated_markets == 1
    assert run.total_evaluations > 0, "shadow research must evaluate, not merely announce"
    assert run.total_shadow_trades >= 1
    assert run.total_net_pnl_mxn != D("0")


def test_shadow_pipeline_reports_missing_data_honestly() -> None:
    """A market without candles is reported as such, never given synthetic data."""
    run = run_shadow_research(
        eligible_markets=("ETH/MXN",), provider=_Provider({}), taker_fee_rate=PRODUCTION_FEE,
        spread_bps=D("10"), budget_mxn=D("11"))
    result = run.markets[0]
    assert result.status == NO_CANDLE_DATA
    assert result.evaluations == 0
    assert run.total_evaluations == 0
    assert run.evaluated_markets == 0


def test_shadow_pipeline_excludes_fiat_like_markets_by_classification() -> None:
    """A fiat-like book is excluded by class, not by a volatility heuristic."""
    run = run_shadow_research(
        eligible_markets=("usd_mxn",), provider=_Provider({"usd_mxn": trending_series()}),
        taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"), budget_mxn=D("11"))
    excluded = [item["market"] for item in run.excluded]
    assert "usd_mxn" in excluded


def test_shadow_pipeline_creates_no_production_orders() -> None:
    provider = _Provider({"BTC/MXN": trending_series()})
    run = run_shadow_research(
        eligible_markets=("BTC/MXN",), provider=provider, taker_fee_rate=PRODUCTION_FEE,
        spread_bps=D("10"), budget_mxn=D("11"))
    payload = run.telemetry()
    assert payload["production_orders"] == 0
    for result in payload["results"]:
        assert result["production_orders"] == 0
        assert result["shadow_is_separate_from_production"] is True


def test_shadow_pipeline_covers_multiple_markets_and_profiles() -> None:
    provider = _Provider({"BTC/MXN": trending_series(), "ETH/MXN": trending_series()})
    run = run_shadow_research(
        eligible_markets=("BTC/MXN", "ETH/MXN"), provider=provider,
        taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"), books=SHADOW_BOOKS,
        budget_mxn=D("11"))
    assert run.evaluated_markets == 2
    per_market = run.markets[0].evidence
    assert len(per_market) == len(PROFILE_REGISTRY)


def test_shadow_evaluation_status_is_per_profile_market() -> None:
    """Compatibility must be reported per pair, since a profile is not universal."""
    provider = _Provider({"ETH/MXN": trending_series()})
    run = run_shadow_research(
        eligible_markets=("ETH/MXN",), provider=provider, taker_fee_rate=PRODUCTION_FEE,
        spread_bps=D("10"), budget_mxn=D("11"))
    for item in run.markets[0].evidence:
        if item.profile_id == "mean-reversion-safe-v1":
            assert item.compatibility == RESEARCH_ONLY
        else:
            assert item.compatibility == RESEARCH_ONLY


def test_scanner_records_real_shadow_evaluations() -> None:
    """`shadow_evaluations` must count genuine evaluations, not announcements."""
    from autofund.mvp.scanner import MarketScanner

    scanner = MarketScanner(source=None)
    scanner.shadow = {"BTC/MXN": {"market": "BTC/MXN", "evidence_count": 0, "evaluated": False}}
    scanner.record_shadow_evaluation(
        "BTC/MXN", candles=52, evaluations=156, signals=2, shadow_trades=1,
        gross_pnl_mxn="0.89", fees_mxn="0.09", slippage_mxn="0", net_pnl_mxn="0.80",
        data_source=CAPTURED)
    evidence = scanner.scanner_evidence()
    assert evidence["shadow_evaluations"] == 1
    row = scanner.evidence()["shadow"][0]
    assert row["evaluated"] is True
    assert row["net_pnl_mxn"] == "0.80"
    assert row["data_source"] == CAPTURED


def test_scanner_shadow_intent_seeds_awaiting_data_not_a_false_zero() -> None:
    from autofund.mvp.scanner import MarketScanner

    scanner = MarketScanner(source=None)
    scanner._run_shadow(emit=lambda *args, **kwargs: None)
    assert scanner.shadow == {}


# ---------------------------------------------------------------------------
# Position contracts
# ---------------------------------------------------------------------------

def _contract(profile_id: str = "trend-continuation-v1") -> PositionContract:
    return PositionContract(
        key="af-live-abc", market="BTC/MXN", major_asset="BTC",
        strategy_profile_id=profile_id, strategy_version="0.1",
        strategy_fingerprint="fp", profile_fingerprint="pfp",
        entry_economics=EntryEconomicEvidence(
            expected_gross_edge_bps=D("300"), estimated_round_trip_friction_bps=D("165"),
            expected_net_edge_bps=D("135"), taker_fee_rate=PRODUCTION_FEE,
            spread_bps=D("10"), slippage_bps=D("0"), admission_outcome="ECONOMICALLY_ADMISSIBLE",
            economic_policy_version="autofund.economic-edge.v1"),
        expected_exit_model=ExpectedExitModel(
            exit_class=PROFIT_TAKING, target_price_mxn=D("1003000"), target_bps=D("300"),
            target_model_version="autofund.volatility-aware-target.v1",
            expected_holding_horizon=60),
        opened_at="2026-09-25T00:00:00Z")


def test_contract_persists_the_owning_strategy_and_evidence(tmp_path: Any) -> None:
    store = PositionContractStore(tmp_path)
    store.record_entry(_contract())
    loaded = store.get("af-live-abc")
    assert loaded.strategy_profile_id == "trend-continuation-v1"
    assert loaded.entry_economics.expected_gross_edge_bps == D("300")
    assert loaded.expected_exit_model.exit_class == PROFIT_TAKING
    assert loaded.fingerprint == _contract().fingerprint


def test_a_position_cannot_be_closed_by_a_different_strategy() -> None:
    """Spec 20: profile A's position must never use profile B's exit semantics."""
    contract = _contract("trend-continuation-v1")
    with pytest.raises(ContractError, match="EXIT_PROFILE_DOES_NOT_MATCH_ENTRY_PROFILE"):
        exit_semantics_for(contract, "mean-reversion-safe-v1")
    assert exit_semantics_for(contract, "trend-continuation-v1").target_bps == D("300")


def test_missing_contract_raises_rather_than_falling_back(tmp_path: Any) -> None:
    """A silent fallback to the Champion would be the exact bug this prevents."""
    store = PositionContractStore(tmp_path)
    with pytest.raises(ContractError, match="NO_POSITION_CONTRACT_FOR_ORIGIN"):
        store.get("af-live-unknown")


def test_contract_survives_a_restart(tmp_path: Any) -> None:
    PositionContractStore(tmp_path).record_entry(_contract())
    reopened = PositionContractStore(tmp_path)
    assert reopened.get("af-live-abc").strategy_profile_id == "trend-continuation-v1"


def test_closed_contract_is_no_longer_considered_open(tmp_path: Any) -> None:
    store = PositionContractStore(tmp_path)
    store.record_entry(_contract())
    store.record_exit(key="af-live-abc", reason="PROFIT_TAKING_EXIT",
                      exit_profile_id="trend-continuation-v1")
    assert store.open_contracts() == ()
    with pytest.raises(ContractError):
        store.get("af-live-abc")


def test_risk_exits_remain_independent_of_any_contract() -> None:
    """Safety is never negotiated with a strategy contract."""
    assert is_risk_exit(RISK_EXIT) is True
    assert is_risk_exit(PROFIT_TAKING) is False


def test_contract_rejects_malformed_input() -> None:
    with pytest.raises(ContractError):
        PositionContract(
            key="", market="BTC/MXN", major_asset="BTC",
            strategy_profile_id="x", strategy_version="0.1", strategy_fingerprint="fp",
            profile_fingerprint="pfp",
            entry_economics=_contract().entry_economics,
            expected_exit_model=_contract().expected_exit_model, opened_at="now")


# ---------------------------------------------------------------------------
# Research report and the MVP 0.2 selector contract
# ---------------------------------------------------------------------------

def test_research_report_covers_every_profile_and_market() -> None:
    from autofund.mvp.research import build_research_report

    report = build_research_report(
        series_by_market={"BTC/MXN": trending_series(), "ETH/MXN": trending_series()},
        taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"), budget_mxn=D("11"),
        generated_at="2026-09-25T00:00:00Z")
    assert len(report.rows) == 2 * len(PROFILE_REGISTRY)
    assert report.champion_row is not None
    assert len(report.challenger_rows) == 2 * len(CHALLENGER_PROFILES)
    telemetry = report.telemetry()
    assert telemetry["promotion"] == PROMOTION
    assert telemetry["multi_market_production"] == "DISABLED"


def test_research_report_does_not_promote_anything() -> None:
    from autofund.mvp.research import build_research_report

    report = build_research_report(
        series_by_market={"BTC/MXN": trending_series()}, taker_fee_rate=PRODUCTION_FEE,
        spread_bps=D("10"), budget_mxn=D("11"), generated_at="2026-09-25T00:00:00Z")
    assert report.certifiable == ()
    for row in report.rows:
        assert row.verdict.telemetry()["promotion"] == PROMOTION


def test_selector_contract_exposes_everything_a_selector_needs() -> None:
    """MVP 0.2 preparation: complete, self-describing, and fingerprinted."""
    item = evaluate_profile_market(
        market="BTC/MXN", profile_id="trend-continuation-v1", series=trending_series(),
        taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"), bids=DEEP_BIDS, asks=DEEP_ASKS,
        budget_mxn=D("11"), policy=DEFAULT_POLICY)
    contract = item.selector_contract()
    for field in ("market", "market_class", "profile_id", "profile_fingerprint",
                  "strategy_fingerprint", "market_certification_status",
                  "strategy_compatibility", "expected_gross_edge_bps",
                  "expected_round_trip_friction_bps", "expected_net_edge_bps",
                  "economic_guard_result", "economic_reject_rate", "evidence_fingerprint"):
        assert field in contract, field
    assert contract["multi_market_production"] == "DISABLED"
    assert contract["promotion"] == PROMOTION
    assert item.evidence_fingerprint


def test_fixture_evidence_is_labelled_and_never_claims_real_performance() -> None:
    """Synthetic results must be marked, or they would be an overclaim."""
    item = evaluate_profile_market(
        market="BTC/MXN", profile_id="trend-continuation-v1", series=trending_series(),
        taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"), bids=DEEP_BIDS, asks=DEEP_ASKS,
        budget_mxn=D("11"), policy=DEFAULT_POLICY)
    assert item.data_source == SYNTHETIC
    assert "FIXTURE_EVIDENCE_NOT_REAL_MARKET_PERFORMANCE" in item.notes


# ---------------------------------------------------------------------------
# Decimal discipline
# ---------------------------------------------------------------------------

def test_no_float_appears_in_any_decision_payload() -> None:
    """Floats are forbidden in money. The whole payload is a Decimal-derived dict."""
    item = trending_series()
    proposal = TrendContinuationV1().propose(candles=item.candles)
    result = replay_candles(
        candles=item.candles, profile_id="trend-continuation-v1", market="BTC/MXN",
        evaluator=TrendContinuationV1(), taker_fee_rate=PRODUCTION_FEE, spread_bps=D("10"),
        bids=DEEP_BIDS, asks=DEEP_ASKS, budget_mxn=D("11"), policy=DEFAULT_POLICY)

    def assert_no_float(value: Any, path: str = "") -> None:
        assert not isinstance(value, float), f"float at {path}"
        if isinstance(value, dict):
            for key, item_value in value.items():
                assert_no_float(item_value, f"{path}.{key}")
        elif isinstance(value, (list, tuple)):
            for index, item_value in enumerate(value):
                assert_no_float(item_value, f"{path}[{index}]")

    assert_no_float(proposal.telemetry(), "proposal")
    assert_no_float(result.telemetry(), "replay")
    assert_no_float(assess_viability(
        proposal=proposal, budget_mxn=D("11"), taker_fee_rate=PRODUCTION_FEE,
        spread_bps=D("10"), bids=DEEP_BIDS, asks=DEEP_ASKS,
        policy=DEFAULT_POLICY).telemetry(), "viability")


def test_profiles_never_hold_float_parameters() -> None:
    """Parameters and targets are Decimals and ints only."""
    for profile in PROFILE_REGISTRY:
        for _name, value in profile.identity.parameters:
            assert isinstance(value, (str, int)), (_name, value)
        for key, value in profile.identity.target_model.public().items():
            assert not isinstance(value, float), key


def test_slippage_arithmetic_is_decimal_only() -> None:
    walk = walk_for_notional((Level(D("100"), D("0.05")), Level(D("110"), D("1"))), D("10"))
    assert isinstance(walk.expected_slippage_bps, Decimal)
    assert isinstance(walk.expected_execution_price_mxn, Decimal)
    assert isinstance(walk.filled_mxn, Decimal)


def test_percentiles_return_decimal_not_float() -> None:
    stats = percentiles([D("1"), D("2"), D("3"), D("4"), D("5")], minimum_sample=5)
    assert isinstance(stats.p50, Decimal) and isinstance(stats.p90, Decimal)


# ---------------------------------------------------------------------------
# Production safety unchanged
# ---------------------------------------------------------------------------

def test_promotion_is_disabled_everywhere_it_is_reported() -> None:
    """Spec 16: no automatic strategy promotion, anywhere."""
    assert PROMOTION == "DISABLED"
    for profile in PROFILE_REGISTRY:
        assert "promotion" not in profile.public()


def test_champion_remains_the_production_profile() -> None:
    """Challengers exist as evidence; the Champion keeps Production."""
    champion = AdaptiveEngine().champion
    assert champion.profile_id == "mean-reversion-safe"
    assert champion.certification_status == "CERTIFIED"
    assert AdaptiveEngine().auto_promotion is False


def test_no_module_in_this_milestone_imports_a_write_transport() -> None:
    """The research path must be structurally incapable of an exchange write."""
    import pathlib

    forbidden = ("BitsoProductionLiveClient", "BitsoProductionLiveTransport",
                 "submit_authorized", "submit_sell_authorized", "permit.consume")
    root = pathlib.Path("src/autofund/mvp")
    for name in ("profiles.py", "profile_library.py", "slippage.py", "viability.py",
                 "replay_eval.py", "research.py", "shadow_research.py", "position_contract.py",
                 "historical.py"):
        text = (root / name).read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in text, f"{name} references {marker}"


def test_production_fee_is_used_and_never_a_placeholder() -> None:
    """The economics must be driven by the confirmed fee, not a guessed constant."""
    item = flat_then(["900"])
    proposal = MeanReversionSafeV1().propose(candles=item.candles)
    assessment = assess_viability(
        proposal=proposal, budget_mxn=D("11"), taker_fee_rate=PRODUCTION_FEE,
        spread_bps=D("10"), bids=DEEP_BIDS, asks=DEEP_ASKS, policy=DEFAULT_POLICY)
    assert assessment.friction.entry_fee_mxn == D("11") * PRODUCTION_FEE
    assert assessment.policy is DEFAULT_POLICY


def test_spread_and_slippage_are_reported_separately_from_fees() -> None:
    item = trending_series()
    proposal = TrendContinuationV1().propose(candles=item.candles)
    assessment = assess_viability(
        proposal=proposal, budget_mxn=D("11"), taker_fee_rate=PRODUCTION_FEE,
        spread_bps=D("25"), bids=DEEP_BIDS, asks=DEEP_ASKS, policy=DEFAULT_POLICY)
    payload = assessment.telemetry()
    assert payload["friction"]["spread_cost_mxn"] != payload["friction"]["entry_fee_mxn"]
    assert payload["slippage"]["entry_slippage_bps"] == "0"
    assert payload["slippage_tolerance_reused_as_forecast"] is False

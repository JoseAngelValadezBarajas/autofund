"""Concrete strategy profiles: one frozen Champion and research challengers.

The Champion is reproduced here **byte-for-byte in behaviour** as
``mean-reversion-safe-v1``. Its thresholds (3 / 20 / 10 bps / 20 bps), its
arithmetic and therefore its fingerprint are unchanged. This milestone does not
retune it; it only names it, freezes it, and treats it as reference evidence that
cannot open a new Production position at the real fee.

The challengers exist because the *scale of the intended move* was the defect, not
the guard. They target larger opportunities and size their targets against
observed volatility, so their proposals can survive real friction. They are
deliberately not presented as profitable: viability is decided per market by
EconomicEdgeGuard and by replay evidence, and most will report NOT_VIABLE.

Design rules, all enforced by construction:

- **Past-only.** Every profile reads only the candle slice it is given. The slice
  ends at the current closed candle. No profile can index past its end.
- **Decimal-only.** No floats anywhere in a decision path.
- **Deterministic.** Pure functions of candles and declared parameters. Same
  candles, same proposal, same fingerprint.
- **Bounded.** Targets are floored and capped, so no parameter can be pushed to an
  absurd value by a favourable sample.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ONE, ZERO, financial
from autofund.replay.data import Candle

from .champion import CHAMPION_PARAMETERS, ChampionParameters
from .profiles import (
    BPS,
    DECISION_BUY,
    DECISION_NO_SIGNAL,
    DECISION_SELL,
    ENTRY_CONDITION_NOT_MET,
    EXIT_CONDITION_NOT_MET,
    INSUFFICIENT_HISTORY,
    NO_CLOSED_CANDLE,
    SIGNAL_BUY,
    SIGNAL_SELL,
    ProfileIdentity,
    StrategyProposal,
    TargetModel,
    VolatilityFeatures,
    volatility_features,
)

# ---------------------------------------------------------------------------
# Market certification sets. A profile declares where it has evidence. Nothing
# here is promoted automatically: the Champion keeps Production regardless.
# ---------------------------------------------------------------------------

CHAMPION_MARKETS = frozenset({"btc_mxn"})

# Challengers are research-only: they declare no certified market yet, so they are
# RESEARCH_ONLY everywhere until replay evidence supports certification.
NO_CERTIFIED_MARKETS: frozenset[str] = frozenset()


# Target models are declared once and shared between each profile's identity and its
# decision logic. Sharing one frozen instance is what guarantees the fingerprint
# describes the parameters the evaluator actually uses.
CHAMPION_TARGET_MODEL = TargetModel(atr_multiple=Decimal("1.5"), floor_bps=Decimal("60"),
                                   cap_bps=Decimal("1200"))
TREND_TARGET_MODEL = TargetModel(atr_multiple=Decimal("2.0"), floor_bps=Decimal("250"),
                                 cap_bps=Decimal("1500"))
VOLATILITY_MR_TARGET_MODEL = TargetModel(atr_multiple=Decimal("1.5"),
                                         floor_bps=Decimal("120"), cap_bps=Decimal("900"))


@dataclass(frozen=True, slots=True)
class ProfileDefinition:
    """A profile: identity, the markets it is certified for, and its evaluator."""

    identity: ProfileIdentity
    markets: frozenset[str]
    evaluator: str
    description: str

    @property
    def profile_id(self) -> str:
        return self.identity.profile_id

    @property
    def fingerprint(self) -> str:
        return self.identity.fingerprint

    def public(self) -> dict[str, Any]:
        return {**self.identity.public(), "markets": sorted(self.markets),
                "evaluator": self.evaluator, "description": self.description}


# ===========================================================================
# Profile 1 — mean-reversion-safe-v1 (the frozen Champion)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class MeanReversionSafeV1:
    """The certified Champion, reproduced exactly and frozen.

    Decision logic is identical to `evaluate_champion`:
      - BUY  when close <= mean(window) * (1 - 0.001)
      - SELL when close >= average_cost * (1 + 0.002)

    Its proposal uses the Champion's own 20 bps exit target, which is why it is
    structurally NOT_VIABLE at the confirmed 78 bps fee. That is the 0.1.3 finding,
    preserved as evidence rather than edited away.
    """

    parameters: ChampionParameters = CHAMPION_PARAMETERS
    target_model: TargetModel = CHAMPION_TARGET_MODEL

    identity = ProfileIdentity(profile_id="mean-reversion-safe-v1",
                               strategy_id="mean_reversion", version="0.1",
                               parameters=(("entry_threshold", "0.001"),
                                           ("exit_threshold", "0.002"),
                                           ("max_window", "20"), ("min_history", "3")),
                               target_model=CHAMPION_TARGET_MODEL)

    @financial
    def propose(self, *, candles: Sequence[Candle], quantity: Decimal = ZERO,
                cost_basis_mxn: Decimal = ZERO, market: str = "BTC/MXN") -> StrategyProposal:
        window = self.parameters.max_window

        def build(decision: str, reason: str, entry: Decimal, target: Decimal,
                  features: VolatilityFeatures) -> StrategyProposal:
            gross = ((target - entry) / entry * BPS) if entry > ZERO and target > ZERO else ZERO
            return StrategyProposal(
                decision=decision, reason_code=reason, profile_id=self.identity.profile_id,
                strategy_id=self.identity.strategy_id, strategy_version=self.identity.version,
                strategy_fingerprint=self.identity.strategy_fingerprint, market=market,
                entry_reference_mxn=entry, expected_exit_reference_mxn=target,
                expected_gross_edge_bps=gross,
                expected_holding_horizon=self.parameters.max_window, target_model=self.target_model,
                confidence_evidence={"window": str(min(len(candles), window)),
                                     "entry_threshold": str(self.parameters.entry_threshold),
                                     "exit_threshold": str(self.parameters.exit_threshold)},
                features=features, closed_candle_count=len(candles),
                position_open=quantity > ZERO)

        if not candles:
            return build(DECISION_NO_SIGNAL, NO_CLOSED_CANDLE, ZERO, ZERO,
                         VolatilityFeatures(0, ZERO, ZERO, ZERO, ZERO, ZERO))
        features = volatility_features(candles, window=window)
        if len(candles) < self.parameters.min_history:
            return build(DECISION_NO_SIGNAL, INSUFFICIENT_HISTORY, ZERO, ZERO, features)

        close = candles[-1].close
        if quantity > ZERO and cost_basis_mxn > ZERO:
            average_cost = cost_basis_mxn / quantity
            boundary = average_cost * (ONE + self.parameters.exit_threshold)
            if close >= boundary:
                return build(DECISION_SELL, SIGNAL_SELL, close, boundary, features)
            return build(DECISION_NO_SIGNAL, EXIT_CONDITION_NOT_MET, close, boundary, features)

        boundary = features.mean_mxn * (ONE - self.parameters.entry_threshold)
        if close <= boundary:
            # The Champion's exit target is its own average-cost boundary, so the
            # gross edge it intends is exactly its 20 bps threshold.
            average_cost = close
            target = average_cost * (ONE + self.parameters.exit_threshold)
            return build(DECISION_BUY, SIGNAL_BUY, close, target, features)
        return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, boundary, features)


# ===========================================================================
# Profile 2 — trend-continuation-v1
# ===========================================================================


@dataclass(frozen=True, slots=True)
class TrendContinuationV1:
    """Follows an established directional move instead of fading it.

    Rationale: mean reversion's ceiling is the distance back to the mean, which is
    small exactly when the market is calm. Trend continuation's ceiling is the
    continuation of a move already larger than recent noise, so it can target moves
    that exceed round-trip cost.

    Entry requires *two* independent confirmations, so a single noisy candle cannot
    trigger it:
      1. price is above the fast mean by more than `momentum_threshold_bps`;
      2. the fast mean is itself above the slow mean (direction is established).

    Exit is the profit target or a **volatility-bounded stop below the fast mean**.

    The stop sitting strictly below the fast mean is not a detail. Entry requires price
    to be *above* the fast mean, so an exit that fired at the fast mean would be
    violated the instant the position opened, and would close on ordinary noise for a
    guaranteed loss after two fees. The stop distance is volatility-scaled and bounded
    on both sides, so it is wide in a volatile market and never degenerate in a calm one.
    """

    fast_window: int = 8
    slow_window: int = 21
    momentum_threshold_bps: Decimal = Decimal("25")
    stop_atr_multiple: Decimal = Decimal("1.5")
    stop_floor_bps: Decimal = Decimal("30")
    stop_cap_bps: Decimal = Decimal("400")
    min_history: int = 21
    expected_holding_horizon: int = 60
    target_model: TargetModel = TREND_TARGET_MODEL

    identity = ProfileIdentity(
        profile_id="trend-continuation-v1", strategy_id="trend_continuation", version="0.2",
        parameters=(("fast_window", "8"), ("slow_window", "21"),
                    ("momentum_threshold_bps", "25"), ("min_history", "21"),
                    ("expected_holding_horizon", "60"), ("stop_atr_multiple", "1.5"),
                    ("stop_floor_bps", "30"), ("stop_cap_bps", "400")),
        target_model=TREND_TARGET_MODEL)

    @financial
    def stop_distance_bps(self, features: VolatilityFeatures) -> Decimal:
        """Volatility-scaled stop distance, floored and capped.

        Bounded on both sides for the same reason the target is: an unbounded multiple
        would be pushed to an extreme by one volatile sample, and an unfloored one would
        be tighter than the spread in a calm market.
        """
        raw = features.atr_bps * self.stop_atr_multiple
        return min(max(raw, self.stop_floor_bps), self.stop_cap_bps)

    @financial
    def propose(self, *, candles: Sequence[Candle], quantity: Decimal = ZERO,
                cost_basis_mxn: Decimal = ZERO, market: str = "BTC/MXN") -> StrategyProposal:
        def build(decision: str, reason: str, entry: Decimal, target: Decimal,
                  features: VolatilityFeatures) -> StrategyProposal:
            gross = ((target - entry) / entry * BPS) if entry > ZERO and target > ZERO else ZERO
            return StrategyProposal(
                decision=decision, reason_code=reason, profile_id=self.identity.profile_id,
                strategy_id=self.identity.strategy_id, strategy_version=self.identity.version,
                strategy_fingerprint=self.identity.strategy_fingerprint, market=market,
                entry_reference_mxn=entry, expected_exit_reference_mxn=target,
                expected_gross_edge_bps=gross,
                expected_holding_horizon=self.expected_holding_horizon,
                target_model=self.target_model,
                confidence_evidence={"fast_window": str(self.fast_window),
                                     "slow_window": str(self.slow_window),
                                     "momentum_threshold_bps": str(self.momentum_threshold_bps),
                                     "stop_distance_bps": str(self.stop_distance_bps(features))},
                features=features, closed_candle_count=len(candles),
                position_open=quantity > ZERO)

        if not candles:
            return build(DECISION_NO_SIGNAL, NO_CLOSED_CANDLE, ZERO, ZERO,
                         VolatilityFeatures(0, ZERO, ZERO, ZERO, ZERO, ZERO))
        features = volatility_features(candles, window=self.slow_window)
        if len(candles) < self.min_history:
            return build(DECISION_NO_SIGNAL, INSUFFICIENT_HISTORY, ZERO, ZERO, features)

        closes = [c.close for c in candles[-self.slow_window:]]
        fast = sum(closes[-self.fast_window:], ZERO) / Decimal(self.fast_window)
        slow = sum(closes, ZERO) / Decimal(len(closes))
        close = closes[-1]

        if quantity > ZERO and cost_basis_mxn > ZERO:
            average_cost = cost_basis_mxn / quantity
            target = self.target_model.target_price_mxn(average_cost, features)
            # Strictly below the fast mean, so entry cannot immediately violate the stop.
            stop = fast * (ONE - self.stop_distance_bps(features) / BPS)
            if close <= stop or close >= target:
                return build(DECISION_SELL, SIGNAL_SELL, close, target, features)
            return build(DECISION_NO_SIGNAL, EXIT_CONDITION_NOT_MET, close, target, features)

        if fast <= slow:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, slow, features)
        momentum_bps = (close - fast) / fast * BPS if fast > ZERO else ZERO
        if momentum_bps < self.momentum_threshold_bps:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, slow, features)
        target = self.target_model.target_price_mxn(close, features)
        return build(DECISION_BUY, SIGNAL_BUY, close, target, features)


# ===========================================================================
# Profile 3 — volatility-aware mean reversion
# ===========================================================================


@dataclass(frozen=True, slots=True)
class VolatilityAwareMeanReversionV1:
    """Mean reversion whose entry depth and target both scale with volatility.

    The Champion's fixed 10 bps entry band is tiny in a volatile market and wide in
    a calm one. This profile requires the price to be displaced from the mean by a
    volatility-scaled distance, so it only acts on genuinely stretched prices, and
    it targets the reversion to the mean -- a move whose size is a property of the
    displacement, not a constant.

    Entry requires displacement beyond `displacement_atr_multiple * ATR`, so in a
    calm market small noise cannot trigger it.
    """

    window: int = 21
    displacement_atr_multiple: Decimal = Decimal("2.0")
    min_history: int = 21
    expected_holding_horizon: int = 45
    target_model: TargetModel = VOLATILITY_MR_TARGET_MODEL

    identity = ProfileIdentity(
        profile_id="volatility-mean-reversion-v1", strategy_id="volatility_mean_reversion",
        version="0.1",
        parameters=(("window", "21"), ("displacement_atr_multiple", "2.0"),
                    ("min_history", "21"), ("expected_holding_horizon", "45")),
        target_model=VOLATILITY_MR_TARGET_MODEL)

    @financial
    def propose(self, *, candles: Sequence[Candle], quantity: Decimal = ZERO,
                cost_basis_mxn: Decimal = ZERO, market: str = "BTC/MXN") -> StrategyProposal:
        def build(decision: str, reason: str, entry: Decimal, target: Decimal,
                  features: VolatilityFeatures) -> StrategyProposal:
            gross = ((target - entry) / entry * BPS) if entry > ZERO and target > ZERO else ZERO
            return StrategyProposal(
                decision=decision, reason_code=reason, profile_id=self.identity.profile_id,
                strategy_id=self.identity.strategy_id, strategy_version=self.identity.version,
                strategy_fingerprint=self.identity.strategy_fingerprint, market=market,
                entry_reference_mxn=entry, expected_exit_reference_mxn=target,
                expected_gross_edge_bps=gross,
                expected_holding_horizon=self.expected_holding_horizon,
                target_model=self.target_model,
                confidence_evidence={"window": str(self.window),
                                     "displacement_atr_multiple": str(self.displacement_atr_multiple)},
                features=features, closed_candle_count=len(candles),
                position_open=quantity > ZERO)

        if not candles:
            return build(DECISION_NO_SIGNAL, NO_CLOSED_CANDLE, ZERO, ZERO,
                         VolatilityFeatures(0, ZERO, ZERO, ZERO, ZERO, ZERO))
        features = volatility_features(candles, window=self.window)
        if len(candles) < self.min_history:
            return build(DECISION_NO_SIGNAL, INSUFFICIENT_HISTORY, ZERO, ZERO, features)

        close = candles[-1].close
        displacement_bps = features.atr_bps * self.displacement_atr_multiple
        entry_boundary = features.mean_mxn * (ONE - displacement_bps / BPS)

        if quantity > ZERO and cost_basis_mxn > ZERO:
            average_cost = cost_basis_mxn / quantity
            # Reversion target: the mean is where the displacement closes.
            target = max(features.mean_mxn, self.target_model.target_price_mxn(average_cost, features))
            if close >= target:
                return build(DECISION_SELL, SIGNAL_SELL, close, target, features)
            return build(DECISION_NO_SIGNAL, EXIT_CONDITION_NOT_MET, close, target, features)

        if close <= entry_boundary:
            # Target the reversion, but never below the fee-aware target model:
            # reverting 4 bps in a market that costs 156 bps is not an opportunity.
            raw_reversion = features.mean_mxn
            scaled = self.target_model.target_price_mxn(close, features)
            target = max(raw_reversion, scaled)
            return build(DECISION_BUY, SIGNAL_BUY, close, target, features)
        return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, entry_boundary, features)


# ===========================================================================
# Registry
# ===========================================================================

CHAMPION_PROFILE: ProfileDefinition = ProfileDefinition(
    identity=MeanReversionSafeV1.identity, markets=CHAMPION_MARKETS,
    evaluator="MeanReversionSafeV1",
    description="Certified Champion, frozen. 20 bps target; structurally below the "
                "confirmed round-trip fee, so its BUYs are refused by the economic guard.")

TREND_PROFILE: ProfileDefinition = ProfileDefinition(
    identity=TrendContinuationV1.identity, markets=NO_CERTIFIED_MARKETS,
    evaluator="TrendContinuationV1",
    description="Research challenger. Follows an established trend with two independent "
                "confirmations; targets a volatility-scaled continuation.")

VOLATILITY_MR_PROFILE: ProfileDefinition = ProfileDefinition(
    identity=VolatilityAwareMeanReversionV1.identity, markets=NO_CERTIFIED_MARKETS,
    evaluator="VolatilityAwareMeanReversionV1",
    description="Research challenger. Mean reversion gated on volatility-scaled "
                "displacement, targeting the reversion.")

# The smallest defensible set: the Champion plus two challengers whose opportunity
# scale can realistically exceed friction. Ordering is stable and is NOT a ranking
# of quality -- nothing here is promoted by position.
PROFILE_REGISTRY: tuple[ProfileDefinition, ...] = (
    CHAMPION_PROFILE, TREND_PROFILE, VOLATILITY_MR_PROFILE,
)

CHALLENGER_PROFILES: tuple[ProfileDefinition, ...] = (TREND_PROFILE, VOLATILITY_MR_PROFILE)

PROFILE_BY_ID: dict[str, ProfileDefinition] = {p.profile_id: p for p in PROFILE_REGISTRY}


def evaluator_for(profile_id: str) -> Any:
    """Instantiate the evaluator for a registered profile.

    Raises for an unknown id rather than silently defaulting to the Champion: a
    typo must never cause trades to be admitted under the wrong strategy.
    """
    definition = PROFILE_BY_ID.get(profile_id)
    if definition is None:
        raise KeyError(f"UNKNOWN_STRATEGY_PROFILE:{profile_id}")
    return {"MeanReversionSafeV1": MeanReversionSafeV1,
            "TrendContinuationV1": TrendContinuationV1,
            "VolatilityAwareMeanReversionV1": VolatilityAwareMeanReversionV1}[definition.evaluator]()


@financial
def entry_and_exit_references(profile_id: str, *, candles: Sequence[Candle],
                              market: str = "BTC/MXN") -> tuple[Decimal, Decimal]:
    """Entry and expected-exit references a profile would use, for reporting."""
    proposal = evaluator_for(profile_id).propose(candles=candles, market=market)
    return proposal.entry_reference_mxn, proposal.expected_exit_reference_mxn

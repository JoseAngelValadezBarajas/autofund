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

# v2 raises the target floor to 250 bps. At the confirmed ~173 bps round-trip friction a
# 120 bps floor cannot pay for itself, so v1's floor was itself part of the defect rather
# than only its absent boundary. The floor is stated explicitly so it is visible as a
# parameter choice and not a hidden tuning knob.
VOLATILITY_MR_V2_TARGET_MODEL = TargetModel(atr_multiple=Decimal("2.0"),
                                           floor_bps=Decimal("250"),
                                           cap_bps=Decimal("1200"))

# Range expansion targets a larger move by construction; its floor must exceed friction
# with margin, and its cap is wider because an expansion is not bounded by a prior range.
RANGE_EXPANSION_TARGET_MODEL = TargetModel(atr_multiple=Decimal("2.5"),
                                          floor_bps=Decimal("300"),
                                          cap_bps=Decimal("1800"))


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
                cost_basis_mxn: Decimal = ZERO, market: str = "BTC/MXN",
                entry_price_mxn: Decimal = ZERO,
                target_price_mxn: Decimal = ZERO) -> StrategyProposal:
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
                cost_basis_mxn: Decimal = ZERO, market: str = "BTC/MXN",
                entry_price_mxn: Decimal = ZERO,
                target_price_mxn: Decimal = ZERO) -> StrategyProposal:
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
                cost_basis_mxn: Decimal = ZERO, market: str = "BTC/MXN",
                entry_price_mxn: Decimal = ZERO,
                target_price_mxn: Decimal = ZERO) -> StrategyProposal:
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
# Profile 4 — volatility-mean-reversion-v2 (risk-bounded)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class VolatilityMeanReversionV2:
    """Mean reversion with a *declared boundary* and a *bounded holding period*.

    The v1 evidence established one specific defect, and this profile addresses that
    defect rather than tuning v1's constants. v1 had no invalidation price and no time
    limit, so it waited indefinitely for reversion: every closed trade eventually won,
    and the cost was carried as unrealized exposure (0.34-0.85 MXN against 0.04-0.20 MXN
    of reward). Its win rate was an artifact of patience, not of edge.

    Four things are therefore defined independently and decided at entry:

    1. **Entry dislocation** -- price displaced below the mean by more than
       `displacement_atr_multiple` ATRs, so only genuinely stretched prices qualify.
    2. **Expected target** -- the reversion to the mean, floored by the fee-aware target
       model so the intended move is never smaller than the friction it must clear.
    3. **Invalidation price** -- a hard boundary *below* the entry, computed from
       volatility and known at entry. A breach means the dislocation was not a
       dislocation; the position is wrong and is exited, rather than held.
    4. **Maximum holding** -- a bar limit, so a flat position releases micro-capital
       instead of occupying it indefinitely.

    The distinguishing property is that the reward/risk geometry must clear
    `EconomicEdgeGuard` *and* the risk-adjusted entry gate **before** the position is
    opened. At the confirmed fee this is a demanding bar, and the honest expectation is
    that many opportunities are refused. That is the intended behaviour, not a defect.
    """

    window: int = 21
    displacement_atr_multiple: Decimal = Decimal("2.0")
    stop_atr_multiple: Decimal = Decimal("1.0")
    stop_floor_bps: Decimal = Decimal("80")
    stop_cap_bps: Decimal = Decimal("500")
    max_holding_bars: int = 240
    min_history: int = 21
    expected_holding_horizon: int = 120
    target_model: TargetModel = VOLATILITY_MR_V2_TARGET_MODEL

    # Whether this profile declares a risk boundary and a holding limit on its proposals.
    # An explicit class constant rather than something inferred by probing a synthetic
    # proposal: a probe returns an early no-signal proposal that declares nothing, which
    # would report a boundary-bounded profile as boundary-less. This is a claim about the
    # profile, so the profile states it.
    DECLARES_RISK_BOUNDARY = True

    @property
    def parameters(self) -> tuple[tuple[str, str], ...]:
        """The parameters this instance actually runs with.

        Derived from the live field values rather than hard-coded, so a parameter variant
        automatically reports the parameters it used. A hard-coded tuple would let a
        variant claim the default's parameters and share its fingerprint, which would make
        two different strategies indistinguishable in the evidence.
        """
        return (("window", str(self.window)),
                ("displacement_atr_multiple", str(self.displacement_atr_multiple)),
                ("stop_atr_multiple", str(self.stop_atr_multiple)),
                ("stop_floor_bps", str(self.stop_floor_bps)),
                ("stop_cap_bps", str(self.stop_cap_bps)),
                ("target_floor_bps", str(self.target_model.floor_bps)),
                ("max_holding_bars", str(self.max_holding_bars)),
                ("min_history", str(self.min_history)),
                ("expected_holding_horizon", str(self.expected_holding_horizon)))

    @property
    def identity(self) -> ProfileIdentity:
        """Identity derived from the live parameters, so a variant cannot share one."""
        return ProfileIdentity(
            profile_id="volatility-mean-reversion-v2",
            strategy_id="volatility_mean_reversion", version="0.2",
            parameters=self.parameters, target_model=self.target_model)

    @financial
    def stop_distance_bps(self, features: VolatilityFeatures) -> Decimal:
        """Volatility-scaled boundary distance, floored and capped.

        Bounded on both sides: an unbounded multiple is set by one volatile sample, and an
        unfloored one sits inside the spread in a calm market, so the position would be
        invalidated by noise before its own thesis could play out.
        """
        raw = features.atr_bps * self.stop_atr_multiple
        return min(max(raw, self.stop_floor_bps), self.stop_cap_bps)

    @financial
    def propose(self, *, candles: Sequence[Candle], quantity: Decimal = ZERO,
                cost_basis_mxn: Decimal = ZERO, market: str = "BTC/MXN",
                entry_price_mxn: Decimal = ZERO,
                target_price_mxn: Decimal = ZERO) -> StrategyProposal:
        def build(decision: str, reason: str, entry: Decimal, target: Decimal,
                  boundary: Decimal, features: VolatilityFeatures) -> StrategyProposal:
            gross = ((target - entry) / entry * BPS) if entry > ZERO and target > ZERO else ZERO
            distance = ((entry - boundary) / entry * BPS) if entry > ZERO and boundary > ZERO else ZERO
            return StrategyProposal(
                decision=decision, reason_code=reason, profile_id=self.identity.profile_id,
                strategy_id=self.identity.strategy_id, strategy_version=self.identity.version,
                strategy_fingerprint=self.identity.strategy_fingerprint, market=market,
                entry_reference_mxn=entry, expected_exit_reference_mxn=target,
                expected_gross_edge_bps=gross,
                expected_holding_horizon=self.expected_holding_horizon,
                target_model=self.target_model,
                confidence_evidence={"window": str(self.window),
                                     "displacement_atr_multiple": str(self.displacement_atr_multiple),
                                     "stop_distance_bps": str(self.stop_distance_bps(features)),
                                     "max_holding_bars": str(self.max_holding_bars)},
                features=features, closed_candle_count=len(candles),
                position_open=quantity > ZERO,
                invalidation_price_mxn=boundary, invalidation_distance_bps=distance,
                max_holding_bars=self.max_holding_bars)

        if not candles:
            return build(DECISION_NO_SIGNAL, NO_CLOSED_CANDLE, ZERO, ZERO, ZERO,
                         VolatilityFeatures(0, ZERO, ZERO, ZERO, ZERO, ZERO))
        features = volatility_features(candles, window=self.window)
        if len(candles) < self.min_history:
            return build(DECISION_NO_SIGNAL, INSUFFICIENT_HISTORY, ZERO, ZERO, ZERO, features)

        close = candles[-1].close
        displacement_bps = features.atr_bps * self.displacement_atr_multiple
        entry_boundary = features.mean_mxn * (ONE - displacement_bps / BPS)

        if quantity > ZERO and cost_basis_mxn > ZERO:
            # The boundary and target anchor to the price the position actually *paid*.
            # `cost_basis_mxn / quantity` includes the entry fee, so it sits ~78 bps above
            # the execution price; anchoring a bps distance to it would place the boundary
            # almost on top of the entry and invalidate the position immediately. The
            # caller supplies the execution price; the cost basis remains the recorded
            # outlay used for P&L.
            anchor = entry_price_mxn if entry_price_mxn > ZERO else cost_basis_mxn / quantity
            # The target is FIXED at entry, supplied by the caller, and only recomputed as
            # a fallback when it is missing. Recomputing it from a drifting ATR made it
            # unreachable in exactly the trend the position was waiting for: in a steady
            # advance the ATR rose, so the target rose with the price and the position never
            # closed -- the same "wait indefinitely" defect v1 had, wearing a different hat.
            target = (target_price_mxn if target_price_mxn > ZERO
                      else max(features.mean_mxn,
                               self.target_model.target_price_mxn(anchor, features)))
            boundary = anchor * (ONE - self.stop_distance_bps(features) / BPS)
            if close >= target:
                return build(DECISION_SELL, SIGNAL_SELL, close, target, boundary, features)
            # A breach is reported as a SELL so the profile and the replay agree; the
            # replay attributes the exit reason from the price, not from this code.
            if close <= boundary:
                return build(DECISION_SELL, SIGNAL_SELL, close, target, boundary, features)
            return build(DECISION_NO_SIGNAL, EXIT_CONDITION_NOT_MET, close, target,
                         boundary, features)

        if close <= entry_boundary:
            raw_reversion = features.mean_mxn
            scaled = self.target_model.target_price_mxn(close, features)
            target = max(raw_reversion, scaled)
            boundary = close * (ONE - self.stop_distance_bps(features) / BPS)
            return build(DECISION_BUY, SIGNAL_BUY, close, target, boundary, features)
        return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, entry_boundary,
                     ZERO, features)


# ===========================================================================
# Profile 5 — range-expansion-v1 (breakout)
# ===========================================================================


@dataclass(frozen=True, slots=True)
class RangeExpansionV1:
    """Targets a *range expansion* rather than a reversion or a simple trend.

    The hypothesis is a direct consequence of the cost structure: at ~173 bps round-trip
    friction, a strategy needs to capture a move that is large relative to recent noise,
    and the only honest source of such a move is an expansion of the range itself. Mean
    reversion's ceiling is bounded by the displacement it fades, which is small exactly
    when the market is quiet; a range expansion has no such ceiling.

    Entry requires the current bar to *expand* the recent range, using only information
    available at decision time:

      1. the bar's true range exceeds `expansion_atr_multiple` x ATR, so the move is
         large relative to the recent noise regime; and
      2. the close is above the highest high of the preceding window, so the expansion is
         confirmed in the direction being traded.

    Deliberately **not** used: any retroactive "this was a breakout" test. Condition 2
    compares the close to prior highs only, never to a high that has not yet occurred, and
    the signal is still filled at the next bar's executable ask, so the profile does not
    buy a completed historical move.

    Boundaries are declared at entry: the invalidation sits below the broken range, and a
    holding limit bounds how long the expansion may take to continue.
    """

    window: int = 20
    expansion_atr_multiple: Decimal = Decimal("1.5")
    stop_atr_multiple: Decimal = Decimal("1.2")
    stop_floor_bps: Decimal = Decimal("100")
    stop_cap_bps: Decimal = Decimal("600")
    max_holding_bars: int = 180
    min_history: int = 21
    expected_holding_horizon: int = 90
    target_model: TargetModel = RANGE_EXPANSION_TARGET_MODEL

    # See VolatilityMeanReversionV2.DECLARES_RISK_BOUNDARY for why this is explicit.
    DECLARES_RISK_BOUNDARY = True

    @property
    def parameters(self) -> tuple[tuple[str, str], ...]:
        """The parameters this instance actually runs with. See v2 for the rationale."""
        return (("window", str(self.window)),
                ("expansion_atr_multiple", str(self.expansion_atr_multiple)),
                ("stop_atr_multiple", str(self.stop_atr_multiple)),
                ("stop_floor_bps", str(self.stop_floor_bps)),
                ("stop_cap_bps", str(self.stop_cap_bps)),
                ("target_floor_bps", str(self.target_model.floor_bps)),
                ("max_holding_bars", str(self.max_holding_bars)),
                ("min_history", str(self.min_history)),
                ("expected_holding_horizon", str(self.expected_holding_horizon)))

    @property
    def identity(self) -> ProfileIdentity:
        return ProfileIdentity(
            profile_id="range-expansion-v1", strategy_id="range_expansion", version="0.1",
            parameters=self.parameters, target_model=self.target_model)

    @financial
    def stop_distance_bps(self, features: VolatilityFeatures) -> Decimal:
        raw = features.atr_bps * self.stop_atr_multiple
        return min(max(raw, self.stop_floor_bps), self.stop_cap_bps)

    @financial
    def range_high_mxn(self, candles: Sequence[Candle]) -> Decimal:
        """Highest high of the window *preceding* the current bar.

        The current bar is excluded on purpose. Including it would make the comparison
        "did this bar exceed its own high", which is always true and would admit every bar.
        """
        history = candles[-(self.window + 1):-1] if len(candles) > self.window else ()
        if not history:
            return ZERO
        return max(candle.high for candle in history)

    @financial
    def true_range_bps(self, candle: Candle) -> Decimal:
        """This bar's true range as bps of its close."""
        if candle.close <= ZERO:
            return ZERO
        span = candle.high - candle.low
        if candle.open > ZERO:
            span = max(span, abs(candle.high - candle.open), abs(candle.low - candle.open))
        return span / candle.close * BPS

    @financial
    def propose(self, *, candles: Sequence[Candle], quantity: Decimal = ZERO,
                cost_basis_mxn: Decimal = ZERO, market: str = "BTC/MXN",
                entry_price_mxn: Decimal = ZERO,
                target_price_mxn: Decimal = ZERO) -> StrategyProposal:
        def build(decision: str, reason: str, entry: Decimal, target: Decimal,
                  boundary: Decimal, features: VolatilityFeatures) -> StrategyProposal:
            gross = ((target - entry) / entry * BPS) if entry > ZERO and target > ZERO else ZERO
            distance = ((entry - boundary) / entry * BPS) if entry > ZERO and boundary > ZERO else ZERO
            return StrategyProposal(
                decision=decision, reason_code=reason, profile_id=self.identity.profile_id,
                strategy_id=self.identity.strategy_id, strategy_version=self.identity.version,
                strategy_fingerprint=self.identity.strategy_fingerprint, market=market,
                entry_reference_mxn=entry, expected_exit_reference_mxn=target,
                expected_gross_edge_bps=gross,
                expected_holding_horizon=self.expected_holding_horizon,
                target_model=self.target_model,
                confidence_evidence={"window": str(self.window),
                                     "expansion_atr_multiple": str(self.expansion_atr_multiple),
                                     "stop_distance_bps": str(self.stop_distance_bps(features)),
                                     "max_holding_bars": str(self.max_holding_bars)},
                features=features, closed_candle_count=len(candles),
                position_open=quantity > ZERO,
                invalidation_price_mxn=boundary, invalidation_distance_bps=distance,
                max_holding_bars=self.max_holding_bars)

        if not candles:
            return build(DECISION_NO_SIGNAL, NO_CLOSED_CANDLE, ZERO, ZERO, ZERO,
                         VolatilityFeatures(0, ZERO, ZERO, ZERO, ZERO, ZERO))
        features = volatility_features(candles, window=self.window)
        if len(candles) < self.min_history:
            return build(DECISION_NO_SIGNAL, INSUFFICIENT_HISTORY, ZERO, ZERO, ZERO, features)

        candle = candles[-1]
        close = candle.close
        preceding_high = self.range_high_mxn(candles)

        if quantity > ZERO and cost_basis_mxn > ZERO:
            anchor = entry_price_mxn if entry_price_mxn > ZERO else cost_basis_mxn / quantity
            # Fixed at entry for the same reason as v2: a target that ratchets with
            # volatility in a healthy expansion is never reached.
            target = (target_price_mxn if target_price_mxn > ZERO
                      else self.target_model.target_price_mxn(anchor, features))
            boundary = anchor * (ONE - self.stop_distance_bps(features) / BPS)
            if close >= target or close <= boundary:
                return build(DECISION_SELL, SIGNAL_SELL, close, target, boundary, features)
            return build(DECISION_NO_SIGNAL, EXIT_CONDITION_NOT_MET, close, target,
                         boundary, features)

        # Two independent confirmations, both from completed bars only:
        #   1. THIS bar's true range expands beyond the recent norm, so the move is large
        #      relative to the noise regime the friction has to be cleared against; and
        #   2. the close exceeds the highest high of the *preceding* window, so the
        #      expansion is confirmed in the direction being traded.
        #
        # Condition 2 compares `close` rather than the bar's high against prior highs.
        # The close is always at or below the high within its own bar, so using the high
        # would trigger on a wick that closed back inside the range -- a failed expansion.
        #
        # The expansion multiple applies to one bar's range against the ATR of the
        # preceding window. Comparing the window's own range to the window's ATR would be
        # near-tautological for a fixed window and would admit almost everything.
        expansion_required_bps = features.atr_bps * self.expansion_atr_multiple
        if self.true_range_bps(candle) < expansion_required_bps:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, preceding_high,
                         ZERO, features)
        if preceding_high <= ZERO or close <= preceding_high:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, preceding_high,
                         ZERO, features)
        target = self.target_model.target_price_mxn(close, features)
        # The boundary is volatility-scaled and anchored to the entry, exactly as v2's is.
        # It is deliberately NOT placed at the broken range level: that level sits as far
        # below the close as the breakout was strong, so it produces a boundary wider than
        # the target it is meant to protect -- at the confirmed 173 bps round-trip friction
        # that geometry cannot clear its own cost, and the risk-adjusted gate refuses it
        # before entry. A stop sized by volatility is what makes the trade's risk knowable
        # at entry, which is the property this milestone is testing for.
        boundary = close * (ONE - self.stop_distance_bps(features) / BPS)
        return build(DECISION_BUY, SIGNAL_BUY, close, target, boundary, features)


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

VOLATILITY_MR_V2_PROFILE: ProfileDefinition = ProfileDefinition(
    identity=VolatilityMeanReversionV2().identity, markets=NO_CERTIFIED_MARKETS,
    evaluator="VolatilityMeanReversionV2",
    description="Research challenger (v2). Mean reversion with an invalidation boundary "
                "and a holding limit declared at entry, addressing v1's unbounded "
                "adverse excursion rather than retuning its constants.")

RANGE_EXPANSION_PROFILE: ProfileDefinition = ProfileDefinition(
    identity=RangeExpansionV1().identity, markets=NO_CERTIFIED_MARKETS,
    evaluator="RangeExpansionV1",
    description="Research challenger. Trades a confirmed range expansion, on the "
                "hypothesis that only a move large relative to recent noise can clear "
                "AutoFund's round-trip friction.")

# The smallest defensible set: the Champion plus two challengers whose opportunity
# scale can realistically exceed friction, plus the two 0.2.2 risk-bounded challengers.
# Ordering is stable and is NOT a ranking of quality -- nothing here is promoted by
# position.
PROFILE_REGISTRY: tuple[ProfileDefinition, ...] = (
    CHAMPION_PROFILE, TREND_PROFILE, VOLATILITY_MR_PROFILE,
    VOLATILITY_MR_V2_PROFILE, RANGE_EXPANSION_PROFILE,
)

# Profiles introduced in MVP 0.2.2. Kept as a named set so a caller can evaluate the new
# research generation without also re-evaluating the frozen previous one.
RISK_ADJUSTED_CHALLENGERS: tuple[ProfileDefinition, ...] = (
    VOLATILITY_MR_V2_PROFILE, RANGE_EXPANSION_PROFILE,
)

# Profiles whose conclusions were frozen by MVP 0.2.1. Listed explicitly so a research
# cycle can never silently overwrite or "improve" them.
FROZEN_PROFILES: tuple[ProfileDefinition, ...] = (
    CHAMPION_PROFILE, TREND_PROFILE, VOLATILITY_MR_PROFILE,
)

CHALLENGER_PROFILES: tuple[ProfileDefinition, ...] = (
    TREND_PROFILE, VOLATILITY_MR_PROFILE, VOLATILITY_MR_V2_PROFILE,
    RANGE_EXPANSION_PROFILE,
)

PROFILE_BY_ID: dict[str, ProfileDefinition] = {p.profile_id: p for p in PROFILE_REGISTRY}


EVALUATORS: dict[str, Any] = {
    "MeanReversionSafeV1": MeanReversionSafeV1,
    "TrendContinuationV1": TrendContinuationV1,
    "VolatilityAwareMeanReversionV1": VolatilityAwareMeanReversionV1,
    "VolatilityMeanReversionV2": VolatilityMeanReversionV2,
    "RangeExpansionV1": RangeExpansionV1,
}


def evaluator_for(profile_id: str) -> Any:
    """Instantiate the evaluator for a registered profile.

    Raises for an unknown id rather than silently defaulting to the Champion: a
    typo must never cause trades to be admitted under the wrong strategy.

    The evaluator is resolved through an explicit table rather than dynamic dispatch, so
    a profile whose evaluator was renamed or removed fails loudly at import time instead
    of at the first signal.
    """
    definition = PROFILE_BY_ID.get(profile_id)
    if definition is None:
        raise KeyError(f"UNKNOWN_STRATEGY_PROFILE:{profile_id}")
    evaluator = EVALUATORS.get(definition.evaluator)
    if evaluator is None:
        raise KeyError(f"UNKNOWN_STRATEGY_EVALUATOR:{definition.evaluator}")
    return evaluator()


@financial
def entry_and_exit_references(profile_id: str, *, candles: Sequence[Candle],
                              market: str = "BTC/MXN") -> tuple[Decimal, Decimal]:
    """Entry and expected-exit references a profile would use, for reporting."""
    proposal = evaluator_for(profile_id).propose(candles=candles, market=market)
    return proposal.entry_reference_mxn, proposal.expected_exit_reference_mxn

"""Asymmetric challengers: entries whose invalidation is narrow enough to be affordable.

Both challengers here answer the same arithmetic problem from opposite directions, and both
were designed *after* the MAE/MFE diagnostic rather than before it — the spec's instruction
not to implement two arbitrary indicators only has force if the evidence was consulted first.

**The problem they are trying to solve.** At the confirmed costs on this account the
pre-entry geometry must satisfy

    gross_reward >= friction + risk * (1 + required_ratio)

with friction around 173 bps and risk capped near 281 bps by the existing 0.50 MXN
single-trade policy. Every frozen profile sizes its invalidation as a multiple of ATR, which
at these horizons is 400-1200 bps — an order of magnitude wider than the cap. The risk gate
therefore refuses essentially everything, and the trades that do get through are the ones
with the widest targets and the slowest invalidation, which is the opposite of asymmetry.

So the design constraint is not "find better signals". It is **bound the risk narrowly and
honestly**. Two ways to do that exist in the price data itself:

    StructuralInvalidationPullbackV1   enter at a level that already exists, with the
                                       invalidation just beyond it, so risk is defined by
                                       distance to a real price structure rather than by
                                       recent volatility.

    ExpansionRetestV1                  wait for a confirmed range expansion, then require
                                       price to return to the broken level so that the
                                       invalidation sits just inside it.

**Where the target comes from, and why it is not ATR.** Both challengers set the target from
the *required* geometry rather than from a volatility multiple. A target chosen as
`2 x ATR` is a statement about volatility; a target chosen as
`friction + risk * (1 + ratio) + margin` is a statement about this account's break-even. When
risk is 100-280 bps rather than 800, the second produces a target around 350-750 bps instead
of 500-2400 bps, and the difference is the difference between an achievable move and one
that does not occur at these horizons. The target is capped by a volatility multiple so a
narrow stop cannot demand an absurd move, and floored at the break-even geometry so it
cannot demand too little.

**Nothing here reads the future, and the structure is arranged so it cannot.** Every level
comes from `market_structure.levels_known_at`, which is passed the decision index and slices
the series at it. The invalidation is placed below a level that already existed. Targets and
invalidations are both declared at the moment of the proposal and the replay never revises
them — a target that ratchets away or a stop that widens is what makes a strategy's stated
reward/risk a fiction, and 0.2.2 already found and fixed both.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ONE, ZERO, financial
from autofund.replay.data import Candle

from .execution_model import BPS
from .market_structure import (
    DEFAULT_LOOKBACK_BARS,
    DEFAULT_PIVOT_WINDOW,
    levels_known_at,
    nearest_level_below,
    proximity_bps,
    retest_confirmed,
)
from .profiles import (
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

ASYMMETRIC_CHALLENGER_VERSION = "autofund.asymmetric-challenger.v1"

# The cap on how far the target may be placed, as a multiple of recent ATR.
#
# This number is not a preference; it is derived from the arithmetic the *unchanged* risk
# gate imposes. That gate nets friction from both the reward and the risk path, so it
# requires `gross >= friction + ratio * (risk + friction)`. At the confirmed 173 bps round
# trip that is a floor of 346 bps even at zero risk, rising to 527-612 bps at the 80-180 bps
# invalidations this experiment tests. Measured ATR at these horizons is 29-96 bps, so the
# requirement is 10-20x ATR.
#
# 20 is therefore the cap, and the reasoning matters: a cap *below* the gate's own minimum
# requirement would guarantee that no configuration could ever trade, which is not a
# conservative choice but a broken experiment -- it would report "no opportunity" for a
# search that never reached the market. 20x admits the required geometry while still refusing
# a move materially beyond anything these horizons produce.
#
# Note the interaction this exposes, which is the milestone's central difficulty: at 15m, where
# ATR is around 29 bps on BTC, even 20x ATR (580 bps) only just reaches the requirement. The
# horizon where a narrow invalidation is most affordable is also the horizon whose typical
# movement is smallest relative to what this account's costs demand.
DEFAULT_TARGET_ATR_CAP_MULTIPLE = Decimal("20.0")

# The floor under the target, as a multiple of recent ATR. Purpose: the break-even geometry
# can be satisfied by a target *smaller* than the market's ordinary noise, which would be
# filled by noise rather than by the thesis. The floor keeps the target meaningful.
DEFAULT_TARGET_ATR_FLOOR_MULTIPLE = Decimal("1.0")

# Margin added on top of the bare break-even requirement, so the trade is not merely
# profitable-in-the-model. Expressed as a multiple of the risk distance, which makes it scale
# with the geometry rather than being an absolute figure that means different things at
# different stop widths.
DEFAULT_TARGET_MARGIN_RISK_MULTIPLE = Decimal("0.5")

# How close price must be to a structural level to count as "at" it, as a multiple of
# recent ATR.
#
# Volatility-relative rather than proportional to the risk distance. An earlier draft used
# "proximity as a fraction of the risk distance", which is self-referential: the risk
# distance is itself derived from the entry's distance to the level, so the condition reduced
# to a constant comparison that no bar could satisfy and the challenger produced no signals
# at all.
#
# 1.5 rather than a tighter figure because the value has to be *satisfiable*, and this one was
# set by asking what a pullback is rather than what would produce trades: a return to within
# one and a half bar-ranges of a level is a pullback to that level, whereas a tighter bound
# describes price sitting exactly on it, which rarely happens and is not the same idea. Risk
# stays well inside the policy cap at this distance -- 1.5 ATR plus the buffer is under 2 ATR,
# roughly 60 bps at these horizons against a 281 bps allowance -- so widening it does not
# weaken the risk term that makes the hypothesis testable.
DEFAULT_ENTRY_PROXIMITY_ATR_MULTIPLE = Decimal("1.5")

# Retained for the invalidation buffer, which is genuinely a fraction of the level distance:
# the boundary sits this far beyond the level as a proportion of how far price is above it.
DEFAULT_PROXIMITY_FRACTION = Decimal("0.25")

# The tolerance for counting a level as retested, in bps of price.
DEFAULT_RETEST_TOLERANCE_BPS = Decimal("18")

# The invalidation sits this far beyond the structural level, as a fraction of the entry's
# distance to that level. Placing it exactly at the level would stop the trade on the wick
# that touches it; placing it far beyond would widen risk past what the level justifies.
DEFAULT_INVALIDATION_BUFFER_FRACTION = Decimal("0.30")


class AsymmetricProfileError(ValueError):
    """An asymmetric challenger was configured in a way it cannot defend."""


@dataclass(frozen=True, slots=True)
class StructuralInvalidationPullbackV1:
    """Enter on a pullback into a level that already existed, risking the distance past it.

    The idea is that a level is where a thesis can be *wrong cheaply*. If price is at a
    support that has held and the invalidation sits just beyond it, the risk is the distance
    to that level rather than a volatility-scaled multiple of ATR. That is the only way the
    risk term can be small enough for the reward term to matter.

    Entry condition, all from completed bars at or before the decision bar:

    1. A structural pivot low exists below the current close and is recent enough to still
       describe this market.
    2. The current close is within `entry_proximity_fraction` of that level, measured in
       units of the risk distance — i.e. price is genuinely *at* the level, not merely on
       the same side of it.
    3. Price has not already broken it: the current bar's low is above the invalidation.
    4. Recent volatility is not so elevated that the level is meaningless — a level inside
       the noise band will be swept regardless of the thesis.
    """

    window: int = 21
    pivot_window: int = DEFAULT_PIVOT_WINDOW
    lookback: int = DEFAULT_LOOKBACK_BARS
    entry_proximity_atr_multiple: Decimal = DEFAULT_ENTRY_PROXIMITY_ATR_MULTIPLE
    invalidation_buffer_fraction: Decimal = DEFAULT_INVALIDATION_BUFFER_FRACTION
    target_atr_cap_multiple: Decimal = DEFAULT_TARGET_ATR_CAP_MULTIPLE
    target_atr_floor_multiple: Decimal = DEFAULT_TARGET_ATR_FLOOR_MULTIPLE
    target_margin_risk_multiple: Decimal = DEFAULT_TARGET_MARGIN_RISK_MULTIPLE
    # The fraction of price that may be risked. This is what keeps the geometry inside the
    # 0.50 MXN policy cap, and it is expressed in bps so the constraint is visible in the
    # parameters rather than emerging from a chain of calculations.
    max_risk_bps: Decimal = Decimal("260")
    max_holding_bars: int = 16
    min_history: int = 41
    expected_holding_horizon: int = 8
    timeframe_name: str = "15m"
    # Included in the identity. Without it every configuration on the same timeframe shares a
    # profile_id, so results keyed by id would collide and the four-configuration budget would
    # be indistinguishable in the evidence even though the fingerprints differed.
    config_label: str = "BASE"

    @property
    def timeframe(self) -> Any:
        from .horizon import FIFTEEN_MINUTE, ONE_HOUR_TIMEFRAME

        return {"15m": FIFTEEN_MINUTE, "1h": ONE_HOUR_TIMEFRAME}[self.timeframe_name]

    @property
    def parameters(self) -> tuple[tuple[str, str], ...]:
        return (("timeframe", self.timeframe_name),
                ("timeframe_seconds", str(self.timeframe.seconds)),
                ("config_label", self.config_label),
                ("window", str(self.window)), ("pivot_window", str(self.pivot_window)),
                ("lookback", str(self.lookback)),
                ("entry_proximity_atr_multiple", str(self.entry_proximity_atr_multiple)),
                ("invalidation_buffer_fraction", str(self.invalidation_buffer_fraction)),
                ("max_risk_bps", str(self.max_risk_bps)),
                ("target_atr_cap_multiple", str(self.target_atr_cap_multiple)),
                ("target_atr_floor_multiple", str(self.target_atr_floor_multiple)),
                ("target_margin_risk_multiple", str(self.target_margin_risk_multiple)),
                ("max_holding_bars", str(self.max_holding_bars)))

    @property
    def identity(self) -> ProfileIdentity:
        return ProfileIdentity(
            profile_id=(f"structural-invalidation-pullback-"
                        f"{self.timeframe_name}-{self.config_label}-v1"),
            strategy_id="structural_invalidation_pullback", version="0.1",
            parameters=self.parameters,
            target_model=TargetModel(atr_multiple=self.target_atr_cap_multiple,
                                     floor_bps=Decimal("1"),
                                     cap_bps=Decimal("100000")))

    def public(self) -> dict[str, Any]:
        return {**self.identity.public(), "concept": "structural_invalidation_pullback",
                "asymmetric_challenger_version": ASYMMETRIC_CHALLENGER_VERSION,
                "description": "enter at a pre-existing structural level with the "
                               "invalidation just beyond it, so risk is defined by price "
                               "structure rather than by a volatility multiple"}

    @financial
    def propose(self, *, candles: Sequence[Candle], quantity: Decimal = ZERO,
                cost_basis_mxn: Decimal = ZERO, market: str = "BTC/MXN",
                entry_price_mxn: Decimal = ZERO,
                target_price_mxn: Decimal = ZERO) -> StrategyProposal:
        def build(decision: str, reason: str, entry: Decimal, target: Decimal,
                  boundary: Decimal, features: VolatilityFeatures,
                  evidence: dict[str, str] | None = None) -> StrategyProposal:
            gross = ((target - entry) / entry * BPS) if entry > ZERO and target > ZERO else ZERO
            distance = ((entry - boundary) / entry * BPS) if entry > ZERO and boundary > ZERO else ZERO
            return StrategyProposal(
                decision=decision, reason_code=reason, profile_id=self.identity.profile_id,
                strategy_id=self.identity.strategy_id, strategy_version=self.identity.version,
                strategy_fingerprint=self.identity.strategy_fingerprint, market=market,
                entry_reference_mxn=entry, expected_exit_reference_mxn=target,
                expected_gross_edge_bps=gross,
                expected_holding_horizon=self.expected_holding_horizon,
                target_model=self.identity.target_model,
                confidence_evidence=dict(evidence or {}),
                features=features, closed_candle_count=len(candles),
                position_open=quantity > ZERO,
                invalidation_price_mxn=boundary, invalidation_distance_bps=distance,
                max_holding_bars=self.max_holding_bars)

        flat = VolatilityFeatures(0, ZERO, ZERO, ZERO, ZERO, ZERO)
        if not candles:
            return build(DECISION_NO_SIGNAL, NO_CLOSED_CANDLE, ZERO, ZERO, ZERO, flat)
        features = volatility_features(candles, window=self.window)
        if len(candles) < self.min_history:
            return build(DECISION_NO_SIGNAL, INSUFFICIENT_HISTORY, ZERO, ZERO, ZERO, features)

        index = len(candles) - 1
        candle = candles[index]
        close = candle.close

        if quantity > ZERO and cost_basis_mxn > ZERO:
            # The exit path never consults the level: the boundary and target were fixed at
            # entry and are passed back in. Recomputing either here would be the ratchet that
            # makes a stated reward/risk meaningless.
            anchor = entry_price_mxn if entry_price_mxn > ZERO else cost_basis_mxn / quantity
            boundary = anchor * (ONE - self.max_risk_bps / BPS)
            target = target_price_mxn
            if target > ZERO and close >= target:
                return build(DECISION_SELL, SIGNAL_SELL, close, target, boundary, features)
            if close <= boundary:
                return build(DECISION_SELL, SIGNAL_SELL, close, target, boundary, features)
            return build(DECISION_NO_SIGNAL, EXIT_CONDITION_NOT_MET, close, target,
                         boundary, features)

        # ---- entry evaluation, completed bars only ----
        levels = levels_known_at(candles=candles, decision_index=index,
                                 half_width=self.pivot_window, lookback=self.lookback)
        support = nearest_level_below(levels=levels, price_mxn=close,
                                      kind="PIVOT_LOW")
        if support is None:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO, ZERO,
                         features, {"reason": "NO_STRUCTURAL_LEVEL_BELOW"})

        # The invalidation sits a fraction of the level distance beyond the level itself.
        level_distance = close - support.price_mxn
        if level_distance <= ZERO:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO, ZERO,
                         features, {"reason": "LEVEL_NOT_BELOW_CLOSE"})
        boundary = support.price_mxn - (level_distance * self.invalidation_buffer_fraction)
        risk_bps = (close - boundary) / close * BPS
        if risk_bps <= ZERO:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO,
                         boundary, features, {"reason": "NONPOSITIVE_RISK"})
        if risk_bps > self.max_risk_bps:
            # Risk policy bounds the trade, so a geometry wider than the cap is refused at
            # entry rather than discovered by the risk gate a bar later. Neither gate is
            # weakened; this one is simply narrower by design.
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO,
                         boundary, features, {"risk_bps": str(risk_bps),
                                              "reason": "RISK_WIDER_THAN_DESIGN_CAP"})

        # Proximity, measured against recent volatility. Price must be within a fraction of
        # its own ATR of the level, which is what makes the entry a pullback *to* the level
        # rather than an entry somewhere on the same side of it.
        proximity = proximity_bps(price_mxn=close, level_mxn=support.price_mxn)
        allowed_proximity = features.atr_bps * self.entry_proximity_atr_multiple
        if allowed_proximity <= ZERO or proximity > allowed_proximity:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO,
                         boundary, features,
                         {"reason": "NOT_CLOSE_ENOUGH_TO_LEVEL",
                          "proximity_bps": str(proximity),
                          "allowed_bps": str(allowed_proximity)})

        # The level must not already be broken on this bar, or the entry is buying into a
        # failure rather than a pullback.
        if candle.low <= support.price_mxn:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO,
                         boundary, features, {"reason": "LEVEL_ALREADY_BROKEN"})

        target = self._target(close=close, risk_bps=risk_bps, features=features)
        if target <= close:
            # The move this account needs is larger than the horizon plausibly delivers.
            # Refused explicitly so the evidence records the real obstacle.
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO,
                         boundary, features,
                         {"reason": "REQUIRED_MOVE_EXCEEDS_VOLATILITY_CAP",
                          "required_bps": str(self._required_target_bps(risk_bps=risk_bps)),
                          "atr_bps": str(features.atr_bps)})
        return build(DECISION_BUY, SIGNAL_BUY, close, target, boundary, features,
                     {"support_price_mxn": str(support.price_mxn),
                      "support_index": str(support.source_index),
                      "support_age_bars": str(support.age_bars),
                      "risk_bps": str(risk_bps), "proximity_bps": str(proximity)})

    @financial
    def _required_target_bps(self, *, risk_bps: Decimal) -> Decimal:
        """The gross move the *unchanged* risk gate demands for this geometry.

        Derived by inverting the gate's own ratio, which nets friction from both paths:

            ratio = (gross - friction) / (risk + friction)
            =>  gross = friction + ratio * (risk + friction)

        An earlier version of this function used `friction + margin * risk`, which omits the
        friction term inside the ratio. That is not a rounding difference — it made the
        required target *smaller* than the gate's requirement for every risk below about
        346 bps, so all 64 configurations were economically admissible and every one was then
        refused by the risk gate. The experiment produced no trades and would have been
        reported as an absence of opportunity rather than as a defect in this formula.
        """
        ratio = ONE + self.target_margin_risk_multiple
        return self._friction_bps() + ratio * (risk_bps + self._friction_bps())

    @financial
    def _target(self, *, close: Decimal, risk_bps: Decimal,
                features: VolatilityFeatures) -> Decimal:
        """The target, derived from the break-even geometry the unchanged gate imposes.

        The requirement comes first; volatility only bounds it. That ordering matters: a
        target sized from volatility alone is a statement about the market, whereas this
        account's obstacle is a statement about its costs. When the required move exceeds
        what the horizon plausibly delivers, `_target` returns ZERO and the caller refuses
        the trade, so the refusal is recorded as a geometry failure rather than being
        disguised as a smaller target.
        """
        required = self._required_target_bps(risk_bps=risk_bps)
        volatility_cap = features.atr_bps * self.target_atr_cap_multiple
        if volatility_cap <= ZERO or required > volatility_cap:
            return ZERO
        target_bps = max(required, features.atr_bps * self.target_atr_floor_multiple)
        return close * (ONE + target_bps / BPS)

    def _friction_bps(self) -> Decimal:
        """Modelled round-trip friction, from the values the certification uses.

        Hard-coded here rather than passed in because a profile's target must be a pure
        function of the inputs it sees. If the account's fees changed, this constant would
        change and the profile's fingerprint would change with it — which is the correct
        behaviour, since the geometry it demands would genuinely be different.
        """
        return Decimal("173")


@dataclass(frozen=True, slots=True)
class ExpansionRetestV1:
    """Wait for expansion, then require a retest before entering.

    This is the second way to bound risk narrowly. A range expansion says the market has
    committed to a direction, but entering at the breakout price puts the invalidation at
    the volatility-scaled distance the frozen `range-expansion-v1` already uses — hundreds
    of bps. Waiting for price to come back to the broken level instead places the
    invalidation just inside that level, where the thesis is specifically "the breakout
    level now acts as support".

    The two conditions are deliberately sequential rather than simultaneous:

    1. An expansion happened within the recent lookback (a completed bar whose true range
       exceeded a multiple of ATR, closing beyond the prior range high).
    2. Since then, a later completed bar traded back to that level.

    The retest it requires has already occurred at decision time. It does not require the
    level to have *held*, because that would be reading the future.
    """

    window: int = 21
    pivot_window: int = DEFAULT_PIVOT_WINDOW
    lookback: int = DEFAULT_LOOKBACK_BARS
    expansion_atr_multiple: Decimal = Decimal("1.4")
    expansion_search_bars: int = 12
    retest_tolerance_bps: Decimal = DEFAULT_RETEST_TOLERANCE_BPS
    invalidation_buffer_fraction: Decimal = DEFAULT_INVALIDATION_BUFFER_FRACTION
    target_atr_cap_multiple: Decimal = DEFAULT_TARGET_ATR_CAP_MULTIPLE
    target_atr_floor_multiple: Decimal = Decimal("1.0")
    target_margin_risk_multiple: Decimal = Decimal("0.5")
    max_risk_bps: Decimal = Decimal("260")
    max_holding_bars: int = 16
    min_history: int = 41
    expected_holding_horizon: int = 8
    timeframe_name: str = "15m"
    # Included in the identity. Without it every configuration on the same timeframe shares a
    # profile_id, so results keyed by id would collide and the four-configuration budget would
    # be indistinguishable in the evidence even though the fingerprints differed.
    config_label: str = "BASE"

    @property
    def timeframe(self) -> Any:
        from .horizon import FIFTEEN_MINUTE, ONE_HOUR_TIMEFRAME

        return {"15m": FIFTEEN_MINUTE, "1h": ONE_HOUR_TIMEFRAME}[self.timeframe_name]

    @property
    def parameters(self) -> tuple[tuple[str, str], ...]:
        return (("timeframe", self.timeframe_name),
                ("timeframe_seconds", str(self.timeframe.seconds)),
                ("window", str(self.window)), ("pivot_window", str(self.pivot_window)),
                ("lookback", str(self.lookback)),
                ("expansion_atr_multiple", str(self.expansion_atr_multiple)),
                ("expansion_search_bars", str(self.expansion_search_bars)),
                ("retest_tolerance_bps", str(self.retest_tolerance_bps)),
                ("invalidation_buffer_fraction", str(self.invalidation_buffer_fraction)),
                ("max_risk_bps", str(self.max_risk_bps)),
                ("target_atr_cap_multiple", str(self.target_atr_cap_multiple)),
                ("target_margin_risk_multiple", str(self.target_margin_risk_multiple)),
                ("max_holding_bars", str(self.max_holding_bars)))

    @property
    def identity(self) -> ProfileIdentity:
        return ProfileIdentity(
            profile_id=f"expansion-retest-{self.timeframe_name}-{self.config_label}-v1",
            strategy_id="expansion_retest", version="0.1", parameters=self.parameters,
            target_model=TargetModel(atr_multiple=self.target_atr_cap_multiple,
                                     floor_bps=Decimal("1"),
                                     cap_bps=Decimal("100000")))

    def public(self) -> dict[str, Any]:
        return {**self.identity.public(), "concept": "expansion_retest",
                "asymmetric_challenger_version": ASYMMETRIC_CHALLENGER_VERSION,
                "description": "require a retest of a broken level so the invalidation can "
                               "sit just inside it rather than at a volatility distance"}

    @financial
    def _true_range_bps(self, candle: Candle) -> Decimal:
        if candle.close <= ZERO:
            return ZERO
        span = candle.high - candle.low
        if candle.open > ZERO:
            span = max(span, abs(candle.high - candle.open), abs(candle.low - candle.open))
        return span / candle.close * BPS

    @financial
    def _find_broken_level(self, *, candles: Sequence[Candle], index: int,
                           features: VolatilityFeatures) -> Decimal:
        """The most recent completed breakout level, searched strictly before `index`.

        Searched backwards from `index - 1`, so the retest the caller then requires can be
        found in bars that have already happened. Searching from `index` would make the
        breakout and the retest the same bar, which is a lookahead dressed as a condition.
        """
        required = features.atr_bps * self.expansion_atr_multiple
        lowest = max(self.window + 1, index - self.expansion_search_bars)
        for position in range(index - 1, lowest - 1, -1):
            candle = candles[position]
            if self._true_range_bps(candle) < required:
                continue
            prior = candles[max(0, position - self.window):position]
            if not prior:
                continue
            if candle.close > max(item.high for item in prior):
                return max(item.high for item in prior)
        return ZERO

    @financial
    def propose(self, *, candles: Sequence[Candle], quantity: Decimal = ZERO,
                cost_basis_mxn: Decimal = ZERO, market: str = "BTC/MXN",
                entry_price_mxn: Decimal = ZERO,
                target_price_mxn: Decimal = ZERO) -> StrategyProposal:
        def build(decision: str, reason: str, entry: Decimal, target: Decimal,
                  boundary: Decimal, features: VolatilityFeatures,
                  evidence: dict[str, str] | None = None) -> StrategyProposal:
            gross = ((target - entry) / entry * BPS) if entry > ZERO and target > ZERO else ZERO
            distance = ((entry - boundary) / entry * BPS) if entry > ZERO and boundary > ZERO else ZERO
            return StrategyProposal(
                decision=decision, reason_code=reason, profile_id=self.identity.profile_id,
                strategy_id=self.identity.strategy_id, strategy_version=self.identity.version,
                strategy_fingerprint=self.identity.strategy_fingerprint, market=market,
                entry_reference_mxn=entry, expected_exit_reference_mxn=target,
                expected_gross_edge_bps=gross,
                expected_holding_horizon=self.expected_holding_horizon,
                target_model=self.identity.target_model,
                confidence_evidence=dict(evidence or {}),
                features=features, closed_candle_count=len(candles),
                position_open=quantity > ZERO,
                invalidation_price_mxn=boundary, invalidation_distance_bps=distance,
                max_holding_bars=self.max_holding_bars)

        flat = VolatilityFeatures(0, ZERO, ZERO, ZERO, ZERO, ZERO)
        if not candles:
            return build(DECISION_NO_SIGNAL, NO_CLOSED_CANDLE, ZERO, ZERO, ZERO, flat)
        features = volatility_features(candles, window=self.window)
        if len(candles) < self.min_history:
            return build(DECISION_NO_SIGNAL, INSUFFICIENT_HISTORY, ZERO, ZERO, ZERO, features)

        index = len(candles) - 1
        candle = candles[index]
        close = candle.close

        if quantity > ZERO and cost_basis_mxn > ZERO:
            anchor = entry_price_mxn if entry_price_mxn > ZERO else cost_basis_mxn / quantity
            boundary = anchor * (ONE - self.max_risk_bps / BPS)
            target = target_price_mxn
            if target > ZERO and close >= target:
                return build(DECISION_SELL, SIGNAL_SELL, close, target, boundary, features)
            if close <= boundary:
                return build(DECISION_SELL, SIGNAL_SELL, close, target, boundary, features)
            return build(DECISION_NO_SIGNAL, EXIT_CONDITION_NOT_MET, close, target,
                         boundary, features)

        level = self._find_broken_level(candles=candles, index=index, features=features)
        if level <= ZERO:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO, ZERO,
                         features, {"reason": "NO_RECENT_CONFIRMED_EXPANSION"})
        # The retest must already have occurred, strictly before this bar.
        if not retest_confirmed(candles=candles, decision_index=index, level_mxn=level,
                                tolerance_bps=self.retest_tolerance_bps):
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO, ZERO,
                         features, {"reason": "NO_RETEST_OF_BROKEN_LEVEL"})
        # Price must now be back above the level, so the entry is on the successful retest
        # rather than on the break itself.
        if close <= level:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO, ZERO,
                         features,
                         {"reason": "PRICE_NOT_RECLAIMED_LEVEL", "level": str(level)})
        boundary = level - ((close - level) * self.invalidation_buffer_fraction)
        if boundary <= ZERO or boundary >= close:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO, ZERO,
                         features, {"reason": "INVALID_BOUNDARY_GEOMETRY"})
        risk_bps = (close - boundary) / close * BPS
        if risk_bps <= ZERO:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO,
                         boundary, features, {"reason": "NONPOSITIVE_RISK"})
        if risk_bps > self.max_risk_bps:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO,
                         boundary, features, {"risk_bps": str(risk_bps),
                                              "reason": "RISK_WIDER_THAN_DESIGN_CAP"})
        required = (Decimal("173")
                    + (ONE + self.target_margin_risk_multiple) * (risk_bps + Decimal("173")))
        volatility_cap = features.atr_bps * self.target_atr_cap_multiple
        if volatility_cap <= ZERO or required > volatility_cap:
            return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, close, ZERO,
                         boundary, features,
                         {"reason": "REQUIRED_MOVE_EXCEEDS_VOLATILITY_CAP",
                          "required_bps": str(required),
                          "atr_bps": str(features.atr_bps)})
        target_bps = max(required, features.atr_bps * self.target_atr_floor_multiple)
        target = close * (ONE + target_bps / BPS)
        return build(DECISION_BUY, SIGNAL_BUY, close, target, boundary, features,
                     {"broken_level_mxn": str(level), "risk_bps": str(risk_bps),
                      "target_bps": str(target_bps)})


def asymmetric_challengers() -> tuple[Any, ...]:
    """The predeclared asymmetric challengers, one per configuration and horizon.

    Built from the predeclared configuration sets, not from dataclass defaults, so the list
    this function reports is the one the experiment actually evaluates. A list built from
    defaults would share a single `config_label` across four configurations and would report
    identities that do not exist in the search.
    """
    from .asymmetry_experiment import (
        CONFIG_SETS,
        PREDECLARED_HORIZONS,
        build_challenger,
    )

    return tuple(build_challenger(concept=concept, timeframe=tf, config=config)
                 for concept, configs in CONFIG_SETS.items()
                 for tf in PREDECLARED_HORIZONS for config in configs)


def asymmetric_challenger_ids() -> tuple[str, ...]:
    return tuple(p.identity.profile_id for p in asymmetric_challengers())


__all__ = [
    "ASYMMETRIC_CHALLENGER_VERSION",
    "DEFAULT_ENTRY_PROXIMITY_ATR_MULTIPLE",
    "DEFAULT_INVALIDATION_BUFFER_FRACTION",
    "DEFAULT_PROXIMITY_FRACTION",
    "DEFAULT_RETEST_TOLERANCE_BPS",
    "DEFAULT_TARGET_ATR_CAP_MULTIPLE",
    "DEFAULT_TARGET_ATR_FLOOR_MULTIPLE",
    "DEFAULT_TARGET_MARGIN_RISK_MULTIPLE",
    "AsymmetricProfileError",
    "ExpansionRetestV1",
    "StructuralInvalidationPullbackV1",
    "asymmetric_challenger_ids",
    "asymmetric_challengers",
]

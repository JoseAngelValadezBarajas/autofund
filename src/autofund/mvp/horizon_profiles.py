"""New versioned profiles that test the *same hypotheses* on coarser horizons.

The point of this module is what it does **not** do. It does not introduce a new strategy
family, and it does not retune the existing ones. MVP 0.2.3 ended with a quantified
obstacle — roughly 173 bps of round-trip friction against 1-minute moves that are a
fraction of that — and the honest next question is whether the *horizon* is the problem
rather than the idea.

So these profiles reuse the two concepts that already have stated hypotheses and known
failure modes:

    volatility mean reversion   (fade a volatility-scaled dislocation)
    range expansion             (trade a confirmed expansion of the recent range)

and change only the horizon they observe. The decision logic is inherited verbatim from
the frozen implementations; identities are new, so fingerprints differ and the previous
evidence remains untouched and attributable.

**Why coarser bars might help, mechanically.** Both profiles size their target as a
multiple of observed ATR. A 1-hour bar's range is much larger than a 1-minute bar's, so
the *same* multiple of ATR produces a materially larger target in bps at a coarser
horizon. Friction does not scale with the horizon at all. If that mechanism is real, the
friction-to-opportunity ratio should fall sharply with horizon, and that ratio is the
single most informative number this milestone produces.

**Why it might not help.** A larger target is only worth having if it is *reached*. Coarser
bars also mean fewer opportunities, longer capital occupation, and targets that are further
away in wall-clock time. The capital-hours metric exists to make that cost visible, because
a strategy that earns more per trade while locking capital far longer is not obviously
better and must not be reported as such.

**Parameters are explicit and horizon-scaled.** Nothing is inherited implicitly: each
horizon states its own window, holding limit and target floor, so a reader can see exactly
what was tested rather than inferring it from a default.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial
from autofund.replay.data import Candle

from .horizon import FIFTEEN_MINUTE, ONE_HOUR_TIMEFRAME, TimeframeSpec
from .profile_library import RangeExpansionV1, VolatilityMeanReversionV2
from .profiles import (
    ProfileIdentity,
    StrategyProposal,
    TargetModel,
    VolatilityFeatures,
)

HORIZON_PROFILE_VERSION = "autofund.horizon-profile.v1"

# Declared once and shared between each profile's identity and its decision logic, for the
# same reason the frozen profiles do it: the fingerprint must describe the parameters the
# evaluator actually uses, and a per-instance TargetModel would let the two drift apart.
MR_HORIZON_TARGET_MODEL = TargetModel(
    atr_multiple=Decimal("2.0"), floor_bps=Decimal("250"), cap_bps=Decimal("1200"))
RANGE_HORIZON_TARGET_MODEL = TargetModel(
    atr_multiple=Decimal("2.5"), floor_bps=Decimal("300"), cap_bps=Decimal("1800"))

_MEAN_REVERSION_DOC = """Volatility mean reversion evaluated on a coarser horizon.

Inherits the v2 decision logic exactly: entry requires a volatility-scaled dislocation,
the target is the reversion floored by the fee-aware target model, the invalidation
boundary and holding limit are declared at entry and never revised. Only the horizon, and
therefore the wall-clock meaning of every window and holding limit, differs.
"""

_RANGE_EXPANSION_DOC = """Range expansion evaluated on a coarser horizon.

Inherits the range-expansion decision logic exactly: entry requires the current bar's true
range to exceed a multiple of the recent ATR *and* the close to exceed the preceding
window's high. Only the horizon differs.
"""


@dataclass(frozen=True, slots=True)
class _HorizonIdentity:
    """Shared identity construction, so every horizon profile is labelled consistently."""

    @staticmethod
    def build(*, profile_id: str, strategy_id: str, timeout_name: str,
              parameters: tuple[tuple[str, str], ...],
              target_model: TargetModel) -> ProfileIdentity:
        return ProfileIdentity(profile_id=profile_id, strategy_id=strategy_id,
                               version=timeout_name, parameters=parameters,
                               target_model=target_model)


@dataclass(frozen=True, slots=True)
class VolatilityMeanReversionHorizon:
    """Volatility mean reversion on a predeclared coarser horizon.

    `timeframe` is part of the identity's parameters, so the same concept on 15m and 1h
    can never share a fingerprint, and neither can share one with the 1-minute v2.
    """

    timeframe_name: str = FIFTEEN_MINUTE.name
    window: int = 21
    displacement_atr_multiple: Decimal = Decimal("2.0")
    stop_atr_multiple: Decimal = Decimal("1.0")
    stop_floor_bps: Decimal = Decimal("80")
    stop_cap_bps: Decimal = Decimal("500")
    max_holding_bars: int = 16
    min_history: int = 21
    expected_holding_horizon: int = 8
    target_model: TargetModel = MR_HORIZON_TARGET_MODEL

    @property
    def timeframe(self) -> TimeframeSpec:
        return _TIMEFRAMES[self.timeframe_name]

    @property
    def parameters(self) -> tuple[tuple[str, str], ...]:
        return (("timeframe", self.timeframe_name), ("timeframe_seconds",
                                                     str(self.timeframe.seconds)),
                ("window", str(self.window)),
                ("displacement_atr_multiple", str(self.displacement_atr_multiple)),
                ("stop_atr_multiple", str(self.stop_atr_multiple)),
                ("stop_floor_bps", str(self.stop_floor_bps)),
                ("stop_cap_bps", str(self.stop_cap_bps)),
                ("target_floor_bps", str(self.target_model.floor_bps)),
                ("max_holding_bars", str(self.max_holding_bars)),
                ("expected_holding_horizon", str(self.expected_holding_horizon)))

    @property
    def identity(self) -> ProfileIdentity:
        return _HorizonIdentity.build(
            profile_id=f"volatility-mean-reversion-{self.timeframe_name}-v1",
            strategy_id="volatility_mean_reversion_horizon", timeout_name="0.1",
            parameters=self.parameters, target_model=self.target_model)

    @financial
    def stop_distance_bps(self, features: VolatilityFeatures) -> Decimal:
        raw = features.atr_bps * self.stop_atr_multiple
        return min(max(raw, self.stop_floor_bps), self.stop_cap_bps)

    @financial
    def propose(self, *, candles: Sequence[Candle], quantity: Decimal = ZERO,
                cost_basis_mxn: Decimal = ZERO, market: str = "BTC/MXN",
                entry_price_mxn: Decimal = ZERO,
                target_price_mxn: Decimal = ZERO) -> StrategyProposal:
        """Augment the inherited logic with horizon-scaled parameters.

        The evaluation is delegated to the frozen v2 implementation, parameterised so that
        every window and limit means the same thing in wall-clock terms as it does there.
        Delegation rather than duplication is deliberate: two copies of the same decision
        logic would eventually diverge, and the divergence would be invisible.
        """
        proxy = VolatilityMeanReversionV2(
            window=self.window,
            displacement_atr_multiple=self.displacement_atr_multiple,
            stop_atr_multiple=self.stop_atr_multiple,
            stop_floor_bps=self.stop_floor_bps, stop_cap_bps=self.stop_cap_bps,
            max_holding_bars=self.max_holding_bars, min_history=self.min_history,
            expected_holding_horizon=self.expected_holding_horizon,
            target_model=self.target_model)
        return proxy.propose(candles=candles, quantity=quantity,
                             cost_basis_mxn=cost_basis_mxn, market=market,
                             entry_price_mxn=entry_price_mxn,
                             target_price_mxn=target_price_mxn)

    def public(self) -> dict[str, Any]:
        return {**self.identity.public(), "timeframe": self.timeframe.public(),
                "concept": "volatility_mean_reversion",
                "horizon_profile_version": HORIZON_PROFILE_VERSION,
                "description": _MEAN_REVERSION_DOC}


@dataclass(frozen=True, slots=True)
class RangeExpansionHorizon:
    """Range expansion on a predeclared coarser horizon."""

    timeframe_name: str = FIFTEEN_MINUTE.name
    window: int = 20
    expansion_atr_multiple: Decimal = Decimal("1.5")
    stop_atr_multiple: Decimal = Decimal("1.2")
    stop_floor_bps: Decimal = Decimal("100")
    stop_cap_bps: Decimal = Decimal("600")
    max_holding_bars: int = 12
    min_history: int = 21
    expected_holding_horizon: int = 6
    target_model: TargetModel = RANGE_HORIZON_TARGET_MODEL

    @property
    def timeframe(self) -> TimeframeSpec:
        return _TIMEFRAMES[self.timeframe_name]

    @property
    def parameters(self) -> tuple[tuple[str, str], ...]:
        return (("timeframe", self.timeframe_name), ("timeframe_seconds",
                                                     str(self.timeframe.seconds)),
                ("window", str(self.window)),
                ("expansion_atr_multiple", str(self.expansion_atr_multiple)),
                ("stop_atr_multiple", str(self.stop_atr_multiple)),
                ("stop_floor_bps", str(self.stop_floor_bps)),
                ("stop_cap_bps", str(self.stop_cap_bps)),
                ("target_floor_bps", str(self.target_model.floor_bps)),
                ("max_holding_bars", str(self.max_holding_bars)),
                ("expected_holding_horizon", str(self.expected_holding_horizon)))

    @property
    def identity(self) -> ProfileIdentity:
        return _HorizonIdentity.build(
            profile_id=f"range-expansion-{self.timeframe_name}-v1",
            strategy_id="range_expansion_horizon", timeout_name="0.1",
            parameters=self.parameters, target_model=self.target_model)

    @financial
    def propose(self, *, candles: Sequence[Candle], quantity: Decimal = ZERO,
                cost_basis_mxn: Decimal = ZERO, market: str = "BTC/MXN",
                entry_price_mxn: Decimal = ZERO,
                target_price_mxn: Decimal = ZERO) -> StrategyProposal:
        proxy = RangeExpansionV1(
            window=self.window, expansion_atr_multiple=self.expansion_atr_multiple,
            stop_atr_multiple=self.stop_atr_multiple,
            stop_floor_bps=self.stop_floor_bps, stop_cap_bps=self.stop_cap_bps,
            max_holding_bars=self.max_holding_bars, min_history=self.min_history,
            expected_holding_horizon=self.expected_holding_horizon,
            target_model=self.target_model)
        return proxy.propose(candles=candles, quantity=quantity,
                             cost_basis_mxn=cost_basis_mxn, market=market,
                             entry_price_mxn=entry_price_mxn,
                             target_price_mxn=target_price_mxn)

    def public(self) -> dict[str, Any]:
        return {**self.identity.public(), "timeframe": self.timeframe.public(),
                "concept": "range_expansion",
                "horizon_profile_version": HORIZON_PROFILE_VERSION,
                "description": _RANGE_EXPANSION_DOC}


_TIMEFRAMES: dict[str, TimeframeSpec] = {
    FIFTEEN_MINUTE.name: FIFTEEN_MINUTE,
    ONE_HOUR_TIMEFRAME.name: ONE_HOUR_TIMEFRAME,
}


def horizon_profiles() -> tuple[Any, ...]:
    """One profile instance per (concept, horizon). Four in total, as predeclared."""
    return (
        VolatilityMeanReversionHorizon(timeframe_name=FIFTEEN_MINUTE.name),
        VolatilityMeanReversionHorizon(timeframe_name=ONE_HOUR_TIMEFRAME.name),
        RangeExpansionHorizon(timeframe_name=FIFTEEN_MINUTE.name),
        RangeExpansionHorizon(timeframe_name=ONE_HOUR_TIMEFRAME.name),
    )


def horizon_profile_by_id(profile_id: str) -> Any:
    for profile in horizon_profiles():
        if profile.identity.profile_id == profile_id:
            return profile
    raise KeyError(f"UNKNOWN_HORIZON_PROFILE:{profile_id}")


def horizon_profile_ids() -> tuple[str, ...]:
    return tuple(profile.identity.profile_id for profile in horizon_profiles())


__all__ = [
    "HORIZON_PROFILE_VERSION",
    "MR_HORIZON_TARGET_MODEL",
    "RANGE_HORIZON_TARGET_MODEL",
    "RangeExpansionHorizon",
    "VolatilityMeanReversionHorizon",
    "horizon_profile_by_id",
    "horizon_profile_ids",
    "horizon_profiles",
]

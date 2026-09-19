"""Deterministic market opportunity scoring.

Scores tradability-adjusted opportunity, never raw volatility. Every component is
a bounded Decimal-derived fraction in [0, 1], so the score is reproducible and
free of binary floating point. Weights are transparent, conservative and
deliberately NOT tuned on any observed session.

Formula (SCORE_VERSION below):

    opportunity = 100 * (
        w_movement   * movement_score
      + w_volatility * volatility_score
      + w_liquidity  * liquidity_score
      + w_depth      * depth_score
      + w_spread     * spread_cost_score
      + w_fee        * fee_cost_score
      + w_slippage   * slippage_score
      + w_quality    * data_quality_score
    ) / sum(weights)

Friction components (spread, fee, slippage) are *penalties*: their scores fall as
cost rises, so a market that moves strongly but is expensive to cross ranks below
a calmer, cheaper one. `friction_coverage` reports the fraction of the round-trip
friction that observed movement must exceed.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ONE, ZERO, financial

SCORE_VERSION = "autofund.market-opportunity.v1"
FRICTION_VERSION = "autofund.round-trip-friction.v1"

# Transparent conservative weights. They are documented constants, not fitted
# parameters, and must only change with a SCORE_VERSION bump.
WEIGHTS: dict[str, Decimal] = {
    "movement": Decimal("0.20"),
    "volatility": Decimal("0.10"),
    "liquidity": Decimal("0.10"),
    "depth": Decimal("0.15"),
    "spread_cost": Decimal("0.20"),
    "fee_cost": Decimal("0.10"),
    "slippage": Decimal("0.05"),
    "data_quality": Decimal("0.10"),
}

# Normalisation ceilings. A value at or above the ceiling scores 1; zero scores 0.
MOVEMENT_FULL_BPS = Decimal("200")      # observed high-low range as bps of price
VOLATILITY_FULL_BPS = Decimal("150")    # realised 1m volatility in bps
SPREAD_ZERO_BPS = Decimal("100")        # spread at/above this scores 0
SPREAD_FULL_BPS = Decimal("5")          # spread at/below this scores 1
FEE_ZERO_RATE = Decimal("0.02")         # taker rate at/above this scores 0
FEE_FULL_RATE = Decimal("0.001")        # taker rate at/below this scores 1
SLIPPAGE_FULL_BPS = Decimal("50")       # estimated round-trip slippage ceiling
DEPTH_FULL_MXN = Decimal("500")         # depth necessary to absorb the order cap

# Eligibility policy for the current MVP envelope.
MAX_SINGLE_ORDER_CAP_MXN = Decimal("11")
MAX_AUTHORIZED_CAPITAL_MXN = Decimal("50")
MAX_ELIGIBLE_SPREAD_BPS = Decimal("100")
MIN_REQUIRED_DEPTH_MXN = Decimal("11")
MAX_DATA_AGE_SECONDS = Decimal("15")


def _clamp(value: Decimal) -> Decimal:
    return ZERO if value < ZERO else min(value, ONE)


def _rising(value: Decimal | None, full: Decimal) -> Decimal:
    """Score 0 at zero, 1 at `full` and above."""
    if value is None or full <= ZERO:
        return ZERO
    return _clamp(value / full)


def _falling(value: Decimal | None, full: Decimal, zero: Decimal) -> Decimal:
    """Score 1 at/below `full`, 0 at/above `zero`."""
    if value is None or zero <= full:
        return ZERO
    return _clamp((zero - value) / (zero - full))


@dataclass(frozen=True, slots=True)
class FrictionEstimate:
    """Estimated round-trip cost, with its assumptions made explicit."""

    spread_cost_mxn: Decimal
    taker_fee_mxn: Decimal
    entry_slippage_mxn: Decimal
    exit_slippage_mxn: Decimal
    round_trip_mxn: Decimal
    round_trip_bps: Decimal
    executable_notional_mxn: Decimal
    assumptions: tuple[str, ...]

    def telemetry(self) -> dict[str, Any]:
        return {"version": FRICTION_VERSION, "spread_cost_mxn": str(self.spread_cost_mxn),
                "taker_fee_mxn": str(self.taker_fee_mxn),
                "entry_slippage_mxn": str(self.entry_slippage_mxn),
                "exit_slippage_mxn": str(self.exit_slippage_mxn),
                "round_trip_mxn": str(self.round_trip_mxn), "round_trip_bps": str(self.round_trip_bps),
                "executable_notional_mxn": str(self.executable_notional_mxn),
                "assumptions": list(self.assumptions)}


@financial
def estimate_friction(*, notional_mxn: Decimal, spread_bps: Decimal, taker_rate: Decimal,
                      depth_mxn: Decimal) -> FrictionEstimate:
    """Conservative cost model for one round trip on the observed top of book.

    Assumptions (stated, not hidden):
    - both legs cross the spread at the observed top-of-book spread;
    - taker fee applies to both legs at the observed account/public rate;
    - slippage is modelled from available depth per unit of notional and is an
      estimate, not a measurement.
    """
    if notional_mxn <= ZERO:
        zero = ZERO
        return FrictionEstimate(zero, zero, zero, zero, zero, zero, zero,
                                ("NO_EXECUTABLE_NOTIONAL",))
    spread_cost = notional_mxn * (spread_bps / Decimal("10000")) / Decimal("2")
    taker_fee = notional_mxn * taker_rate * Decimal("2")
    depth_ratio = (notional_mxn / depth_mxn) if depth_mxn > ZERO else ONE
    slippage = notional_mxn * (spread_bps / Decimal("10000")) * _clamp(depth_ratio)
    total = spread_cost + taker_fee + slippage + slippage
    return FrictionEstimate(spread_cost, taker_fee, slippage, slippage, total,
                            (total / notional_mxn) * Decimal("10000"), notional_mxn,
                            ("BOTH_LEGS_CROSS_TOP_OF_BOOK", "TAKER_FEE_BOTH_LEGS",
                             "SLIPPAGE_ESTIMATED_FROM_TOB_DEPTH", "CHARGES_FOR_ONE_ROUND_TRIP"))


@dataclass(frozen=True, slots=True)
class MarketOpportunityScore:
    """Versioned, fully-exposed tradability-adjusted opportunity score."""

    score: Decimal
    components: dict[str, str]
    weights: dict[str, str]
    movement_bps: Decimal
    volatility_bps: Decimal
    friction: FrictionEstimate
    friction_coverage: str
    version: str = SCORE_VERSION

    def telemetry(self) -> dict[str, Any]:
        return {"score_version": self.version, "score": str(self.score),
                "components": dict(self.components), "weights": dict(self.weights),
                "movement_bps": str(self.movement_bps), "volatility_bps": str(self.volatility_bps),
                "friction_coverage": self.friction_coverage, "friction": self.friction.telemetry()}


@financial
def score_market(*, movement_bps: Decimal | None, volatility_bps: Decimal | None,
                 spread_bps: Decimal | None, depth_mxn: Decimal | None, volume_mxn: Decimal | None,
                 taker_rate: Decimal | None, quality: str, staleness_seconds: Decimal | None,
                 notional_mxn: Decimal = MAX_SINGLE_ORDER_CAP_MXN) -> MarketOpportunityScore:
    """Score one market. Deterministic: identical inputs always give one score."""
    spread_cost = _falling(spread_bps, SPREAD_FULL_BPS, SPREAD_ZERO_BPS)
    fee_cost = _falling(taker_rate, FEE_FULL_RATE, FEE_ZERO_RATE)
    depth_score = _rising(depth_mxn, DEPTH_FULL_MXN)
    # Liquidity blends observed volume with available depth so thin books cannot
    # rank highly on volume alone.
    liquidity = _clamp((_rising(volume_mxn, DEPTH_FULL_MXN * Decimal("10")) + depth_score) / Decimal("2"))
    slippage = _falling(spread_bps, SPREAD_FULL_BPS, SLIPPAGE_FULL_BPS)
    data_quality = {"VALID": ONE, "DEGRADED": Decimal("0.5"), "INVALID": ZERO}.get(quality, ZERO)
    if staleness_seconds is not None and staleness_seconds > MAX_DATA_AGE_SECONDS:
        data_quality = ZERO
    components = {
        "movement_score": _rising(movement_bps, MOVEMENT_FULL_BPS),
        "volatility_score": _rising(volatility_bps, VOLATILITY_FULL_BPS),
        "liquidity_score": liquidity, "depth_score": depth_score,
        "spread_cost_score": spread_cost, "fee_cost_score": fee_cost,
        "slippage_score": slippage, "data_quality_score": data_quality,
    }
    weighted = sum((WEIGHTS[name] * components[f"{name}_score"] for name in WEIGHTS), ZERO)
    total_weight = sum(WEIGHTS.values(), ZERO)
    score = (weighted / total_weight * Decimal("100")) if total_weight > ZERO else ZERO
    friction = estimate_friction(notional_mxn=notional_mxn,
                                 spread_bps=spread_bps or ZERO, taker_rate=taker_rate or ZERO,
                                 depth_mxn=depth_mxn or ZERO)
    coverage = "NOT_APPLICABLE"
    if movement_bps is not None and friction.round_trip_bps > ZERO:
        coverage = str(movement_bps / friction.round_trip_bps)
    return MarketOpportunityScore(
        score=score, components={k: str(v) for k, v in components.items()},
        weights={k: str(v) for k, v in WEIGHTS.items()},
        movement_bps=movement_bps or ZERO, volatility_bps=volatility_bps or ZERO,
        friction=friction, friction_coverage=coverage)

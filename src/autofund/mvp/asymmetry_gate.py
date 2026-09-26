"""Pre-entry asymmetry feasibility, expressed as geometry rather than as a ratio.

This gate exists to answer one question before a position is contemplated: *is this trade's
shape even capable of paying for itself?* It is deliberately the third of three gates and
the weakest of them, because the other two own the questions that actually decide whether
money may move:

    EconomicEdgeGuard   may this be traded at all, given real costs?      (authoritative)
    RiskEngine          is the downside within policy?                    (authoritative)
    This gate           is the shape capable of clearing its own cost?    (research only)

It has no authority. It cannot admit anything the other two refuse, and it is reported
separately so that a rejected shape is never confused with a rejected opportunity.

**Why geometry instead of a magic ratio.** The spec's instruction not to assume 2:1 or 3:1
is not a stylistic preference, it follows from the arithmetic. Friction is an additive
constant; reward and risk are both measured in the same price units; and the profitability
condition is

    net_reward = gross_reward - friction  >=  risk * ratio
    =>  gross_reward >= friction + risk * (1 + ratio)

so the *same* ratio becomes progressively cheaper to satisfy as risk shrinks. At the
confirmed ~173 bps round-trip friction on this account, a 0.50 MXN risk cap on an 11 MXN
budget permits at most

    risk <= cap / budget * 10_000 - friction = 281.5 bps

and a ratio of 1.0 then requires a gross move of at least 735 bps — approximately 7.4% on
BTC/MXN, at a horizon where the median favourable excursion observed in 0.2.4 was zero.

That computation, performed before any threshold was chosen, is what makes the predeclared
band defensible: the interesting region is narrow invalidation, not a larger ratio. Ratios
from 0.5 to 2.0 are tested as a small predeclared set, all retained, and none is treated as
correct in advance.

**Friction is charged on both paths.** A stopped-out trade still paid both fee legs, the
spread and the slippage. Netting friction only from the reward would make every losing trade
look cheaper than it is, which is the error that would let this gate pass a strategy whose
stops lose money structurally.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial

from .execution_model import BPS

FEASIBILITY_VERSION = "autofund.risk-reward-feasibility.v1"

# ---- predeclared candidate thresholds (spec section 9) ----
# A small band, declared before evaluation and frozen into the experiment manifest. 1.0 is
# included because it is the project's existing risk-policy requirement, so the band is
# anchored on a decision the project already made rather than on a textbook figure.
# 0.5 is included because the arithmetic above says requiring less is the *cheaper* way to
# satisfy the gate when risk is the binding term, and excluding it would have made the
# experiment unable to test its own stated mechanism.
PREDECLARED_REWARD_RISK_THRESHOLDS: tuple[Decimal, ...] = (
    Decimal("0.5"), Decimal("0.75"), Decimal("1.0"), Decimal("2.0"))

# Why the band stops at 2.0: at 2.0 the gross requirement at an 80 bps invalidation is
# already 173 + 80*3 = 413 bps, and a 3.0 threshold would need 573 bps at the same stop,
# which the observed excursions do not reach. Including a threshold the evidence cannot
# satisfy would spend budget on a configuration that cannot pass.
DEFAULT_REWARD_RISK_THRESHOLD = Decimal("1.0")

# The minimum buffer required on top of net-positive, as a fraction of friction. A trade
# that clears friction by exactly zero has no margin for the difference between the modelled
# fill and a real one, so "barely profitable in the model" is treated as not profitable.
DEFAULT_MINIMUM_ECONOMIC_BUFFER_FRACTION = Decimal("0.10")

# Classifications.
FEASIBLE = "FEASIBLE"
INSUFFICIENT_GROSS_MOVE = "INSUFFICIENT_GROSS_MOVE"
INSUFFICIENT_NET_REWARD = "INSUFFICIENT_NET_REWARD"
RISK_TOO_WIDE_FOR_POLICY = "RISK_TOO_WIDE_FOR_POLICY"
NO_INVALIDATION_DECLARED = "NO_INVALIDATION_DECLARED"
INVALIDATION_NOT_BELOW_ENTRY = "INVALIDATION_NOT_BELOW_ENTRY"
TARGET_NOT_ABOVE_ENTRY = "TARGET_NOT_ABOVE_ENTRY"
ECONOMIC_EDGE_REJECT = "ECONOMIC_EDGE_REJECT"

ALL_CLASSIFICATIONS: tuple[str, ...] = (
    FEASIBLE, INSUFFICIENT_GROSS_MOVE, INSUFFICIENT_NET_REWARD,
    RISK_TOO_WIDE_FOR_POLICY, NO_INVALIDATION_DECLARED, INVALIDATION_NOT_BELOW_ENTRY,
    TARGET_NOT_ABOVE_ENTRY, ECONOMIC_EDGE_REJECT)


class FeasibilityError(ValueError):
    """The gate was asked to evaluate something it cannot express."""


@dataclass(frozen=True, slots=True)
class RiskRewardFeasibility:
    """The pre-entry geometry of one prospective trade.

    Every figure is derived from prices known at the decision bar and from the account's
    confirmed costs. Nothing is read from the trade's later path, so this cannot be
    back-filled from the outcome.
    """

    classification: str
    feasible: bool
    gross_reward_bps: Decimal
    risk_to_invalidation_bps: Decimal
    net_reward_bps: Decimal
    friction_bps: Decimal
    gross_reward_to_risk: Decimal | None
    net_reward_to_risk: Decimal | None
    required_ratio: Decimal
    minimum_economic_buffer_bps: Decimal
    economic_guard_admissible: bool
    max_risk_bps_allowed: Decimal
    # The gross move this geometry would need in order to pass, reported so a rejection
    # states what *would* have been required rather than only that something failed.
    required_gross_bps: Decimal

    def __post_init__(self) -> None:
        if self.classification not in ALL_CLASSIFICATIONS:
            raise FeasibilityError(f"unknown classification: {self.classification}")

    def telemetry(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {"version": FEASIBILITY_VERSION, "classification": self.classification,
                "feasible": self.feasible,
                "gross_reward_bps": str(self.gross_reward_bps),
                "risk_to_invalidation_bps": str(self.risk_to_invalidation_bps),
                "net_reward_bps": str(self.net_reward_bps),
                "friction_bps": str(self.friction_bps),
                "gross_reward_to_risk": s(self.gross_reward_to_risk),
                "net_reward_to_risk": s(self.net_reward_to_risk),
                "required_ratio": str(self.required_ratio),
                "minimum_economic_buffer_bps": str(self.minimum_economic_buffer_bps),
                "economic_guard_admissible": self.economic_guard_admissible,
                "max_risk_bps_allowed": str(self.max_risk_bps_allowed),
                "required_gross_bps": str(self.required_gross_bps),
                "research_only": True,
                "production_authority": False,
                "can_override_economic_guard": False,
                "can_override_risk_engine": False,
                "evaluated_before_entry": True}


@financial
def friction_bps_for(*, taker_fee_rate: Decimal, spread_bps: Decimal,
                     slippage_bps: Decimal) -> Decimal:
    """Round-trip friction in bps: two fee legs, the spread paid once, and slippage.

    The spread is charged once because only one leg crosses it — the entry pays the ask and
    the exit receives the bid, so the spread is crossed exactly once per round trip. Charging
    it on both legs would double-count it and would make this gate reject geometry the
    economic guard had already accepted.
    """
    return (taker_fee_rate * BPS * Decimal("2")) + spread_bps + slippage_bps


@financial
def maximum_risk_bps(*, budget_mxn: Decimal, max_single_trade_risk_mxn: Decimal,
                     friction_bps: Decimal) -> Decimal:
    """The widest invalidation distance that can still satisfy risk policy.

    Inverting `risk_mxn = notional * risk_bps / BPS + friction_mxn <= cap` gives
    `risk_bps <= cap / budget * BPS - friction_bps`. This is the figure that makes the
    milestone's difficulty explicit: on the real account it is roughly 281 bps, while a
    volatility-scaled stop at a 15m or 1h horizon is three to twenty times wider.
    """
    if budget_mxn <= ZERO:
        raise FeasibilityError("budget must be positive")
    return (max_single_trade_risk_mxn / budget_mxn * BPS) - friction_bps


@financial
def assess_feasibility(*, entry_price_mxn: Decimal, target_price_mxn: Decimal,
                       invalidation_price_mxn: Decimal, budget_mxn: Decimal,
                       friction_bps: Decimal, required_ratio: Decimal,
                       max_single_trade_risk_mxn: Decimal,
                       economic_guard_admissible: bool,
                       minimum_economic_buffer_fraction: Decimal
                       = DEFAULT_MINIMUM_ECONOMIC_BUFFER_FRACTION,
                       ) -> RiskRewardFeasibility:
    """Judge the geometry of a prospective trade before it exists.

    The order of the checks is the order of the instructions the gate is subordinate to:
    economics first (the guard is authoritative), then risk policy, then this gate's own
    asymmetry question. That ordering means a rejection reason always names the most
    senior gate that objected, rather than burying an economic refusal under a shape
    comment.
    """
    empty = Decimal("0")
    if entry_price_mxn <= ZERO:
        raise FeasibilityError("entry price must be positive")
    if required_ratio <= ZERO:
        raise FeasibilityError("required ratio must be positive")
    max_risk_bps = maximum_risk_bps(budget_mxn=budget_mxn,
                                    max_single_trade_risk_mxn=max_single_trade_risk_mxn,
                                    friction_bps=friction_bps)
    risk_bps = ZERO
    gross_bps = ZERO
    if invalidation_price_mxn > ZERO:
        risk_bps = (entry_price_mxn - invalidation_price_mxn) / entry_price_mxn * BPS
    if target_price_mxn > ZERO:
        gross_bps = (target_price_mxn - entry_price_mxn) / entry_price_mxn * BPS

    buffer_bps = friction_bps * minimum_economic_buffer_fraction
    net_bps = gross_bps - friction_bps
    gross_to_risk = (gross_bps / risk_bps) if risk_bps > ZERO else None
    net_to_risk = (net_bps / risk_bps) if risk_bps > ZERO else None
    # Inverting the profitability condition: net >= risk * ratio
    #   => gross - friction >= risk * ratio
    #   => gross >= friction + risk * (1 + ratio)
    required_gross_bps = friction_bps + risk_bps * (Decimal("1") + required_ratio)

    def verdict(classification: str) -> RiskRewardFeasibility:
        return RiskRewardFeasibility(
            classification=classification, feasible=classification == FEASIBLE,
            gross_reward_bps=gross_bps, risk_to_invalidation_bps=risk_bps,
            net_reward_bps=net_bps, friction_bps=friction_bps,
            gross_reward_to_risk=gross_to_risk, net_reward_to_risk=net_to_risk,
            required_ratio=required_ratio, minimum_economic_buffer_bps=buffer_bps,
            economic_guard_admissible=economic_guard_admissible,
            max_risk_bps_allowed=max_risk_bps, required_gross_bps=required_gross_bps)

    # 1. The economic guard is authoritative. Asymmetry cannot substitute for net economics:
    #    a beautiful reward/risk ratio on a target that does not cover fees is still a losing
    #    trade, and passing it here would be this gate overruling a stronger one.
    if not economic_guard_admissible:
        return verdict(ECONOMIC_EDGE_REJECT)
    # 2. A trade with no declared invalidation has no risk to evaluate. It is refused here
    #    rather than accepted, because accepting it would let an unbounded trade pass a gate
    #    whose entire purpose is bounding risk. Reported distinctly from a *wrong* boundary.
    if invalidation_price_mxn <= ZERO:
        return verdict(NO_INVALIDATION_DECLARED)
    if risk_bps <= ZERO:
        return verdict(INVALIDATION_NOT_BELOW_ENTRY)
    if gross_bps <= ZERO:
        return verdict(TARGET_NOT_ABOVE_ENTRY)
    # 3. Risk policy, unchanged and not negotiable by this module.
    if risk_bps > max_risk_bps:
        return verdict(RISK_TOO_WIDE_FOR_POLICY)
    # 4. The gross move must be large enough to fund friction, the risk taken, and the
    #    required ratio of profit on that risk.
    if gross_bps < required_gross_bps:
        return verdict(INSUFFICIENT_GROSS_MOVE)
    # 5. And the net must clear friction with a margin, so a model barely at break-even is
    #    not reported as an edge.
    if net_bps < (risk_bps * required_ratio) + buffer_bps:
        return verdict(INSUFFICIENT_NET_REWARD)
    del empty
    return verdict(FEASIBLE)


__all__ = [
    "ALL_CLASSIFICATIONS",
    "DEFAULT_MINIMUM_ECONOMIC_BUFFER_FRACTION",
    "DEFAULT_REWARD_RISK_THRESHOLD",
    "ECONOMIC_EDGE_REJECT",
    "FEASIBILITY_VERSION",
    "FEASIBLE",
    "INSUFFICIENT_GROSS_MOVE",
    "INSUFFICIENT_NET_REWARD",
    "INVALIDATION_NOT_BELOW_ENTRY",
    "NO_INVALIDATION_DECLARED",
    "PREDECLARED_REWARD_RISK_THRESHOLDS",
    "RISK_TOO_WIDE_FOR_POLICY",
    "TARGET_NOT_ABOVE_ENTRY",
    "FeasibilityError",
    "RiskRewardFeasibility",
    "assess_feasibility",
    "friction_bps_for",
    "maximum_risk_bps",
]

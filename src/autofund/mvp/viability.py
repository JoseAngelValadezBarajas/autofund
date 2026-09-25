"""Economic viability of a strategy proposal, on a specific market at a specific fee.

This is the bridge the milestone exists for. MVP 0.1.3 built the guard that refuses
unprofitable trades; 0.1.4 builds profiles whose *intended* move can be large enough
to survive that guard. This module answers one question, honestly and for a named
market:

    given this profile's proposal, this real fee, this real book and this real
    spread -- what is left after friction?

Design decisions worth stating, because they are the difference between an
architecture and a sales pitch:

- **Friction is estimated, never assumed away.** Fees come from the confirmed
  account schedule. Spread comes from the observed book. Slippage comes from
  walking the real depth for the real order size (``slippage.py``), never from the
  slippage *tolerance*, which is a limit and not a forecast.
- **The profile's own target is used as the exit price.** No invented profit, and
  no optimistic assumption that the market will exceed what the strategy claims.
  If the profile's target cannot pay for the round trip, it is NOT_VIABLE.
- **A structurally unviable profile is reported as such.** The Champion will report
  NOT_VIABLE at the confirmed fee. That is the correct, expected result and is
  preserved as evidence.
- **Viability is per-market.** A profile viable on one book may be unviable on
  another with wider spread or thinner depth. Nothing is global except the policy.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ONE, ZERO, financial

from .economics import EconomicPolicy, economic_entry_model
from .profiles import (
    BPS,
    COMPATIBLE,
    NOT_VIABLE,
    PRODUCTION_CERTIFIABLE,
    RESEARCH_ONLY,
    VIABLE,
    StrategyProposal,
    TargetModel,
)
from .slippage import RoundTripSlippage, estimate_round_trip

VIABILITY_VERSION = "autofund.economic-viability.v1"

# Component reason codes, so "not viable" is always attributable to a cause.
FRICTION_EXCEEDS_TARGET = "FRICTION_EXCEEDS_EXPECTED_EDGE"
DEPTH_INSUFFICIENT = "ORDER_SIZE_EXCEEDS_EXECUTABLE_DEPTH"
BELOW_POLICY_BUFFER = "NET_EDGE_BELOW_POLICY_BUFFER"
VIABLE_WITH_MARGIN = "NET_EDGE_POSITIVE_AFTER_FRICTION"


@dataclass(frozen=True, slots=True)
class FrictionBreakdown:
    """Every component of round-trip friction, itemised and attributable."""

    notional_mxn: Decimal
    entry_fee_mxn: Decimal
    exit_fee_mxn: Decimal
    spread_cost_mxn: Decimal
    slippage_cost_mxn: Decimal

    @property
    def total_mxn(self) -> Decimal:
        return (self.entry_fee_mxn + self.exit_fee_mxn + self.spread_cost_mxn
                + self.slippage_cost_mxn)

    @property
    def total_bps(self) -> Decimal:
        if self.notional_mxn <= ZERO:
            return ZERO
        return self.total_mxn / self.notional_mxn * BPS

    def telemetry(self) -> dict[str, str]:
        return {"notional_mxn": str(self.notional_mxn),
                "entry_fee_mxn": str(self.entry_fee_mxn),
                "exit_fee_mxn": str(self.exit_fee_mxn),
                "spread_cost_mxn": str(self.spread_cost_mxn),
                "slippage_cost_mxn": str(self.slippage_cost_mxn),
                "total_mxn": str(self.total_mxn), "total_bps": str(self.total_bps)}


@dataclass(frozen=True, slots=True)
class ViabilityAssessment:
    """Independent economic verdict on one profile/market proposal.

    Deliberately shaped like the guard's own evidence so the two can be compared
    line by line, and so a future ProductionMarketSelector can consume it directly.
    """

    market: str
    profile_id: str
    strategy_fingerprint: str
    compatibility: str
    status: str
    reason_code: str
    expected_gross_edge_bps: Decimal
    friction: FrictionBreakdown
    expected_net_edge_bps: Decimal
    expected_net_pnl_mxn: Decimal
    required_edge_bps: Decimal
    admission_margin_bps: Decimal
    slippage: RoundTripSlippage | None
    policy: EconomicPolicy
    target_model: TargetModel
    evidence_count: int = 0
    notes: tuple[str, ...] = ()

    @property
    def viable(self) -> bool:
        return self.status in (VIABLE, PRODUCTION_CERTIFIABLE)

    @property
    def covers_friction(self) -> bool:
        """Whether the intended gross move alone can pay the round trip."""
        return self.expected_gross_edge_bps > self.friction.total_bps

    def telemetry(self) -> dict[str, Any]:
        return {"version": VIABILITY_VERSION, "market": self.market,
                "profile_id": self.profile_id,
                "strategy_fingerprint": self.strategy_fingerprint,
                "compatibility": self.compatibility, "status": self.status,
                "reason_code": self.reason_code,
                "expected_gross_edge_bps": str(self.expected_gross_edge_bps),
                "friction": self.friction.telemetry(),
                "required_edge_bps": str(self.required_edge_bps),
                "expected_net_edge_bps": str(self.expected_net_edge_bps),
                "expected_net_pnl_mxn": str(self.expected_net_pnl_mxn),
                "admission_margin_bps": str(self.admission_margin_bps),
                "covers_friction": self.covers_friction, "viable": self.viable,
                "slippage": None if self.slippage is None else self.slippage.telemetry(),
                "policy": self.policy.public(), "target_model": self.target_model.public(),
                "evidence_count": self.evidence_count, "notes": list(self.notes),
                "slippage_tolerance_reused_as_forecast": False}


@financial
def assess_viability(*, proposal: StrategyProposal, budget_mxn: Decimal,
                     taker_fee_rate: Decimal, spread_bps: Decimal,
                     bids: tuple[Any, ...], asks: tuple[Any, ...],
                     policy: EconomicPolicy, compatibility: str = RESEARCH_ONLY,
                     slippage_bps: Decimal | None = None,
                     evidence_count: int = 0) -> ViabilityAssessment:
    """Full economic assessment of one proposal on one market.

    The entry/exit prices are the profile's own references, and fees are applied
    exactly as the account charges them: the buy fee reduces owned quantity and the
    sell fee reduces proceeds.

    Slippage comes from walking the observed book for this order size. When `bids` and
    `asks` are empty -- historical evaluation, where the exchange publishes no
    historical depth -- the caller must supply `slippage_bps` explicitly, because a
    book-less walk would return an unexecutable order and sink the evaluation for a
    reason that has nothing to do with the strategy. That assumption is then recorded
    on the assessment so it cannot pass as a measurement.
    """
    entry_price = proposal.entry_reference_mxn
    exit_price = proposal.expected_exit_reference_mxn
    zero = FrictionBreakdown(budget_mxn, ZERO, ZERO, ZERO, ZERO)
    if entry_price <= ZERO or exit_price <= ZERO or budget_mxn <= ZERO:
        return ViabilityAssessment(
            market=proposal.market, profile_id=proposal.profile_id,
            strategy_fingerprint=proposal.strategy_fingerprint, compatibility=compatibility,
            status=NOT_VIABLE, reason_code=FRICTION_EXCEEDS_TARGET,
            expected_gross_edge_bps=ZERO, friction=zero, expected_net_edge_bps=ZERO,
            expected_net_pnl_mxn=ZERO, required_edge_bps=ZERO, admission_margin_bps=ZERO,
            slippage=None, policy=policy, target_model=proposal.target_model,
            evidence_count=evidence_count, notes=("NO_EXECUTABLE_REFERENCE_PRICE",))

    has_book = bool(bids) and bool(asks)
    if has_book:
        walk = estimate_round_trip(bids=bids, asks=asks, notional_mxn=budget_mxn)
        entry_slippage_bps = walk.entry_slippage_bps
        exit_slippage_bps = walk.exit_slippage_bps
        slippage_cost = walk.total_slippage_mxn
    else:
        if slippage_bps is None:
            raise ValueError("MODELLED_SLIPPAGE_REQUIRED_WITHOUT_OBSERVED_BOOK")
        walk = None
        # Modelled for both legs, matching how a round-trip book walk would apply it.
        entry_slippage_bps = slippage_bps
        exit_slippage_bps = slippage_bps
        slippage_cost = budget_mxn * (slippage_bps + slippage_bps) / BPS

    notes: list[str] = []
    if not has_book:
        notes.append("SLIPPAGE_MODELLED_NO_HISTORICAL_DEPTH_PUBLISHED")
    elif not walk or not walk.executable:
        notes.append("DEPTH_INSUFFICIENT_FOR_STRESSED_ORDER_SIZE")

    round_trip_slippage_bps = entry_slippage_bps + exit_slippage_bps

    model = economic_entry_model(
        book=proposal.market, budget_mxn=budget_mxn, buy_price_mxn=entry_price,
        target_price_mxn=exit_price, buy_fee_rate=taker_fee_rate,
        sell_fee_rate=taker_fee_rate, spread_bps=spread_bps,
        slippage_bps=round_trip_slippage_bps, policy=policy)

    spread_cost = budget_mxn * (spread_bps / BPS)
    entry_fee = budget_mxn * taker_fee_rate
    exit_fee = model.expected_exit_fee_mxn
    friction = FrictionBreakdown(
        notional_mxn=budget_mxn, entry_fee_mxn=entry_fee, exit_fee_mxn=exit_fee,
        spread_cost_mxn=spread_cost, slippage_cost_mxn=slippage_cost)

    gross_bps = proposal.expected_gross_edge_bps
    required_bps = friction.total_bps + policy.minimum_net_edge_bps
    margin_bps = gross_bps - required_bps
    net_bps = model.expected_net_edge_bps

    if walk is not None and not walk.executable:
        status, reason = NOT_VIABLE, DEPTH_INSUFFICIENT
    elif gross_bps <= friction.total_bps:
        status, reason = NOT_VIABLE, FRICTION_EXCEEDS_TARGET
    elif net_bps < policy.minimum_net_edge_bps or model.expected_net_pnl_mxn <= policy.minimum_net_profit_mxn:
        status, reason = NOT_VIABLE, BELOW_POLICY_BUFFER
    elif compatibility != COMPATIBLE:
        # Economically viable, but this profile has no certified evidence for this
        # market. Viability is not the same claim as production-readiness.
        status, reason = VIABLE, VIABLE_WITH_MARGIN
        notes.append("VIABLE_BUT_NOT_CERTIFIED_FOR_MARKET")
    else:
        status, reason = VIABLE, VIABLE_WITH_MARGIN

    return ViabilityAssessment(
        market=proposal.market, profile_id=proposal.profile_id,
        strategy_fingerprint=proposal.strategy_fingerprint, compatibility=compatibility,
        status=status, reason_code=reason, expected_gross_edge_bps=gross_bps,
        friction=friction, expected_net_edge_bps=net_bps,
        expected_net_pnl_mxn=model.expected_net_pnl_mxn, required_edge_bps=required_bps,
        admission_margin_bps=margin_bps, slippage=walk, policy=policy,
        target_model=proposal.target_model, evidence_count=evidence_count,
        notes=tuple(notes))


@financial
def minimum_viable_gross_edge_bps(*, taker_fee_rate: Decimal, spread_bps: Decimal,
                                  slippage_bps: Decimal = ZERO,
                                  policy: EconomicPolicy) -> Decimal:
    """Gross edge a profile must intend before it can possibly clear friction.

    Reported so a profile designer can see the bar instead of guessing at it. The
    two fees are counted once each, which is the whole point of the 0.1.3 finding:
    a ~156 bps two-sided cost cannot be cleared by a 20 bps target.
    """
    fee_bps = (taker_fee_rate + taker_fee_rate * (ONE - taker_fee_rate)) * BPS
    return fee_bps + spread_bps + slippage_bps + policy.minimum_net_edge_bps

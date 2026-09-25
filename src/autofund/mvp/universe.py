"""The certified Production universe and the deterministic production market selector.

Two things live here, and they are separate on purpose:

**The universe** is the set of market/profile pairs that are currently *eligible* to be
traded: certified, with valid current market data, known fees, a compatible book and
compatible accounting. It is constructed dynamically and never hardcodes a market. A
stablecoin or fiat-like book is excluded by classification, so a fiat pair cannot
become the "best" volatile-crypto opportunity.

**The selector** picks at most one opportunity from that universe, or none. Its
ordering is fully deterministic and its primary key is *economics*, not a market score:
EconomicEdgeGuard must have admitted the proposal, and the candidates are then ranked by
expected net edge. Market-quality criteria are tie-breakers, not the primary signal, so
the selector cannot prefer a market because it is historically the default, because it
has a signal, or because it is the most volatile.

Two behaviours are load-bearing:

- **NO_TRADE is a first-class outcome.** When nothing is admissible the answer is
  NO_TRADE, never the least-bad market. There is no code path that selects a candidate
  whose economic verdict was negative.
- **Refusals are attributed.** Every candidate carries a deterministic reason code, so
  an operator can distinguish NO_SIGNAL from NOT_CERTIFIED from NEGATIVE_NET_EDGE from
  CAPITAL_REJECT. "Not selected" is never reported without a cause.

No LLM participates in any of this. Every decision is a pure function of the evidence,
the fees, the book and the portfolio.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from autofund.decimal_utils import ZERO, financial

from .certification import (
    CERTIFIED,
    PRODUCTION_CERTIFIABLE,
    SUSPENDED,
    PairCertification,
)
from .profiles import (
    CLASS_STABLE_OR_FIAT,
    DECISION_BUY,
    DECISION_NO_SIGNAL,
    classify_market,
)
from .viability import ViabilityAssessment

SELECTOR_VERSION = "autofund.production-market-selector.v2"
UNIVERSE_VERSION = "autofund.certified-production-universe.v1"

# ---------------------------------------------------------------------------
# Deterministic outcomes and reason codes.
# ---------------------------------------------------------------------------

NO_TRADE = "NO_TRADE"
SELECTED = "SELECTED"

# Why a candidate was not selected. Every refusal names exactly one cause, in the
# order the checks run, so the first failing check is the reported reason.
REASON_NO_SIGNAL = "NO_SIGNAL"
REASON_NOT_CERTIFIED = "NOT_CERTIFIED"
REASON_SUSPENDED = "CERTIFICATION_SUSPENDED"
REASON_INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
REASON_NOT_VIABLE = "NOT_VIABLE"
REASON_NEGATIVE_NET_EDGE = "NEGATIVE_NET_EDGE"
REASON_ECONOMIC_GUARD_REJECT = "ECONOMIC_GUARD_REJECT"
REASON_SPREAD_REJECT = "SPREAD_REJECT"
REASON_DEPTH_REJECT = "DEPTH_REJECT"
REASON_STALE_DATA = "DATA_INVALID"
REASON_FIAT_LIKE_MARKET = "FIAT_LIKE_MARKET_EXCLUDED"
REASON_NO_FEE_DATA = "FEE_DATA_UNAVAILABLE"
REASON_NO_MARKET_DATA = "MARKET_DATA_UNAVAILABLE"
REASON_CAPITAL_REJECT = "CAPITAL_REJECT"
REASON_RISK_REJECT = "RISK_REJECT"
REASON_POSITION_CONSTRAINT = "POSITION_CONSTRAINT"
REASON_UNRESOLVED_ORDER = "UNRESOLVED_ORDER_IN_FLIGHT"
REASON_NOT_IN_UNIVERSE = "MARKET_NOT_IN_CERTIFIED_UNIVERSE"
REASON_ALREADY_HOLDING = "POSITION_ALREADY_OPEN_IN_MARKET"


class MarketDataView(Protocol):
    """Current market state for one candidate. Read-only by construction."""

    @property
    def best_bid(self) -> Decimal: ...

    @property
    def best_ask(self) -> Decimal: ...

    @property
    def spread_bps(self) -> Decimal: ...

    @property
    def age_seconds(self) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class CandidateMarket:
    """Everything the selector knows about one candidate, gathered by the caller.

    Deliberately a plain value object: the selector never reads the exchange, the
    clock or the wallet itself, so its decision is reproducible from the record alone.
    """

    market: str
    book: str
    profile_id: str
    certification: PairCertification
    decision: str
    expected_gross_edge_bps: Decimal
    expected_net_edge_bps: Decimal | None
    expected_net_pnl_mxn: Decimal | None
    viability: ViabilityAssessment | None
    taker_fee_rate: Decimal
    spread_bps: Decimal
    bid_quantity_mxn: Decimal
    data_age_seconds: Decimal
    fee_available: bool = True
    market_data_available: bool = True
    minimum_value_mxn: Decimal = ZERO

    @property
    def market_class(self) -> str:
        return classify_market(self.book)


@dataclass(frozen=True, slots=True)
class PortfolioConstraints:
    """Global portfolio limits. They are global, never per market (spec section 16)."""

    authorized_capital_mxn: Decimal
    max_deployment_mxn: Decimal
    single_order_cap_mxn: Decimal
    deployed_mxn: Decimal
    cash_mxn: Decimal

    @property
    def headroom_mxn(self) -> Decimal:
        """Deployment still available, never negative."""
        remaining = self.max_deployment_mxn - self.deployed_mxn
        return max(ZERO, remaining)

    @property
    def order_cap_mxn(self) -> Decimal:
        """The largest single order currently permitted."""
        return min(self.single_order_cap_mxn, self.headroom_mxn, self.cash_mxn)

    def public(self) -> dict[str, str]:
        return {"authorized_capital_mxn": str(self.authorized_capital_mxn),
                "max_deployment_mxn": str(self.max_deployment_mxn),
                "single_order_cap_mxn": str(self.single_order_cap_mxn),
                "deployed_mxn": str(self.deployed_mxn), "cash_mxn": str(self.cash_mxn),
                "headroom_mxn": str(self.headroom_mxn),
                "order_cap_mxn": str(self.order_cap_mxn)}


@dataclass(frozen=True, slots=True)
class MarketSelectionContext:
    """Portfolio and global-safety state for one selection round."""

    constraints: PortfolioConstraints
    unresolved_orders: int = 0
    open_markets: frozenset[str] = field(default_factory=frozenset)
    max_unresolved_orders: int = 1
    max_market_age_seconds: Decimal = Decimal("15")
    max_spread_bps: Decimal = Decimal("100")


# ---------------------------------------------------------------------------
# The certified universe
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class UniverseEntry:
    """One pair admitted to the Production universe, with why it qualified."""

    market: str
    profile_id: str
    certification_state: str
    profile_fingerprint: str
    strategy_fingerprint: str
    evidence_fingerprint: str
    expected_net_edge_bps: Decimal | None = None
    strategy_compatibility: str = "RESEARCH_ONLY"

    @property
    def key(self) -> str:
        return f"{self.market}|{self.profile_id}"

    def public(self) -> dict[str, Any]:
        return {"market": self.market, "profile_id": self.profile_id,
                "certification_state": self.certification_state,
                "profile_fingerprint": self.profile_fingerprint,
                "strategy_fingerprint": self.strategy_fingerprint,
                "evidence_fingerprint": self.evidence_fingerprint,
                "expected_net_edge_bps": (None if self.expected_net_edge_bps is None
                                          else str(self.expected_net_edge_bps)),
                "strategy_compatibility": self.strategy_compatibility}


@dataclass(frozen=True, slots=True)
class CertifiedProductionUniverse:
    """Dynamically built set of tradable pairs, plus what was excluded and why."""

    entries: tuple[UniverseEntry, ...]
    exclusions: tuple[dict[str, str], ...] = ()
    multi_market_production: str = "DISABLED"

    @property
    def markets(self) -> tuple[str, ...]:
        return tuple(sorted({entry.market for entry in self.entries}))

    @property
    def pairs(self) -> tuple[str, ...]:
        return tuple(entry.key for entry in self.entries)

    @property
    def is_empty(self) -> bool:
        return not self.entries

    def market_quality_ok(self) -> dict[str, bool]:
        """Markets currently in the universe. Used to gate suspension checks."""
        return dict.fromkeys(self.markets, True)

    def public(self) -> dict[str, Any]:
        return {"version": UNIVERSE_VERSION, "entries": [e.public() for e in self.entries],
                "markets": list(self.markets), "size": len(self.entries),
                "exclusions": [dict(item) for item in self.exclusions],
                "multi_market_production": self.multi_market_production}


@financial
def build_certified_universe(*, certifications: tuple[PairCertification, ...],
                             current_market_data: dict[str, bool] | None = None,
                             fee_known: dict[str, bool] | None = None,
                             book_compatible: dict[str, bool] | None = None,
                             accounting_compatible: dict[str, bool] | None = None,
                             execution_compatible: dict[str, bool] | None = None,
                             profile_fingerprints: dict[str, tuple[str, str]] | None = None,
                             profile_markets: dict[str, frozenset[str]] | None = None,
                             ) -> CertifiedProductionUniverse:
    """Admit only pairs that are certified *and* currently operational.

    A certification is a statement about evidence. Admission is a statement about
    *now*: the market must still be readable, the fee known, the book compatible, and
    the accounting and execution paths compatible with it. A pair that earned
    certification but whose market is currently unreadable is excluded, because
    selecting it would mean posting into a market we cannot currently validate.
    """
    data_ok = current_market_data or {}
    fees_ok = fee_known or {}
    book_ok = book_compatible or {}
    accounting_ok = accounting_compatible or {}
    execution_ok = execution_compatible or {}
    fingerprints = profile_fingerprints or {}
    declared = profile_markets or {}

    entries: list[UniverseEntry] = []
    exclusions: list[dict[str, str]] = []
    for certification in sorted(certifications, key=lambda c: (c.market, c.profile_id)):
        market = certification.market
        profile_id = certification.profile_id
        if certification.state not in (CERTIFIED, PRODUCTION_CERTIFIABLE):
            exclusions.append({"market": market, "profile_id": profile_id,
                               "reason": certification.state})
            continue
        if classify_market(market) == CLASS_STABLE_OR_FIAT:
            exclusions.append({"market": market, "profile_id": profile_id,
                               "reason": REASON_FIAT_LIKE_MARKET})
            continue
        if not data_ok.get(market, False):
            exclusions.append({"market": market, "profile_id": profile_id,
                               "reason": REASON_NO_MARKET_DATA})
            continue
        if not fees_ok.get(market, False):
            exclusions.append({"market": market, "profile_id": profile_id,
                               "reason": REASON_NO_FEE_DATA})
            continue
        if not (book_ok.get(market, True) and accounting_ok.get(market, True)
                and execution_ok.get(market, True)):
            exclusions.append({"market": market, "profile_id": profile_id,
                               "reason": REASON_NOT_IN_UNIVERSE})
            continue
        profile_fp, strategy_fp = fingerprints.get(profile_id, ("", ""))
        markets = declared.get(profile_id, frozenset())
        compatibility = ("CERTIFIED_FOR_MARKET"
                         if market.replace("/", "_").lower() in markets
                         else "RESEARCH_ONLY")
        entries.append(UniverseEntry(
            market=market, profile_id=profile_id,
            certification_state=certification.state,
            profile_fingerprint=profile_fp, strategy_fingerprint=strategy_fp,
            evidence_fingerprint=(certification.evidence.telemetry()["dataset_fingerprints"][0]
                                  if certification.evidence
                                  and certification.evidence.dataset_fingerprints else ""),
            strategy_compatibility=compatibility))
    return CertifiedProductionUniverse(entries=tuple(entries),
                                       exclusions=tuple(exclusions))


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    """One candidate's outcome, with its deterministic reason code if refused."""

    market: str
    book: str
    profile_id: str
    certification_state: str
    decision: str
    eligible: bool
    reason_code: str
    expected_gross_edge_bps: Decimal
    expected_net_edge_bps: Decimal | None
    expected_net_pnl_mxn: Decimal | None
    spread_bps: Decimal
    rank_key: tuple[Decimal, Decimal, Decimal, str, str] | None = None

    def public(self) -> dict[str, Any]:
        return {"market": self.market, "book": self.book, "profile_id": self.profile_id,
                "certification_state": self.certification_state,
                "decision": self.decision, "eligible": self.eligible,
                "reason_code": self.reason_code,
                "expected_gross_edge_bps": str(self.expected_gross_edge_bps),
                "expected_net_edge_bps": (None if self.expected_net_edge_bps is None
                                          else str(self.expected_net_edge_bps)),
                "expected_net_pnl_mxn": (None if self.expected_net_pnl_mxn is None
                                         else str(self.expected_net_pnl_mxn)),
                "spread_bps": str(self.spread_bps)}


@dataclass(frozen=True, slots=True)
class SelectedProductionOpportunity:
    """The one opportunity chosen for Production, with the alternatives that lost."""

    outcome: str
    market: str
    book: str
    profile_id: str
    expected_gross_edge_bps: Decimal
    expected_net_edge_bps: Decimal
    expected_net_pnl_mxn: Decimal
    spread_bps: Decimal
    order_budget_mxn: Decimal
    alternatives: tuple[CandidateEvaluation, ...]
    reason_code: str = SELECTED

    @property
    def is_trade(self) -> bool:
        return self.outcome == SELECTED

    def public(self) -> dict[str, Any]:
        return {"version": SELECTOR_VERSION, "outcome": self.outcome,
                "reason_code": self.reason_code, "market": self.market,
                "book": self.book, "profile_id": self.profile_id,
                "expected_gross_edge_bps": str(self.expected_gross_edge_bps),
                "expected_net_edge_bps": str(self.expected_net_edge_bps),
                "expected_net_pnl_mxn": str(self.expected_net_pnl_mxn),
                "spread_bps": str(self.spread_bps),
                "order_budget_mxn": str(self.order_budget_mxn),
                "alternatives": [item.public() for item in self.alternatives],
                "one_unresolved_order_globally": True}


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """Result of a selection round: either one opportunity, or an explained NO_TRADE."""

    outcome: str
    reason_code: str
    selected: SelectedProductionOpportunity | None
    candidates: tuple[CandidateEvaluation, ...]

    @property
    def is_trade(self) -> bool:
        return self.outcome == SELECTED and self.selected is not None

    def public(self) -> dict[str, Any]:
        return {"version": SELECTOR_VERSION, "outcome": self.outcome,
                "reason_code": self.reason_code,
                "selected": None if self.selected is None else self.selected.public(),
                "candidates": [item.public() for item in self.candidates],
                "candidate_count": len(self.candidates),
                "eligible_count": sum(1 for item in self.candidates if item.eligible),
                "multi_market_production": "DISABLED",
                "one_unresolved_order_globally": True}


def _reason_for(candidate: CandidateMarket, context: MarketSelectionContext) -> str:
    """First failing check, in a fixed order. Deterministic and attributable."""
    if candidate.market_class == CLASS_STABLE_OR_FIAT:
        return REASON_FIAT_LIKE_MARKET
    if not candidate.market_data_available:
        return REASON_NO_MARKET_DATA
    if not candidate.fee_available or candidate.taker_fee_rate <= ZERO:
        return REASON_NO_FEE_DATA
    if candidate.certification.state == SUSPENDED:
        return REASON_SUSPENDED
    if not candidate.certification.selectable:
        if candidate.certification.state == "NOT_VIABLE":
            return REASON_NOT_VIABLE
        if candidate.certification.state == "INSUFFICIENT_EVIDENCE":
            return REASON_INSUFFICIENT_EVIDENCE
        return REASON_NOT_CERTIFIED
    if candidate.data_age_seconds > context.max_market_age_seconds:
        return REASON_STALE_DATA
    if candidate.spread_bps > context.max_spread_bps:
        return REASON_SPREAD_REJECT
    if candidate.decision != DECISION_BUY:
        return REASON_NO_SIGNAL
    if candidate.viability is not None and not candidate.viability.viable:
        return REASON_ECONOMIC_GUARD_REJECT
    if candidate.expected_net_edge_bps is None or candidate.expected_net_edge_bps <= ZERO:
        return REASON_NEGATIVE_NET_EDGE
    if candidate.bid_quantity_mxn < context.constraints.order_cap_mxn:
        return REASON_DEPTH_REJECT
    return ""


@financial
def rank_key(candidate: CandidateMarket) -> tuple[Decimal, Decimal, Decimal, str, str]:
    """Deterministic ranking key. Economics first, market quality only as a tie-break.

    The primary key is expected net edge *after* friction, because that is the thing
    the project optimises. Expected net P&L is second (absolute money, which accounts
    for order size), lower spread third, then market and profile id so that a tie
    resolves stably instead of depending on input order. There is no randomness and no
    market-score term in the primary position: the specification is explicit that
    selection must not be driven by MarketOpportunityScore.
    """
    net_edge = candidate.expected_net_edge_bps if candidate.expected_net_edge_bps is not None else ZERO
    net_pnl = candidate.expected_net_pnl_mxn if candidate.expected_net_pnl_mxn is not None else ZERO
    # Negated spread so that "smaller is better" sorts first under a descending sort.
    return (net_edge, net_pnl, -candidate.spread_bps, candidate.market, candidate.profile_id)


@financial
def select_production_opportunity(
    *, candidates: tuple[CandidateMarket, ...], context: MarketSelectionContext,
) -> SelectionResult:
    """Choose at most one opportunity, or NO_TRADE with an attributed reason.

    Global safety is checked before anything else: an unresolved order anywhere blocks
    every new intent, because the first requirement is that a second Production write
    can never be issued while the outcome of the first is unknown.
    """
    evaluated: list[CandidateEvaluation] = []
    if context.unresolved_orders >= context.max_unresolved_orders:
        for candidate in sorted(candidates, key=lambda c: (c.market, c.profile_id)):
            evaluated.append(_evaluation(candidate, eligible=False,
                                         reason=REASON_UNRESOLVED_ORDER))
        return SelectionResult(outcome=NO_TRADE, reason_code=REASON_UNRESOLVED_ORDER,
                               selected=None, candidates=tuple(evaluated))

    eligible: list[CandidateMarket] = []
    for candidate in sorted(candidates, key=lambda c: (c.market, c.profile_id)):
        reason = _reason_for(candidate, context)
        if reason:
            evaluated.append(_evaluation(candidate, eligible=False, reason=reason))
            continue
        if candidate.market in context.open_markets:
            evaluated.append(_evaluation(candidate, eligible=False,
                                         reason=REASON_ALREADY_HOLDING))
            continue
        if context.constraints.order_cap_mxn <= ZERO:
            evaluated.append(_evaluation(candidate, eligible=False,
                                         reason=REASON_CAPITAL_REJECT))
            continue
        if candidate.minimum_value_mxn > context.constraints.order_cap_mxn:
            evaluated.append(_evaluation(candidate, eligible=False,
                                         reason=REASON_POSITION_CONSTRAINT))
            continue
        eligible.append(candidate)
        evaluated.append(_evaluation(candidate, eligible=True, reason=""))

    if not eligible:
        # NO_TRADE is a real answer, never "the least bad market".
        reason = (evaluated[0].reason_code if evaluated else REASON_NOT_IN_UNIVERSE)
        return SelectionResult(outcome=NO_TRADE, reason_code=reason, selected=None,
                               candidates=tuple(evaluated))

    winner = max(eligible, key=rank_key)
    # The budget is the global order cap, which already folds in deployment headroom,
    # available cash and the single-order cap. It is never per-market.
    budget = context.constraints.order_cap_mxn
    selection = SelectedProductionOpportunity(
        outcome=SELECTED, market=winner.market, book=winner.book,
        profile_id=winner.profile_id,
        expected_gross_edge_bps=winner.expected_gross_edge_bps,
        expected_net_edge_bps=(winner.expected_net_edge_bps if winner.expected_net_edge_bps is not None else ZERO),
        expected_net_pnl_mxn=(winner.expected_net_pnl_mxn if winner.expected_net_pnl_mxn is not None else ZERO),
        spread_bps=winner.spread_bps, order_budget_mxn=budget,
        alternatives=tuple(evaluated))
    return SelectionResult(outcome=SELECTED, reason_code=SELECTED, selected=selection,
                           candidates=tuple(evaluated))


def _evaluation(candidate: CandidateMarket, *, eligible: bool, reason: str) -> CandidateEvaluation:
    return CandidateEvaluation(
        market=candidate.market, book=candidate.book, profile_id=candidate.profile_id,
        certification_state=candidate.certification.state, decision=candidate.decision,
        eligible=eligible, reason_code=reason,
        expected_gross_edge_bps=candidate.expected_gross_edge_bps,
        expected_net_edge_bps=candidate.expected_net_edge_bps,
        expected_net_pnl_mxn=candidate.expected_net_pnl_mxn,
        spread_bps=candidate.spread_bps,
        rank_key=rank_key(candidate) if eligible else None)


__all__ = [
    "CERTIFIED",
    "NO_TRADE",
    "REASON_ALREADY_HOLDING",
    "REASON_CAPITAL_REJECT",
    "REASON_DEPTH_REJECT",
    "REASON_ECONOMIC_GUARD_REJECT",
    "REASON_FIAT_LIKE_MARKET",
    "REASON_INSUFFICIENT_EVIDENCE",
    "REASON_NEGATIVE_NET_EDGE",
    "REASON_NOT_CERTIFIED",
    "REASON_NOT_IN_UNIVERSE",
    "REASON_NOT_VIABLE",
    "REASON_NO_FEE_DATA",
    "REASON_NO_MARKET_DATA",
    "REASON_NO_SIGNAL",
    "REASON_POSITION_CONSTRAINT",
    "REASON_SPREAD_REJECT",
    "REASON_STALE_DATA",
    "REASON_SUSPENDED",
    "REASON_UNRESOLVED_ORDER",
    "SELECTED",
    "SELECTOR_VERSION",
    "SUPPORTED",
    "UNIVERSE_VERSION",
    "CandidateEvaluation",
    "CandidateMarket",
    "CertifiedProductionUniverse",
    "MarketSelectionContext",
    "PortfolioConstraints",
    "SelectedProductionOpportunity",
    "SelectionResult",
    "UniverseEntry",
    "build_certified_universe",
    "rank_key",
    "select_production_opportunity",
]

# Kept for API stability: the decision vocabulary a caller may see.
SUPPORTED = frozenset({DECISION_BUY, DECISION_NO_SIGNAL})

"""Top-level product state: portfolio, engineering, research and production, kept apart.

This module answers the four questions an operator actually has when they open AutoFund, and it
answers them in four separate blocks rather than one composite score:

    PORTFOLIO     what capital is exposed?          (read from authoritative accounting)
    ENGINEERING   is the machinery working?         (read from runtime state)
    RESEARCH      what has been learned?            (read from the registry)
    PRODUCTION    may money move, and why not?      (read from authorization state)

**Why not one status.** It is tempting to collapse these into a single green/amber/red. Doing so
would destroy the exact information the product exists to convey: a system can be engineering-
healthy, have a validated predictive signal, and be Production-disabled, all at once — which is
precisely AutoFund's current state. A single colour would have to hide two of those three facts.

**Wallets are not inventory.** The exchange wallet holds whatever the account holds. AutoFund's
position is what its own ledger says it bought. Conflating them would credit the project with
assets it does not own and never bought, so they are separate fields and never summed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .models import NOT_RECORDED, UNKNOWN, ResearchRegistry

OVERVIEW_VERSION = "autofund.research-overview.v1"

# ---- the separated statuses ----
HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
BLOCKED = "BLOCKED"

IDLE = "IDLE"
RUNNING = "RUNNING"
ACCUMULATING = "ACCUMULATING"
CANDIDATE_FOUND = "CANDIDATE_FOUND"

DISABLED = "DISABLED"
READY = "READY"
SESSION_AUTHORIZED = "SESSION_AUTHORIZED"

# The single action an operator should take, and the honest answer is usually NO_TRADE.
NO_TRADE = "NO_TRADE"
TRADE_AUTHORIZED = "TRADE_AUTHORIZED"

# **Authorization is an allowlist, and it fails closed.**
#
# The first implementation asked whether the app state was outside a small set of *inactive*
# states, which meant every state it had not heard of — including `BOOTING` — counted as authorized.
# That is the wrong direction for the one question that decides whether money may move: an
# unrecognised state must be treated as *not* authorized, because the cost of wrongly permitting a
# session is categorically higher than the cost of wrongly blocking one.
#
# Only `RUNNING` means a session is authorized. `STARTING` and `STOPPING` are transitional and a
# trade is not yet or no longer sanctioned; `BOOTING`, `RECOVERING`, `HALTED` and `ERROR` are all
# states in which no session was authorized.
SESSION_AUTHORIZING_STATES: frozenset[str] = frozenset({"RUNNING"})

# The accounting statuses the ledger reports when it is reconciled. `PASS` is what the orchestrator
# actually emits; the others are accepted so a future naming change does not silently degrade the
# health report to UNKNOWN. Anything not listed is DEGRADED rather than UNKNOWN, because the ledger
# having said something unrecognised is itself a problem worth surfacing.
LEDGER_HEALTHY_STATUSES: frozenset[str] = frozenset({"PASS", "OK", "RECONCILED"})


@dataclass(frozen=True, slots=True)
class PortfolioView:
    """Capital exposure, reconstructed from authoritative accounting.

    `autofund_inventory` and `exchange_wallet` are deliberately different shapes and are never
    added together. The ledger's position is what the project owns; the wallet is what the account
    holds, and the account may hold assets the project never bought.
    """

    cash_mxn: Decimal | None = None
    equity_mxn: Decimal | None = None
    deployed_mxn: Decimal | None = None
    authorized_capital_mxn: Decimal | None = None
    max_deployment_mxn: Decimal | None = None
    single_order_cap_mxn: Decimal | None = None
    realized_pnl_mxn: Decimal | None = None
    unrealized_pnl_mxn: Decimal | None = None
    fees_mxn: Decimal | None = None
    fills: int | None = None
    orders: int | None = None
    autofund_inventory: tuple[dict[str, Any], ...] = ()
    exchange_wallet: dict[str, Any] = field(default_factory=dict)
    unresolved_orders: tuple[dict[str, Any], ...] = ()
    accounting_status: str = UNKNOWN
    source: str = "authoritative ledger and journal"

    @property
    def remaining_deployment_mxn(self) -> Decimal | None:
        """How much more could be deployed inside the authorisation envelope."""
        if self.max_deployment_mxn is None or self.deployed_mxn is None:
            return None
        return self.max_deployment_mxn - self.deployed_mxn

    @property
    def has_unresolved_order(self) -> bool:
        return len(self.unresolved_orders) > 0

    def public(self) -> dict[str, Any]:
        return {
            "cash_mxn": _s(self.cash_mxn), "equity_mxn": _s(self.equity_mxn),
            "deployed_mxn": _s(self.deployed_mxn),
            "remaining_deployment_mxn": _s(self.remaining_deployment_mxn),
            "authorized_capital_mxn": _s(self.authorized_capital_mxn),
            "max_deployment_mxn": _s(self.max_deployment_mxn),
            "single_order_cap_mxn": _s(self.single_order_cap_mxn),
            "realized_pnl_mxn": _s(self.realized_pnl_mxn),
            "unrealized_pnl_mxn": _s(self.unrealized_pnl_mxn),
            "fees_mxn": _s(self.fees_mxn), "fills": self.fills, "orders": self.orders,
            "autofund_inventory": list(self.autofund_inventory),
            "exchange_wallet": dict(self.exchange_wallet),
            "unresolved_orders": list(self.unresolved_orders),
            "has_unresolved_order": self.has_unresolved_order,
            "accounting_status": self.accounting_status,
            "wallet_is_not_inventory": True,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class EngineeringView:
    """Whether the machinery works. Nothing here says anything about profitability."""

    app_state: str = UNKNOWN
    execution: str = UNKNOWN
    ledger: str = UNKNOWN
    market_data: str = UNKNOWN
    collectors: str = UNKNOWN
    control_state: str = UNKNOWN
    overall: str = UNKNOWN
    detail: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {"app_state": self.app_state, "execution": self.execution,
                "ledger": self.ledger, "market_data": self.market_data,
                "collectors": self.collectors, "control_state": self.control_state,
                "overall": self.overall, "detail": dict(self.detail),
                "says_nothing_about_profitability": True}


@dataclass(frozen=True, slots=True)
class ResearchView:
    """What has been learned, in the vocabulary the milestones actually used."""

    price_only: str = UNKNOWN
    cross_market_alpha: str = UNKNOWN
    microstructure: str = UNKNOWN
    cross_venue: str = UNKNOWN
    experiments_total: int = 0
    experiments_completed: int = 0
    validated_information_signals: int = 0
    economically_usable_signals: int = 0
    strategies_frozen: int = 0
    strategies_not_viable: int = 0
    strategies_active_production: int = 0
    active_campaigns: tuple[str, ...] = ()
    accumulating_campaigns: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "price_only": self.price_only, "cross_market_alpha": self.cross_market_alpha,
            "microstructure": self.microstructure, "cross_venue": self.cross_venue,
            "experiments_total": self.experiments_total,
            "experiments_completed": self.experiments_completed,
            "validated_information_signals": self.validated_information_signals,
            "economically_usable_signals": self.economically_usable_signals,
            "strategies_frozen": self.strategies_frozen,
            "strategies_not_viable": self.strategies_not_viable,
            "strategies_active_production": self.strategies_active_production,
            "active_campaigns": list(self.active_campaigns),
            "accumulating_campaigns": list(self.accumulating_campaigns),
        }


@dataclass(frozen=True, slots=True)
class ProductionView:
    """Authorization: whether money may move, and the concrete reason when it may not.

    `current_action` is the plainest statement the product makes. When nothing is certified it
    reads NO_TRADE, and that is a finding rather than a failure.
    """

    certified_opportunities: int = 0
    production_eligible: int = 0
    session_authorized: bool = False
    unresolved_orders: int = 0
    current_action: str = NO_TRADE
    reasons: tuple[str, ...] = ()
    authorization_state: str = DISABLED
    demo_mode: bool = True
    auto_execution: bool = False

    def public(self) -> dict[str, Any]:
        return {"certified_opportunities": self.certified_opportunities,
                "production_eligible": self.production_eligible,
                "session_authorized": self.session_authorized,
                "unresolved_orders": self.unresolved_orders,
                "current_action": self.current_action, "reasons": list(self.reasons),
                "authorization_state": self.authorization_state,
                "demo_mode": self.demo_mode, "auto_execution": self.auto_execution,
                "evidence_does_not_imply_authorization": True}


@dataclass(frozen=True, slots=True)
class ProductStatus:
    """The three separated summary states, plus the action, and never one blended light."""

    engineering: str
    research: str
    production: str
    current_action: str
    built_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def public(self) -> dict[str, Any]:
        return {"engineering": self.engineering, "research": self.research,
                "production": self.production, "current_action": self.current_action,
                "built_at": self.built_at,
                "statuses_are_independent": True,
                "note": ("engineering health, evidence quality, economic result and production "
                         "authorization are four separate facts and are never combined")}


@dataclass(frozen=True, slots=True)
class ControlCenterOverview:
    portfolio: PortfolioView
    engineering: EngineeringView
    research: ResearchView
    production: ProductionView
    status: ProductStatus
    latest_experiment: dict[str, Any] | None = None
    latest_evidence: tuple[dict[str, Any], ...] = ()
    schema_version: str = OVERVIEW_VERSION

    def public(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "status": self.status.public(),
                "portfolio": self.portfolio.public(),
                "engineering": self.engineering.public(),
                "research": self.research.public(),
                "production": self.production.public(),
                "latest_experiment": self.latest_experiment,
                "latest_evidence": list(self.latest_evidence)}


def build_portfolio(*, snapshot: dict[str, Any],
                    unresolved_orders: tuple[dict[str, Any], ...] = ()
                    ) -> PortfolioView:
    """Read exposure from the authoritative snapshot.

    The orchestrator's snapshot comes from the ledger and journal; this function reorganises it and
    adds nothing. Every value that is absent stays absent rather than defaulting to zero, because a
    zero cash balance and an unread one are different facts.
    """
    position = snapshot.get("position")
    inventory: list[dict[str, Any]] = []
    if isinstance(position, dict) and position:
        # A position is what the ledger says was bought, and it is reported as owned inventory.
        inventory.append({
            "asset": position.get("asset", "BTC"),
            "quantity": position.get("quantity"),
            "cost_basis_mxn": position.get("cost_basis_mxn"),
            "average_cost_mxn": position.get("average_cost_mxn"),
            "mark_price_mxn": position.get("mark_price_mxn"),
            "unrealized_pnl_mxn": position.get("unrealized_pnl_mxn"),
            "status": "OPEN",
            "owned_by": "AUTOFUND",
        })
    wallet = snapshot.get("wallet")
    wallet_view: dict[str, Any] = wallet if isinstance(wallet, dict) else {
        "status": UNKNOWN, "balances": []}
    return PortfolioView(
        cash_mxn=_dec(snapshot.get("cash_mxn")),
        equity_mxn=_dec(snapshot.get("equity_mxn")),
        deployed_mxn=_dec(snapshot.get("deployed_mxn")),
        authorized_capital_mxn=_dec(snapshot.get("authorized_capital_mxn")),
        max_deployment_mxn=_dec(snapshot.get("max_deployment_mxn")),
        single_order_cap_mxn=_dec(snapshot.get("single_order_cap_mxn")),
        realized_pnl_mxn=_dec(snapshot.get("realized_pnl_mxn")),
        unrealized_pnl_mxn=_dec((position or {}).get("unrealized_pnl_mxn")
                                if isinstance(position, dict) else None),
        fees_mxn=_dec(snapshot.get("fees_mxn")),
        fills=_int(snapshot.get("fills")), orders=_int(snapshot.get("orders")),
        autofund_inventory=tuple(inventory), exchange_wallet=wallet_view,
        unresolved_orders=unresolved_orders,
        accounting_status=str(snapshot.get("accounting_status", UNKNOWN)))


def build_engineering(*, snapshot: dict[str, Any], campaign_health: tuple[str, ...] = ()
                      ) -> EngineeringView:
    """Derive machinery health. Never inspects any economic result."""
    blocked_recovery = snapshot.get("blocked_recovery") or {}
    accounting = str(snapshot.get("accounting_status", UNKNOWN))
    if accounting in LEDGER_HEALTHY_STATUSES:
        ledger = HEALTHY
    elif accounting == UNKNOWN:
        ledger = UNKNOWN
    else:
        # The ledger reported something not recognised as reconciled. That is a real finding, so it
        # is DEGRADED rather than UNKNOWN — the difference between "we could not ask" and "the
        # answer was not what it should be".
        ledger = DEGRADED
    market_data = str(snapshot.get("market_quality") or UNKNOWN)
    execution = (DEGRADED if blocked_recovery.get("blocked") else HEALTHY)
    if campaign_health and all(item == HEALTHY for item in campaign_health):
        collectors = HEALTHY
    elif any(item == DEGRADED for item in campaign_health):
        collectors = DEGRADED
    elif campaign_health:
        collectors = DEGRADED
    else:
        collectors = UNKNOWN
    app_state = str(snapshot.get("app_state", UNKNOWN))
    control = "IDLE" if app_state in ("STOPPED", "IDLE") else app_state
    states = (execution, ledger, collectors)
    if BLOCKED in states or blocked_recovery.get("blocked"):
        overall = BLOCKED
    elif DEGRADED in states or UNKNOWN in states:
        overall = DEGRADED
    else:
        overall = HEALTHY
    return EngineeringView(
        app_state=app_state, execution=execution, ledger=ledger, market_data=market_data,
        collectors=collectors, control_state=control, overall=overall,
        detail={"blocked_recovery": bool(blocked_recovery.get("blocked")),
                "runtime_gaps": len(snapshot.get("runtime_gaps") or [])})


def build_research(*, registry: ResearchRegistry) -> ResearchView:
    """Summarise the registry. Every value is a count or a status the records themselves hold."""
    def alpha_status(alpha_id: str) -> str:
        record = registry.alpha(alpha_id)
        return str(record.classification) if record else NOT_RECORDED

    validated = [a for a in registry.alpha_sources
                 if a.prediction_status.value == "PREDICTIVE"]
    usable = [a for a in validated if a.tradeable is True]
    running = tuple(c.campaign_id for c in registry.campaigns
                    if c.status.value == "RUNNING")
    accumulating = tuple(c.campaign_id for c in registry.campaigns
                         if (c.coverage_sufficient is False and c.observations is not None)
                         or (c.coverage_sufficient is False))
    return ResearchView(
        price_only=alpha_status("PRICE_ONLY_RESEARCH_BASELINE"),
        cross_market_alpha=alpha_status("VALIDATED_INFORMATION_SIGNAL_V1"),
        microstructure=alpha_status("MICROSTRUCTURE_ORDER_FLOW"),
        cross_venue=alpha_status("CROSS_VENUE_DISLOCATION"),
        experiments_total=len(registry.experiments),
        experiments_completed=sum(1 for e in registry.experiments
                                  if e.status.value == "COMPLETED"),
        validated_information_signals=len(validated),
        economically_usable_signals=len(usable),
        strategies_frozen=sum(1 for s in registry.strategies if s.is_frozen_terminal),
        strategies_not_viable=sum(1 for s in registry.strategies if s.status.value == "NOT_VIABLE"),
        strategies_active_production=sum(1 for s in registry.strategies
                                         if s.status.value == "ACTIVE_PRODUCTION"),
        active_campaigns=running, accumulating_campaigns=accumulating)


def build_production(*, snapshot: dict[str, Any], registry: ResearchRegistry,
                     unresolved_orders: tuple[dict[str, Any], ...]
                     ) -> ProductionView:
    """Authorization state, with the reason it is what it is.

    Certified opportunities are counted from the registry's eligibility records, which each carry a
    concrete blocking reason. Positive research evidence never sets `session_authorized`: that comes
    from the runtime's own control state, and a test asserts the two cannot influence each other.
    """
    eligible = [item for item in registry.eligibility if item.eligible]
    reasons: list[str] = []
    for item in registry.eligibility:
        if item.eligible:
            continue
        if item.blocking_reason and item.blocking_reason not in reasons:
            reasons.append(item.blocking_reason)
    app_state = str(snapshot.get("app_state", UNKNOWN))
    # Fail closed: only a state on the allowlist authorizes a session.
    session_authorized = app_state in SESSION_AUTHORIZING_STATES
    enabled = not bool(snapshot.get("demo_mode", True))
    authorization = (SESSION_AUTHORIZED if session_authorized
                     else (READY if eligible and enabled else DISABLED))
    return ProductionView(
        certified_opportunities=sum(1 for item in registry.eligibility
                                    if item.certification_status == "CERTIFIED"),
        production_eligible=len(eligible),
        session_authorized=session_authorized,
        unresolved_orders=len(unresolved_orders),
        current_action=(TRADE_AUTHORIZED if (eligible and session_authorized) else NO_TRADE),
        reasons=tuple(reasons),
        authorization_state=authorization,
        demo_mode=bool(snapshot.get("demo_mode", True)),
        auto_execution=bool(snapshot.get("auto_execution", False)))


def build_overview(*, registry: ResearchRegistry, snapshot: dict[str, Any],
                   unresolved_orders: tuple[dict[str, Any], ...] = ()
                   ) -> ControlCenterOverview:
    """Assemble the four blocks and derive the three independent summary states."""
    portfolio = build_portfolio(snapshot=snapshot, unresolved_orders=unresolved_orders)
    engineering = build_engineering(snapshot=snapshot,
                                    campaign_health=tuple(
                                        c.process_health for c in registry.campaigns
                                        if c.process_health != UNKNOWN))
    research = build_research(registry=registry)
    production = build_production(snapshot=snapshot, registry=registry,
                                 unresolved_orders=unresolved_orders)

    if engineering.overall == BLOCKED:
        research_state = IDLE
    elif research.active_campaigns:
        research_state = RUNNING
    elif research.accumulating_campaigns:
        research_state = ACCUMULATING
    elif research.economically_usable_signals > 0:
        research_state = CANDIDATE_FOUND
    else:
        research_state = IDLE

    status = ProductStatus(
        engineering=engineering.overall, research=research_state,
        production=production.authorization_state,
        current_action=production.current_action)

    completed = [e for e in registry.experiments if e.finished_at]
    latest = max(completed, key=lambda e: e.finished_at or "", default=None)
    latest_evidence = tuple(
        item.public() for item in sorted(
            registry.evidence,
            key=lambda e: (e.end or e.start or ""), reverse=True)[:5])
    return ControlCenterOverview(
        portfolio=portfolio, engineering=engineering, research=research,
        production=production, status=status,
        latest_experiment=latest.public() if latest else None,
        latest_evidence=latest_evidence)


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _int(value: Any) -> int | None:
    parsed = _dec(value)
    return None if parsed is None else int(parsed)


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)

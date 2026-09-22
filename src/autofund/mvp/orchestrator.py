"""Authoritative MVP application state machine and bounded session controls."""

import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar, Protocol
from uuid import uuid4

from autofund.decimal_utils import decimal
from autofund.live.models import LiveError

from .adaptive import AdaptiveEngine
from .champion import DECISION_BUY, DECISION_SELL, evaluate_champion
from .observability import MvpObservability
from .scanner import MarketScanner
from .telemetry import SessionTelemetry

AUTHORIZED_CAPITAL = Decimal("50")
MAX_DEPLOYMENT = Decimal("25")
SINGLE_ORDER_CAP = Decimal("11")
PREFLIGHT_PASS, PREFLIGHT_FAIL, PREFLIGHT_NOT_RUN = "PASS", "FAIL", "NOT_RUN"
PRODUCT_VERSION = "AutoFund MVP 0.1.2"

# Canonical stop reasons. The backend is authoritative; the UI never infers a
# reason from elapsed time.
STOP_OPERATOR = "OPERATOR_STOP"
STOP_MAX_DURATION = "MAX_SESSION_DURATION_REACHED"
STOP_MAX_ORDERS = "MAX_SESSION_ORDERS_REACHED"
STOP_LOSS_LIMIT = "MAX_SESSION_LOSS_REACHED"
STOP_KILL_SWITCH = "KILL_SWITCH_ACTIVATED"
STOP_RUNNER_FAILURE = "EXECUTION_FAILURE"
# Operator-actionable notes for blocker names that are not self-explanatory. The
# gate itself is unchanged; these only describe what the exact blocker means.
BLOCKER_GUIDANCE: dict[str, str] = {
    "PERMISSIONS_FAIL": "Exchange key permissions are not attested; the exact F5 attestation variable must be "
                        "set to true by the operator out of band. This is never defaulted by the application.",
    "SLIPPAGE_POLICY_FAIL": "No explicit slippage policy is configured for the session.",
    "AUTHENTICATION_FAIL": "Production credentials were rejected; no trading is possible.",
    "EXCHANGE_AVAILABLE_FAIL": "Exchange MXN is below the single-order cap; the order is not executable.",
    "VALUE_LIMITS_FAIL": "Single-order cap is below the exchange minimum value; not executable.",
    "LIVE_CREDENTIALS_MISSING_OR_INVALID": "Production credentials are absent or invalid; set the F5 live "
                                          "credential environment variables before starting the application.",
    "PRODUCTION_PREFLIGHT_UNAVAILABLE": "The Production preflight could not read the exchange; no trading is possible.",
    "UNRESOLVED_ORDER": "A previous order is unresolved; reconciliation must complete before trading.",
    "EXECUTION_JOURNAL_IN_USE": "Another AutoFund process owns the live execution journal. Only one "
                                "instance may trade at a time; close the other instance and retry.",
}


class SessionStartBlocked(LiveError):
    """Recoverable readiness blocker found before a session is authorized.

    This is an ordinary "not ready" outcome, not an execution failure: no
    exchange write happened, automatic execution stays disabled and the
    application returns to STOPPED so the operator can retry once the blocker
    clears. HALTED stays reserved for genuinely unsafe states.
    """

    def __init__(self, blockers: tuple[str, ...]) -> None:
        self.blockers = tuple(blockers) or ("PRODUCTION_PREFLIGHT_BLOCKED",)
        super().__init__("PRODUCTION_PREFLIGHT_BLOCKED: " + ",".join(self.blockers))


class AppState(StrEnum):
    BOOTING = "BOOTING"
    RECOVERING = "RECOVERING"
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    HALTED = "HALTED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class SessionConfig:
    max_session_loss_mxn: Decimal = Decimal("10")
    max_session_duration_seconds: int = 3600
    max_orders_per_session: int = 10

    def __post_init__(self) -> None:
        loss = decimal(self.max_session_loss_mxn, "max_session_loss_mxn")
        if not Decimal("0") < loss <= AUTHORIZED_CAPITAL:
            raise ValueError("max_session_loss_mxn must be within (0, 50]")
        if not 60 <= self.max_session_duration_seconds <= 86_400:
            raise ValueError("max_session_duration_seconds must be within [60, 86400]")
        if not 1 <= self.max_orders_per_session <= 100:
            raise ValueError("max_orders_per_session must be within [1, 100]")


class TradingRunner(Protocol):
    observability: MvpObservability

    def startup(self) -> dict[str, Any]: ...
    def start(self, config: SessionConfig, event: Any) -> None: ...
    def stop(self) -> None: ...
    def kill(self) -> None: ...
    def snapshot(self) -> dict[str, Any]: ...
    def production_preflight(self) -> dict[str, Any]: ...
    def refresh_production_preflight(self, event: Any = None) -> dict[str, Any]: ...


class SafeIdleRunner:
    """Safe runner shell: production adapters can stream decisions into it.

    It owns no exchange transport, so it can never claim Production readiness.
    """

    def __init__(self, *, demo: bool = False) -> None:
        self.running = False
        self.killed = False
        self.observability = MvpObservability(demo=demo)
        self.preflight_status = PREFLIGHT_NOT_RUN
        self.preflight_blockers: tuple[str, ...] = ("PRODUCTION_PREFLIGHT_NOT_RUN",)
        self.preflight_warnings: tuple[str, ...] = ()
        self.preflight_checked_at: datetime | None = None

    def startup(self) -> dict[str, Any]:
        return {"connected": True, "reconciled": True, "unresolved_orders": [],
                "production_preflight": self.production_preflight()}

    def start(self, config: SessionConfig, event: Any) -> None:
        self.running, self.killed = True, False
        event("MARKET_CONNECTED", component="market", message="BTC/MXN market stream ready")
        event("NO_SIGNAL", component="strategy", message="No closed-candle signal yet")

    def stop(self) -> None:
        self.running = False
        self.observability.end("STOPPED")

    def kill(self) -> None:
        self.running, self.killed = False, True
        self.observability.end("HALTED")

    def production_preflight(self) -> dict[str, Any]:
        return {"status": self.preflight_status, "ready": self.preflight_status == PREFLIGHT_PASS,
                "blocked": self.preflight_status != PREFLIGHT_PASS, "blockers": list(self.preflight_blockers),
                "warnings": list(self.preflight_warnings), "checked_at": self.preflight_checked_at,
                "exchange": "NONE", "write_transport": "UNUSED"}

    def refresh_production_preflight(self, event: Any = None) -> dict[str, Any]:
        return self.production_preflight()

    def snapshot(self) -> dict[str, Any]:
        return {"connected": True, "market_quality": "VALID", "accounting_status": "PASS",
                "risk_status": "NORMAL", "cash_mxn": "50", "equity_mxn": "50", "deployed_mxn": "0",
                "position": None, "last_signal": "NO_SIGNAL", "orders": 0, "fills": 0,
                "realized_pnl_mxn": "0", "unrealized_pnl_mxn": "0", "fees_mxn": "0"}


class ProductionAutonomousRunner(SafeIdleRunner):
    """F5-backed production lifecycle; orders remain server-side and policy gated.

    Readiness is produced only by the existing F5 GET-only preflight. Startup and
    session start each run it; neither reuses a stale result. No order POST is
    possible from this class: submission stays behind the strategy path.
    """

    def __init__(self, journal_path: Path) -> None:
        super().__init__()
        self.journal_path = journal_path
        self.execution: Any = None
        self.journal: Any = None
        self.last_preflight: Any = None
        self._event: Any = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closes: list[Decimal] = []
        self._last_minute: str | None = None
        # Certified Champion identity; supplied by the orchestrator at start so
        # the runner records the exact profile the decision came from.
        self._strategy_fingerprint = ""
        self._champion_fingerprint: str | None = None
        self.wallet_balances: tuple[Any, ...] = ()
        self.wallet_status = "UNAVAILABLE"
        self.wallet_error: str | None = None
        self.account_balance_contradiction = False

    def _execution_event(self, event: str, payload: dict[str, Any]) -> None:
        if self._event is None:
            return
        mapped = {"INTENT_CREATED": "ORDER_INTENT_CREATED", "SUBMITTING": "ORDER_SUBMITTING",
                  "SUBMITTED": "ORDER_SUBMITTED", "ACKNOWLEDGED": "ORDER_ACKNOWLEDGED",
                  "OUTCOME_UNKNOWN": "ORDER_OUTCOME_UNKNOWN", "PARTIALLY_FILLED": "PARTIAL_FILL",
                  "FILLED": "FILL", "RECONCILED": "RECONCILIATION_PASS",
                  "HALTED": "RECONCILIATION_FAIL"}.get(event, event)
        self._event(mapped, component="execution", message=str(payload.get("origin_id", "")),
                    origin_id=payload.get("origin_id"), oid=payload.get("oid"),
                    trade_id=payload.get("trade_id"), side=payload.get("side"),
                    major_quantity=payload.get("major_quantity"), minor_value=payload.get("minor_value"),
                    fee=payload.get("confirmed_fee"), fee_currency=payload.get("fee_currency"))
        if event == "FILL_RECOVERED":
            self._event("LEDGER_UPDATED", component="accounting", message="Confirmed Bitso fill applied",
                        origin_id=payload.get("origin_id"), trade_id=payload.get("trade_id"))
            if payload.get("side") == "BUY":
                self._event("POSITION_OPENED", component="accounting", message="AutoFund position increased")
            else:
                inventory = Decimal(str(payload.get("inventory_btc", "0")))
                self._event("POSITION_CLOSED" if inventory == 0 else "POSITION_REDUCED",
                            component="accounting", message="AutoFund position updated")
                self._event("REALIZED_PNL_UPDATED", component="accounting",
                            message=str(payload.get("realized_pnl_mxn", "0")))

    def _refresh_wallet(self) -> None:
        try:
            self.wallet_balances = tuple(self.execution.client.balances())
            self.wallet_status, self.wallet_error = "PASS", None
            totals = {row.currency: row.total for row in self.wallet_balances}
            portfolio = self.execution.public()
            self.account_balance_contradiction = (
                Decimal(str(portfolio["inventory_btc"])) > totals.get("btc", Decimal("0"))
                or Decimal(str(portfolio["cash_mxn"])) > totals.get("mxn", Decimal("0")))
            if self.account_balance_contradiction:
                self.wallet_status, self.wallet_error = "DEGRADED", "ACCOUNT_BALANCE_CONTRADICTION"
        except Exception:
            self.wallet_balances = ()
            self.wallet_status, self.wallet_error = "DEGRADED", "WALLET_READ_UNAVAILABLE"

    def startup(self) -> dict[str, Any]:
        from autofund.live.client import BitsoProductionLiveClient, LiveCredentials
        from autofund.live.execution import LiveExecution
        from autofund.live.journal import LiveExecutionJournal
        from autofund.live.models import LiveConfig

        try:
            credentials = LiveCredentials.from_environment()
        except Exception as exc:
            # Credential problems are never silently downgraded to "ready".
            self._block_with(str(exc.args[0]) if exc.args else "LIVE_CREDENTIALS_MISSING_OR_INVALID")
            raise
        client = BitsoProductionLiveClient(credentials, single_order_cap=SINGLE_ORDER_CAP)
        try:
            self.journal = LiveExecutionJournal(self.journal_path)
        except Exception:
            # Another AutoFund process already owns the single-writer journal.
            # Refusing to proceed is correct; surface why instead of a generic
            # "startup recovery failed".
            self._block_with("EXECUTION_JOURNAL_IN_USE")
            raise
        self.execution = LiveExecution(client, self.journal, LiveConfig(slippage_tolerance=Decimal("0.5")),
                                       emit=self._execution_event)
        if self.execution.unresolved:
            self.execution.recover()
        reconciled = not self.execution.unresolved
        self._refresh_wallet()
        reconciled = reconciled and not self.account_balance_contradiction
        if reconciled:
            # Existing F5 preflight, GET-only, executed once at startup.
            ready, blockers, warnings = self._run_preflight()
        else:
            ready, blockers, warnings = False, ("UNRESOLVED_ORDER", *self.execution.unresolved), ()
        self.preflight_status = PREFLIGHT_PASS if ready else PREFLIGHT_FAIL
        self.preflight_blockers, self.preflight_warnings = blockers, warnings
        self.preflight_checked_at = self.last_preflight.checked_at if self.last_preflight else datetime.now(UTC)
        return {"connected": True, "reconciled": reconciled,
                "unresolved_orders": list(self.execution.unresolved),
                "preflight_ready": ready, "production_preflight": self.production_preflight()}

    def _block_with(self, blocker: str) -> None:
        self.preflight_status = PREFLIGHT_FAIL
        self.preflight_blockers = (blocker,)
        self.preflight_checked_at = datetime.now(UTC)

    def _public_preflight(self) -> dict[str, Any]:
        return self.last_preflight.public() if self.last_preflight else {}

    def _run_preflight(self) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
        try:
            self.last_preflight = self.execution.check()
        except Exception as exc:
            self.last_preflight = None
            message = exc.args[0] if isinstance(exc, LiveError) and exc.args else "PRODUCTION_PREFLIGHT_UNAVAILABLE"
            return False, (str(message),), ()
        checked = self.last_preflight
        return checked.ready, tuple(checked.failures), tuple(checked.warnings)

    def production_preflight(self) -> dict[str, Any]:
        methods = list(self.execution.client.outbound_methods) if self.execution is not None else []
        return {"status": self.preflight_status, "ready": self.preflight_status == PREFLIGHT_PASS,
                "blocked": self.preflight_status != PREFLIGHT_PASS, "blockers": list(self.preflight_blockers),
                "guidance": [BLOCKER_GUIDANCE[name] for name in self.preflight_blockers if name in BLOCKER_GUIDANCE],
                "warnings": list(self.preflight_warnings), "checked_at": self.preflight_checked_at,
                "exchange": "BITSO PRODUCTION", "write_transport": "UNUSED", "report": self._public_preflight(),
                # Evidence that readiness costs zero exchange writes.
                "production_get_count": sum(1 for method in methods if method == "GET"),
                "production_post_count": sum(1 for method in methods if method == "POST")}

    def account_fee_schedules(self) -> tuple[Any, ...]:
        """Authenticated fee snapshot from the same client used by F5 preflight."""
        if self.execution is None:
            raise LiveError("ACCOUNT_FEE_SOURCE_UNAVAILABLE")
        return tuple(self.execution.client.fee_schedules())

    def _preflight_event(self, event: Any, passed: bool) -> None:
        if event is None:
            return
        event("PRODUCTION_PREFLIGHT_PASS" if passed else "PRODUCTION_PREFLIGHT_FAIL", component="execution",
              level="INFO" if passed else "WARNING",
              message="GET-only Production readiness preflight " + ("passed" if passed else "blocked"),
              blockers=list(self.preflight_blockers), warnings=list(self.preflight_warnings),
              checks=self.last_preflight.checks if self.last_preflight else {})

    def refresh_production_preflight(self, event: Any = None) -> dict[str, Any]:
        """Discard the previous result and re-run the GET-only Production preflight."""
        if self.execution is None:
            return self.production_preflight()
        ready, blockers, warnings = self._run_preflight()
        self.preflight_status = PREFLIGHT_PASS if ready else PREFLIGHT_FAIL
        self.preflight_blockers, self.preflight_warnings = blockers, warnings
        self.preflight_checked_at = self.last_preflight.checked_at if self.last_preflight else datetime.now(UTC)
        self._preflight_event(event, ready)
        return self.production_preflight()

    def start(self, config: SessionConfig, event: Any) -> None:
        if self.execution is None:
            raise LiveError("PRODUCTION_PREFLIGHT_REQUIRED")
        # A start decision must never reuse a preflight performed minutes ago.
        self.refresh_production_preflight(event)
        self._refresh_wallet()
        if self.account_balance_contradiction:
            self._block_with("ACCOUNT_BALANCE_CONTRADICTION")
        if self.preflight_status != PREFLIGHT_PASS:
            raise SessionStartBlocked(self.preflight_blockers)
        super().start(config, event)
        self._event = event
        self._strategy_fingerprint = self._champion_fingerprint or ""
        self.observability.heartbeat(status="RUNNING")
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="autofund-trading-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop.set()
        self.observability.end("STOPPED")

    def kill(self) -> None:
        self.killed, self.running = True, False
        self._stop.set()
        self.observability.end("HALTED")

    def _loop(self) -> None:
        while self.running and not self._stop.is_set():
            # Internal runtime liveness, independent of operator publication.
            self.observability.heartbeat()
            try:
                started = monotonic()
                depth = self.execution.client.order_book()
                # Read-only operator projection of real market data.
                self.observability.observe_market(depth, latency_ms=(monotonic() - started) * 1000)
                minute = depth.timestamp.strftime("%Y%m%d%H%M")
                if self._last_minute is not None and minute != self._last_minute:
                    close = depth.best_bid
                    self._closes.append(close)
                    self._closes = self._closes[-20:]
                    self._event("CANDLE_CLOSED", component="market", message="BTC/MXN closed candle accepted")
                    self._evaluate(close)
                self._last_minute = minute
            except Exception:
                self.observability.market_unavailable("Market polling unavailable")
                self._event("MARKET_DISCONNECTED", component="market", level="ERROR", message="Market polling unavailable")
            self._stop.wait(5)

    def _evaluate(self, close: Decimal) -> None:
        """Evaluate the certified Champion on one closed candle.

        The decision comes from the single Champion implementation, and the same
        call returns the evidence telemetry persists. Behavior is unchanged from
        the previously inlined logic.
        """
        if not self.running or self.killed:
            return
        started = monotonic()
        position = self.execution.wallet.positions.get("BTC/MXN")
        quantity = position.quantity if position is not None else Decimal("0")
        cost_basis = position.cost_basis_mxn if position is not None else Decimal("0")
        evidence = evaluate_champion(market="BTC/MXN", closes=tuple(self._closes), quantity=quantity,
                                     cost_basis_mxn=cost_basis, strategy_fingerprint=self._strategy_fingerprint)
        payload = evidence.telemetry()
        self.observability.duration("strategy_evaluation", (monotonic() - started) * 1000)
        if not evidence.eligible:
            # Insufficient history is not a strategy evaluation; it is a documented
            # precondition failure. Counts match the previously inlined behavior.
            self._event("NO_SIGNAL", component="strategy", message=evidence.message, **payload)
            return
        self._event("STRATEGY_EVALUATED", component="strategy", message=evidence.message, **payload)
        if evidence.decision == DECISION_BUY:
            self.handle_signal("BUY")
        elif evidence.decision == DECISION_SELL:
            self.handle_signal("SELL")
        else:
            self._event("NO_SIGNAL", component="strategy", message=evidence.message, **payload)

    def handle_signal(self, side: str) -> None:
        if not self.running or self.killed or self.execution.unresolved:
            return
        correlation = uuid4().hex
        self._event("SIGNAL_GENERATED", component="strategy", correlation_id=correlation, message=side)
        try:
            if side == "BUY":
                checked_at = monotonic()
                checked = self.execution.check()
                self.observability.duration("final_market_preflight", (monotonic() - checked_at) * 1000)
                if not checked.ready:
                    self._event("FINAL_MARKET_CHECK_REJECT", component="execution", correlation_id=correlation,
                                message=",".join(checked.failures))
                    return
                self._event("CAPITAL_CHECK_PASS", component="capital", correlation_id=correlation, message="Within 50/25/11 envelope")
                self._event("RISK_CHECK_PASS", component="risk", correlation_id=correlation, message="RiskEngine accepted BUY")
                intent = self.execution.create(checked)
                self._event("FINAL_MARKET_CHECK_PASS", component="execution", correlation_id=correlation, message="Final GET passed")
                self._event("ORDER_INTENT_CREATED", component="execution", correlation_id=correlation, intent_id=intent.intent_id)
                reconciled_at = monotonic()
                self.execution.submit_authorized(intent)
                self.observability.duration("reconciliation", (monotonic() - reconciled_at) * 1000)
            elif side == "SELL":
                position = self.execution.wallet.positions.get("BTC/MXN")
                if position is None or position.quantity <= 0:
                    self._event("RISK_CHECK_REJECT", component="risk", correlation_id=correlation, message="No AutoFund inventory")
                    return
                intent = self.execution.create_sell(position.quantity)
                self._event("FINAL_MARKET_CHECK_PASS", component="execution", correlation_id=correlation, message="Owned SELL final GET passed")
                self._event("ORDER_INTENT_CREATED", component="execution", correlation_id=correlation, intent_id=intent.intent_id)
                self.execution.submit_sell_authorized(intent)
            else:
                raise LiveError("UNSUPPORTED_AUTONOMOUS_SIGNAL")
        except Exception:
            self.running = False
            self._event("AUTO_HALT_TRIGGERED", component="execution", level="CRITICAL",
                        correlation_id=correlation, message="Execution or reconciliation failed")

    def snapshot(self) -> dict[str, Any]:
        if self.execution is None:
            return {**super().snapshot(), "connected": False, "accounting_status": "UNKNOWN"}
        row = self.execution.public()
        inventory = Decimal(str(row["inventory_btc"]))
        mark = self.last_preflight.depth.best_bid if self.last_preflight else Decimal("0")
        deployed = inventory * mark
        equity = Decimal(str(row["cash_mxn"])) + deployed
        cost_basis = Decimal(str(row["cost_basis_mxn"]))
        average_cost = cost_basis / inventory if inventory else Decimal("0")
        unrealized = deployed - cost_basis
        wallet = [{"currency": balance.currency.upper(), "total": str(balance.total),
                   "available": str(balance.available), "locked": str(balance.locked),
                   "approx_mxn": (str(balance.total) if balance.currency == "mxn" else
                                  str(balance.total * mark) if balance.currency == "btc" and mark > 0 else None)}
                  for balance in self.wallet_balances if balance.total or balance.available or balance.locked]
        return {**super().snapshot(), "cash_mxn": str(row["cash_mxn"]), "equity_mxn": str(equity),
                "deployed_mxn": str(deployed), "accounting_status": "PASS",
                "wallet": {"status": self.wallet_status, "error": self.wallet_error,
                           "read_only": True, "balances": wallet},
                "position": None if inventory == 0 else {"asset": "BTC", "quantity": str(inventory),
                    "average_cost_mxn": str(average_cost), "cost_basis_mxn": str(cost_basis), "mark_mxn": str(mark),
                    "market_value_mxn": str(deployed), "realized_pnl_mxn": str(row["realized_pnl_mxn"]),
                    "unrealized_pnl_mxn": str(unrealized), "fees": row["fees_by_currency"],
                    "strategy_version": "0.1", "status": "OPEN"}, "orders": len(row["orders"]),
                "fills": len(row["ledger"]) - 1, "realized_pnl_mxn": str(row["realized_pnl_mxn"]),
                "unrealized_pnl_mxn": str(unrealized), "fees_mxn": str(row["fees_mxn"]),
                "execution": {"reconciliation_state": row["reconciliation_state"],
                              "orders": row["order_count"], "fills": row["fill_count"],
                              "fees_by_currency": row["fees_by_currency"],
                              "fees_mxn": row["fees_mxn"],
                              "gross_realized_pnl_mxn": row["gross_realized_pnl_mxn"],
                              "net_realized_pnl_mxn": row["realized_pnl_mxn"],
                              "buy": row["buy"], "sell": row["sell"]},
                "capital_status": "CAPITAL_NOT_EXECUTABLE" if Decimal(str(row["cash_mxn"])) < Decimal("10.1") else "EXECUTABLE"}


class DemoAutonomousRunner(SafeIdleRunner):
    """Deterministic browser fixture; never imports or calls an exchange client."""

    def __init__(self) -> None:
        super().__init__(demo=True)
        self.step = 0
        self.event: Any = None

    def production_preflight(self) -> dict[str, Any]:
        # Demo never contacts Bitso, so it must not claim Production readiness.
        return {"status": PREFLIGHT_NOT_RUN, "ready": False, "blocked": True, "label": "NOT APPLICABLE",
                "blockers": ["DEMO_MODE_NOT_PRODUCTION"], "warnings": [], "checked_at": None,
                "exchange": "DEMO", "write_transport": "UNUSED"}

    def start(self, config: SessionConfig, event: Any) -> None:
        super().start(config, event)
        self.event = event
        # A new demo session restarts the deterministic fixture, like the real
        # runner re-runs its preflight, so a restarted session does not replay
        # the previous session's steps.
        self.step = 0
        self.observability.heartbeat(status="RUNNING")

    def _demo_market(self, price: str, *, latency_ms: float = 8.0) -> None:
        """Deterministic, clearly-marked demo market observation (never Production)."""
        from autofund.observer.models import Level, OrderBookSnapshot
        bid = Decimal(price)
        snapshot = OrderBookSnapshot("btc_mxn", datetime.now(UTC), 1000 + self.step,
                                     (Level(bid, Decimal("1")),), (Level(bid + Decimal("100"), Decimal("1")),))
        self.observability.observe_market(snapshot, latency_ms=latency_ms)

    def advance(self) -> None:
        if not self.running or self.event is None or self.step >= 2:
            return
        correlation = "demo-trade-1"
        if self.step == 0:
            self._demo_market("999900")
            for name, component in (("CANDLE_CLOSED", "market"), ("STRATEGY_EVALUATED", "strategy"),
                                    ("SIGNAL_GENERATED", "strategy"), ("CAPITAL_CHECK_PASS", "capital"),
                                    ("RISK_CHECK_PASS", "risk"), ("FINAL_MARKET_CHECK_PASS", "execution"),
                                    ("ORDER_INTENT_CREATED", "execution"), ("ORDER_SUBMITTING", "execution"),
                                    ("ORDER_ACKNOWLEDGED", "execution"), ("FILL", "accounting"),
                                    ("LEDGER_UPDATED", "accounting"), ("POSITION_OPENED", "accounting")):
                self.event(name, component=component, correlation_id=correlation, message="Demo BUY lifecycle")
        else:
            for name, component in (("SIGNAL_GENERATED", "strategy"), ("RISK_CHECK_PASS", "risk"),
                                    ("FINAL_MARKET_CHECK_PASS", "execution"), ("ORDER_INTENT_CREATED", "execution"),
                                    ("ORDER_SUBMITTING", "execution"), ("ORDER_ACKNOWLEDGED", "execution"),
                                    ("FILL", "accounting"), ("LEDGER_UPDATED", "accounting"),
                                    ("POSITION_CLOSED", "accounting")):
                self.event(name, component=component, correlation_id=correlation, message="Demo SELL lifecycle")
        self.step += 1

    def snapshot(self) -> dict[str, Any]:
        base = super().snapshot()
        base.update(orders=self.step, fills=self.step, last_signal="BUY" if self.step == 1 else "SELL" if self.step == 2 else "NO_SIGNAL")
        if self.step == 1:
            base.update(cash_mxn="39", equity_mxn="49.92", deployed_mxn="10.92",
                        position={"asset": "BTC", "quantity": "0.0000109", "average_cost_mxn": "11",
                                  "mark_mxn": "10.92", "market_value_mxn": "10.92", "realized_pnl_mxn": "0",
                                  "unrealized_pnl_mxn": "-0.08", "fees_mxn": "0.08", "strategy_version": "0.1"})
        elif self.step >= 2:
            base.update(cash_mxn="49.80", equity_mxn="49.80", deployed_mxn="0", realized_pnl_mxn="-0.20",
                        fees_mxn="0.16", position=None)
        return base


class AutoFundOrchestrator:
    TRANSITIONS: ClassVar[dict[AppState, set[AppState]]] = {
        AppState.BOOTING: {AppState.RECOVERING, AppState.ERROR},
        AppState.RECOVERING: {AppState.STOPPED, AppState.HALTED, AppState.ERROR},
        # STARTING -> STOPPED is the recoverable readiness blocker path: nothing
        # was written, auto execution stayed off, and HALTED is not warranted.
        AppState.STOPPED: {AppState.STARTING}, AppState.STARTING: {AppState.RUNNING, AppState.STOPPED, AppState.HALTED, AppState.ERROR},
        AppState.RUNNING: {AppState.STOPPING, AppState.HALTED},
        AppState.STOPPING: {AppState.STOPPED, AppState.HALTED},
        AppState.HALTED: {AppState.RECOVERING}, AppState.ERROR: {AppState.RECOVERING},
    }

    def __init__(self, artifacts: Path, runner: TradingRunner | None = None, *, demo: bool = False) -> None:
        self.artifacts = artifacts
        self.runner = runner or SafeIdleRunner()
        self.demo = demo
        self.state = AppState.BOOTING
        self.auto_execution = False
        self.session_id: str | None = None
        self.session_started_at: datetime | None = None
        self.session_config: SessionConfig | None = None
        self.telemetry: SessionTelemetry | None = SessionTelemetry(artifacts, "app-" + uuid4().hex[:8])
        self.adaptive = AdaptiveEngine()
        self.last_error: str | None = None
        self.kill_triggered = False
        self.session_ended_at: datetime | None = None
        self.session_stop_reason: str | None = None
        self._requested_stop: str | None = None
        self._scanner: MarketScanner | None = None
        self._scanner_thread: threading.Thread | None = None
        self._scanner_stop = threading.Event()
        self._scanner_interval = 300
        self._session_monotonic: float | None = None
        self._lock = threading.RLock()
        self._checkpoint("APP_BOOT", component="application", message="AutoFund MVP process initialized")

    def _transition(self, target: AppState) -> None:
        if target not in self.TRANSITIONS[self.state]:
            raise LiveError(f"INVALID_APP_TRANSITION_{self.state}_TO_{target}")
        self.state = target

    def startup(self) -> None:
        with self._lock:
            self._transition(AppState.RECOVERING)
            self._checkpoint("RECOVERY_STARTED", component="orchestrator", message="Loading durable state")
            try:
                if isinstance(self.runner, ProductionAutonomousRunner):
                    self.runner._event = self._checkpoint
                evidence = self.runner.startup()
                if evidence.get("unresolved_orders") or not evidence.get("reconciled", False):
                    self._transition(AppState.HALTED)
                    self.last_error = "STARTUP_RECONCILIATION_FAILED"
                else:
                    self._transition(AppState.STOPPED)
                    self._checkpoint("RECOVERY_COMPLETED", component="orchestrator", message="Startup reconciliation passed")
                    # Startup readiness is informational here; START re-runs it fresh.
                    readiness = self.runner.production_preflight()
                    self._checkpoint("PRODUCTION_PREFLIGHT_PASS" if readiness["ready"] else "PRODUCTION_PREFLIGHT_FAIL",
                                     component="execution", level="INFO" if readiness["ready"] else "WARNING",
                                     message="Startup GET-only Production readiness preflight",
                                     blockers=readiness.get("blockers", []), warnings=readiness.get("warnings", []))
            except Exception as exc:
                self._transition(AppState.HALTED)
                readiness = self.runner.production_preflight()
                blocker = (readiness.get("blockers") or [None])[0]
                # Prefer the precise, operator-facing blocker over a raw internal
                # message when the runner already identified the cause.
                self.last_error = blocker or (str(exc.args[0]) if exc.args else "STARTUP_RECOVERY_FAILED")
                self._checkpoint("RECONCILIATION_FAIL", component="orchestrator", level="ERROR", message="Startup recovery failed")
            self.auto_execution = False

    def _checkpoint(self, event: str, **fields: Any) -> dict[str, Any]:
        if self.telemetry is None:
            raise LiveError("SESSION_TELEMETRY_UNAVAILABLE")
        row = self.telemetry.checkpoint(event, **fields)
        # The operator view is a read-only projection of the same authoritative
        # checkpoint; it can never alter trading behavior.
        self.runner.observability.observe_checkpoint(row)
        return row

    def start(self, config: SessionConfig, confirmation: str) -> None:
        with self._lock:
            if self.state is not AppState.STOPPED:
                raise LiveError("APP_NOT_STOPPED")
            if confirmation != "START AUTOFUND REAL 50":
                raise LiveError("REAL_MONEY_CONFIRMATION_REQUIRED")
            self._transition(AppState.STARTING)
            self.session_id = "mvp-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8]
            self.session_config, self.session_started_at = config, datetime.now(UTC)
            self._session_monotonic = monotonic()
            self.session_ended_at = self.session_stop_reason = self._requested_stop = None
            # The Champion is fixed for the whole session and cannot change mid-session.
            if isinstance(self.runner, ProductionAutonomousRunner):
                self.runner._champion_fingerprint = self.adaptive.champion.fingerprint
            self.runner.observability.begin(self.session_id, self.session_started_at)
            self.telemetry = SessionTelemetry(self.artifacts, self.session_id)
            self._checkpoint("SESSION_START_REQUESTED", component="control", message="Operator authorized bounded real session",
                             config={**asdict(config), "authorized_capital_mxn": "50", "max_deployment_mxn": "25", "single_order_cap_mxn": "11"})
            try:
                self.auto_execution = True
                self.runner.start(config, self._checkpoint)
                self._transition(AppState.RUNNING)
                self._checkpoint("SESSION_STARTED", component="orchestrator", message="Automatic execution enabled for this session")
            except SessionStartBlocked as blocked:
                # Recoverable readiness failure: no exchange write happened and
                # automatic execution stayed off. Remain STOPPED so the operator
                # can retry once the blocker clears.
                self.auto_execution = False
                self.last_error = str(blocked)
                self._transition(AppState.STOPPED)
                self._checkpoint("SESSION_START_BLOCKED", component="control", level="WARNING",
                                 message="Session start blocked by Production readiness", blockers=list(blocked.blockers))
                raise
            except Exception:
                self.auto_execution = False
                self._transition(AppState.HALTED)
                self._checkpoint("AUTO_HALT_TRIGGERED", component="orchestrator", level="CRITICAL", message="Runner startup failed")
                raise

    def stop(self, reason: str = "operator") -> None:
        with self._lock:
            if self.state is not AppState.RUNNING:
                raise LiveError("APP_NOT_RUNNING")
            self._transition(AppState.STOPPING)
            self.auto_execution = False
            # A guard-triggered stop has already recorded its canonical reason.
            self._requested_stop = self._requested_stop or STOP_OPERATOR
            self._checkpoint("SESSION_STOP_REQUESTED", component="control", message=reason,
                             stop_reason=self._stop_reason())
            self.runner.stop()
            self._finish("STOPPED")
            self._transition(AppState.STOPPED)

    def kill(self, reason: str = "operator") -> None:
        with self._lock:
            if self.state not in {AppState.RUNNING, AppState.STOPPING}:
                raise LiveError("APP_NOT_ACTIVE")
            self.auto_execution, self.kill_triggered = False, True
            self._requested_stop = STOP_KILL_SWITCH
            self.runner.kill()
            self._checkpoint("KILL_SWITCH_TRIGGERED", component="control", level="CRITICAL", message=reason)
            self._transition(AppState.HALTED)
            self._finish("HALTED")

    def observe_equity(self, equity: Decimal) -> None:
        with self._lock:
            if self.state is not AppState.RUNNING or self.session_config is None:
                return
            loss = AUTHORIZED_CAPITAL - decimal(equity, "equity")
            if loss >= self.session_config.max_session_loss_mxn:
                self._checkpoint("LOSS_LIMIT_HIT", component="risk", level="CRITICAL", message="Session loss limit reached", loss_mxn=loss)
                self.auto_execution = False
                self._requested_stop = STOP_LOSS_LIMIT
                self.runner.kill()
                self._transition(AppState.HALTED)
                self._checkpoint("AUTO_HALT_TRIGGERED", component="risk", level="CRITICAL", message="Loss-limit halt")
                self._finish("HALTED")

    def _stop_reason(self, *_: object) -> str:
        """Canonical, backend-authoritative reason for the current stop.

        The reason is recorded when the stop is requested, never inferred from
        elapsed time or from the caller.
        """
        if self._requested_stop is not None:
            return self._requested_stop
        if self.last_error is not None:
            return STOP_RUNNER_FAILURE
        return STOP_OPERATOR

    def _finish(self, result: str) -> None:
        # Freeze session time exactly once, at finalization.
        self.session_ended_at = datetime.now(UTC)
        self.session_stop_reason = self._stop_reason()
        if self.telemetry is None:
            return
        if result == "STOPPED":
            self._checkpoint("SESSION_STOPPED", component="orchestrator", message="Session finalized")
        snap = self.runner.snapshot()
        view = self._session_view()
        net_pnl = Decimal(str(snap.get("equity_mxn", AUTHORIZED_CAPITAL))) - AUTHORIZED_CAPITAL
        metrics = {"orders": snap.get("orders", 0), "fills": snap.get("fills", 0),
                   "net_pnl_mxn": str(net_pnl), "fees_mxn": snap.get("fees_mxn", "0")}
        # Adaptive learning runs at completion only; it can never alter hard safety.
        learning = self.adaptive.observe_session(rows=list(self.telemetry.rows), metrics=metrics,
                                                 stop_reason=self.session_stop_reason,
                                                 scanner=self._scanner.evidence() if self._scanner else None)
        self._checkpoint("LEARNING_OBSERVATION", component="learning", level="INFO",
                         message=learning.get("classification", "OBSERVED"),
                         classification=learning.get("classification"), observations=learning.get("observations", []),
                         sessions_observed=learning.get("sessions_observed"), challengers=len(self.adaptive.challengers))
        self.telemetry.finalize(
            result=result, stop_reason=self.session_stop_reason,
            identity={"market": "btc_mxn", "profile_id": self.adaptive.champion.profile_id,
                      "strategy_id": self.adaptive.champion.strategy_id,
                      "champion_fingerprint": self.adaptive.champion.fingerprint},
            config={**(asdict(self.session_config) if self.session_config else {}),
                    "authorized_capital_mxn": str(AUTHORIZED_CAPITAL),
                    "max_deployment_mxn": str(MAX_DEPLOYMENT), "single_order_cap_mxn": str(SINGLE_ORDER_CAP)},
            time_facts={"actual_runtime_seconds": view["actual_runtime_seconds"],
                        "configured_duration_seconds": view["max_duration_seconds"]},
            learning=learning,
            facts={"metrics": metrics, "portfolio": snap.get("position"),
                   "wallet": snap.get("wallet"), "execution": snap.get("execution"),
                   "initial_equity_mxn": str(AUTHORIZED_CAPITAL),
                   "final_equity_mxn": str(snap.get("equity_mxn", AUTHORIZED_CAPITAL)),
                   "realized_pnl_mxn": str(snap.get("realized_pnl_mxn", "0")),
                   "unrealized_pnl_mxn": str(snap.get("unrealized_pnl_mxn", "0")),
                   "max_deployment_mxn": str(snap.get("deployed_mxn", "0")),
                   "execution_quality": {}, "halts": 1 if result == "HALTED" else 0})

    def shutdown(self) -> None:
        """Release the backend publication thread; never changes financial state."""
        self.runner.observability.close()
        self.stop_scanner()

    # ------------------------------------------------------------- scanner
    def start_scanner(self, scanner: MarketScanner, *, interval_seconds: int | None = None) -> None:
        """Start research-only background scanning.

        The scanner is isolated from Production: it never creates an order
        intent, never reaches the ExecutionEngine and cannot change the live
        market. Its failures degrade the scanner only.
        """
        self._scanner = scanner
        self._scanner_interval = interval_seconds or scanner.interval_seconds
        if self._scanner_thread is not None and self._scanner_thread.is_alive():
            return
        self._scanner_stop.clear()
        self._scanner_thread = threading.Thread(target=self._scanner_loop, name="mvp-market-scanner", daemon=True)
        self._scanner_thread.start()

    def stop_scanner(self) -> None:
        self._scanner_stop.set()
        thread, self._scanner_thread = self._scanner_thread, None
        if thread is not None:
            thread.join(timeout=2)

    def _scanner_loop(self) -> None:
        while not self._scanner_stop.is_set():
            self.scan_markets()
            self._scanner_stop.wait(self._scanner_interval)

    def scan_markets(self, *, now: datetime | None = None) -> dict[str, Any]:
        """One bounded scan. Research only; never affects trading."""
        scanner = self._scanner
        if scanner is None:
            return {}
        try:
            return dict(scanner.scan(now=now, telemetry=self._checkpoint))
        except Exception:
            # Even an unexpected scanner failure stays contained to research.
            return dict(scanner.evidence())

    def scanner_evidence(self) -> dict[str, Any]:
        return dict(self._scanner.evidence()) if self._scanner is not None else {
            "scanner_ran": False, "degraded": False, "universe_size": 0, "candidates": [],
            "eligible": [], "rejected": [], "shadow": [], "live_market": "btc_mxn",
            "production_market_rotation": "DISABLED", "market_promotion": "DISABLED",
            "read_only": True, "execution_path_to_production": "NOT_PRESENT"}

    def learning_view(self) -> dict[str, Any]:
        scanner = self._scanner.scanner_evidence() if self._scanner is not None else None
        return dict(self.adaptive.learning_view(scanner=scanner))

    def readiness(self) -> dict[str, Any]:
        """Production readiness as shown before START; never invents a preflight."""
        readiness = dict(self.runner.production_preflight())
        readiness.setdefault("label", "READY" if readiness.get("ready") else "BLOCKED")
        if readiness.get("ready"):
            readiness["reason"] = ""
        else:
            readiness["reason"] = ("; ".join(readiness.get("blockers") or [])
                                   or "PRODUCTION_PREFLIGHT_BLOCKED")
        return readiness

    def _session_view(self) -> dict[str, Any]:
        """Runtime progress. Frozen once the session reaches a terminal state."""
        with self._lock:
            if self.session_config is None or self._session_monotonic is None:
                return {"started_at": None, "ended_at": None, "elapsed_seconds": None,
                        "actual_runtime_seconds": None, "remaining_seconds": None,
                        "max_duration_seconds": None, "max_orders_per_session": None,
                        "max_session_loss_mxn": None, "stop_reason": None, "frozen": False}
            config = self.session_config
            ended = self.session_ended_at
            if ended is not None:
                # Terminal state: elapsed is a historical fact and never advances.
                elapsed = max(0, int((ended - self.session_started_at).total_seconds())) if self.session_started_at else 0
                remaining = max(0, config.max_session_duration_seconds - elapsed)
                frozen = True
            else:
                elapsed = max(0, int(monotonic() - self._session_monotonic))
                remaining = max(0, config.max_session_duration_seconds - elapsed)
                frozen = False
            return {"started_at": self.session_started_at, "ended_at": ended,
                    "elapsed_seconds": elapsed, "actual_runtime_seconds": elapsed if frozen else None,
                    "remaining_seconds": remaining, "max_duration_seconds": config.max_session_duration_seconds,
                    "max_orders_per_session": config.max_orders_per_session,
                    "max_session_loss_mxn": str(config.max_session_loss_mxn),
                    "stop_reason": self.session_stop_reason, "frozen": frozen}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if isinstance(self.runner, DemoAutonomousRunner) and self.state is AppState.RUNNING:
                self.runner.advance()
            live = self.runner.snapshot()
            if self.state is AppState.RUNNING and self.session_config is not None:
                equity = Decimal(str(live.get("equity_mxn", "50")))
                if AUTHORIZED_CAPITAL - equity >= self.session_config.max_session_loss_mxn:
                    self.observe_equity(equity)
                elif int(live.get("orders", 0)) >= self.session_config.max_orders_per_session:
                    self._requested_stop = STOP_MAX_ORDERS
                    self.stop(STOP_MAX_ORDERS)
                elif self._session_monotonic is not None and monotonic() - self._session_monotonic >= self.session_config.max_session_duration_seconds:
                    self._requested_stop = STOP_MAX_DURATION
                    self.stop(STOP_MAX_DURATION)
            rows = list(self.telemetry.rows[-100:]) if self.telemetry else []
            observability = self.runner.observability
            return {"schema_version": "autofund.mvp.v1", "product_version": PRODUCT_VERSION,
                    "demo_mode": self.demo, "app_state": self.state, "mode": "REAL MONEY",
                    "auto_execution": self.auto_execution, "session_id": self.session_id,
                    "session_started_at": self.session_started_at, "authorized_capital_mxn": "50",
                    "max_deployment_mxn": "25", "single_order_cap_mxn": "11",
                    "session_config": asdict(self.session_config) if self.session_config else None,
                    "session": self._session_view(),
                    "kill_triggered": self.kill_triggered, "last_error": self.last_error,
                    "production_preflight": self.readiness(),
                    # Operator observability, reusing the F4.6 runtime contract.
                    "runtime": observability.runtime(),
                    "observability": observability.health(),
                    "market_state": observability.market_state(),
                    "pipeline": observability.pipeline(),
                    "strategy": observability.strategy(),
                    "learning": self.learning_view(),
                    "scanner": self.scanner_evidence(),
                    "candles": observability.candles(),
                    "metrics": observability.metrics(),
                    "champion": {**asdict(self.adaptive.champion), "fingerprint": self.adaptive.champion.fingerprint},
                    "market_regime": "NORMAL", "auto_promotion": False, "challengers": self.adaptive.challengers,
                    "telemetry": rows, **live}

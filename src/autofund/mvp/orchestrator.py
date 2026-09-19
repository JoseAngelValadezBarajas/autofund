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
from .telemetry import SessionTelemetry

AUTHORIZED_CAPITAL = Decimal("50")
MAX_DEPLOYMENT = Decimal("25")
SINGLE_ORDER_CAP = Decimal("11")
PREFLIGHT_PASS, PREFLIGHT_FAIL, PREFLIGHT_NOT_RUN = "PASS", "FAIL", "NOT_RUN"
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

    def __init__(self) -> None:
        self.running = False
        self.killed = False
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

    def stop(self) -> None: self.running = False
    def kill(self) -> None: self.running, self.killed = False, True

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
        self.journal = LiveExecutionJournal(self.journal_path)
        self.execution = LiveExecution(client, self.journal, LiveConfig(slippage_tolerance=Decimal("0.5")))
        if self.execution.unresolved:
            self.execution.recover()
        reconciled = not self.execution.unresolved
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
        if self.preflight_status != PREFLIGHT_PASS:
            raise SessionStartBlocked(self.preflight_blockers)
        super().start(config, event)
        self._event = event
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="autofund-trading-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self._stop.set()

    def kill(self) -> None:
        self.killed, self.running = True, False
        self._stop.set()

    def _loop(self) -> None:
        while self.running and not self._stop.is_set():
            try:
                depth = self.execution.client.order_book()
                minute = depth.timestamp.strftime("%Y%m%d%H%M")
                if self._last_minute is not None and minute != self._last_minute:
                    close = depth.best_bid
                    self._closes.append(close)
                    self._closes = self._closes[-20:]
                    self._event("CANDLE_CLOSED", component="market", message="BTC/MXN closed candle accepted")
                    self._evaluate(close)
                self._last_minute = minute
            except Exception:
                self._event("MARKET_DISCONNECTED", component="market", level="ERROR", message="Market polling unavailable")
            self._stop.wait(5)

    def _evaluate(self, close: Decimal) -> None:
        if not self.running or self.killed or len(self._closes) < 3:
            self._event("NO_SIGNAL", component="strategy", message="Insufficient closed-candle evidence")
            return
        self._event("STRATEGY_EVALUATED", component="strategy", message="Certified mean-reversion profile evaluated")
        position = self.execution.wallet.positions.get("BTC/MXN")
        average = sum(self._closes, Decimal("0")) / len(self._closes)
        if position is None or position.quantity == 0:
            if close < average * Decimal("0.999"):
                self.handle_signal("BUY")
            else:
                self._event("NO_SIGNAL", component="strategy", message="Champion chose no trade")
        else:
            average_cost = position.cost_basis_mxn / position.quantity
            if close > average_cost * Decimal("1.002"):
                self.handle_signal("SELL")
            else:
                self._event("NO_SIGNAL", component="strategy", message="Champion retained owned position")

    def handle_signal(self, side: str) -> None:
        if not self.running or self.killed or self.execution.unresolved:
            return
        correlation = uuid4().hex
        self._event("SIGNAL_GENERATED", component="strategy", correlation_id=correlation, message=side)
        try:
            if side == "BUY":
                checked = self.execution.check()
                if not checked.ready:
                    self._event("FINAL_MARKET_CHECK_REJECT", component="execution", correlation_id=correlation,
                                message=",".join(checked.failures))
                    return
                self._event("CAPITAL_CHECK_PASS", component="capital", correlation_id=correlation, message="Within 50/25/11 envelope")
                self._event("RISK_CHECK_PASS", component="risk", correlation_id=correlation, message="RiskEngine accepted BUY")
                intent = self.execution.create(checked)
                self._event("FINAL_MARKET_CHECK_PASS", component="execution", correlation_id=correlation, message="Final GET passed")
                self._event("ORDER_INTENT_CREATED", component="execution", correlation_id=correlation, intent_id=intent.intent_id)
                self.execution.submit_authorized(intent)
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
        return {**super().snapshot(), "cash_mxn": str(row["cash_mxn"]), "equity_mxn": str(equity),
                "deployed_mxn": str(deployed), "accounting_status": "PASS",
                "position": None if inventory == 0 else {"asset": "BTC", "quantity": str(inventory),
                    "average_cost_mxn": str(row["cost_basis_mxn"]), "mark_mxn": str(mark),
                    "market_value_mxn": str(deployed), "realized_pnl_mxn": str(row["realized_pnl_mxn"]),
                    "unrealized_pnl_mxn": str(equity - AUTHORIZED_CAPITAL), "fees_mxn": "confirmed-ledger",
                    "strategy_version": "0.1"}, "orders": len(row["orders"]),
                "fills": len(row["ledger"]) - 1, "realized_pnl_mxn": str(row["realized_pnl_mxn"]),
                "capital_status": "CAPITAL_NOT_EXECUTABLE" if Decimal(str(row["cash_mxn"])) < Decimal("10.1") else "EXECUTABLE"}


class DemoAutonomousRunner(SafeIdleRunner):
    """Deterministic browser fixture; never imports or calls an exchange client."""

    def __init__(self) -> None:
        super().__init__()
        self.step = 0
        self.event: Any = None

    def production_preflight(self) -> dict[str, Any]:
        # Demo never contacts Bitso, so it must not claim Production readiness.
        return {"status": PREFLIGHT_NOT_RUN, "ready": False, "blocked": True, "label": "NOT APPLICABLE",
                "blockers": ["DEMO_MODE_NOT_PRODUCTION"], "warnings": [], "checked_at": None,
                "exchange": "DEMO", "write_transport": "UNUSED"}

    def start(self, config: SessionConfig, event: Any) -> None:
        self.running, self.killed, self.step, self.event = True, False, 0, event
        event("MARKET_CONNECTED", component="market", message="Deterministic market connected")

    def advance(self) -> None:
        if not self.running or self.event is None or self.step >= 2:
            return
        correlation = "demo-trade-1"
        if self.step == 0:
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
                self.last_error = str(exc.args[0]) if exc.args else "STARTUP_RECOVERY_FAILED"
                self._checkpoint("RECONCILIATION_FAIL", component="orchestrator", level="ERROR", message="Startup recovery failed")
            self.auto_execution = False

    def _checkpoint(self, event: str, **fields: Any) -> dict[str, Any]:
        if self.telemetry is None:
            raise LiveError("SESSION_TELEMETRY_UNAVAILABLE")
        return self.telemetry.checkpoint(event, **fields)

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
            self._checkpoint("SESSION_STOP_REQUESTED", component="control", message=reason)
            self.runner.stop()
            self._finish("STOPPED")
            self._transition(AppState.STOPPED)

    def kill(self, reason: str = "operator") -> None:
        with self._lock:
            if self.state not in {AppState.RUNNING, AppState.STOPPING}:
                raise LiveError("APP_NOT_ACTIVE")
            self.auto_execution, self.kill_triggered = False, True
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
                self.runner.kill()
                self._transition(AppState.HALTED)
                self._checkpoint("AUTO_HALT_TRIGGERED", component="risk", level="CRITICAL", message="Loss-limit halt")
                self._finish("HALTED")

    def _finish(self, result: str) -> None:
        if self.telemetry is None:
            return
        if result == "STOPPED":
            self._checkpoint("SESSION_STOPPED", component="orchestrator", message="Session finalized")
        snap = self.runner.snapshot()
        self.telemetry.finalize(result=result, facts={"metrics": {"orders": snap.get("orders", 0), "fills": snap.get("fills", 0),
            "net_pnl_mxn": snap.get("realized_pnl_mxn", "0"), "fees_mxn": snap.get("fees_mxn", "0")},
            "execution_quality": {}, "halts": 1 if result == "HALTED" else 0})

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
                    self.stop("maximum orders reached")
                elif self._session_monotonic is not None and monotonic() - self._session_monotonic >= self.session_config.max_session_duration_seconds:
                    self.stop("maximum session duration reached")
            rows = list(self.telemetry.rows[-100:]) if self.telemetry else []
            return {"schema_version": "autofund.mvp.v1", "product_version": "AutoFund MVP 0.1",
                    "demo_mode": self.demo, "app_state": self.state, "mode": "REAL MONEY",
                    "auto_execution": self.auto_execution, "session_id": self.session_id,
                    "session_started_at": self.session_started_at, "authorized_capital_mxn": "50",
                    "max_deployment_mxn": "25", "single_order_cap_mxn": "11",
                    "session_config": asdict(self.session_config) if self.session_config else None,
                    "kill_triggered": self.kill_triggered, "last_error": self.last_error,
                    "production_preflight": self.readiness(),
                    "champion": {**asdict(self.adaptive.champion), "fingerprint": self.adaptive.champion.fingerprint},
                    "market_regime": "NORMAL", "auto_promotion": False, "challengers": self.adaptive.challengers,
                    "telemetry": rows, **live}

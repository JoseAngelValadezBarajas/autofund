"""Authoritative MVP application state machine and bounded session controls.

The runners live in ``runners.py`` and the shared constants and types in ``session_types.py``;
both are re-exported here so existing imports keep working."""

import threading
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any, ClassVar
from uuid import uuid4

from autofund.decimal_utils import decimal
from autofund.live.models import LiveError
from autofund.version import PRODUCT_VERSION

from .adaptive import AdaptiveEngine
from .runners import DemoAutonomousRunner, ProductionAutonomousRunner, SafeIdleRunner
from .scanner import MarketScanner
from .session_types import (
    ACCOUNT_CONFIRMED_TAKER_RATE,
    AUTHORIZED_CAPITAL,
    BLOCKER_GUIDANCE,
    DEFAULT_SLIPPAGE_BPS,
    MAX_DEPLOYMENT,
    PREFLIGHT_FAIL,
    PREFLIGHT_NOT_RUN,
    PREFLIGHT_PASS,
    SINGLE_ORDER_CAP,
    STOP_BLOCKED_RECOVERY,
    STOP_KILL_SWITCH,
    STOP_LOSS_LIMIT,
    STOP_MAX_DURATION,
    STOP_MAX_ORDERS,
    STOP_OPERATOR,
    STOP_RUNNER_FAILURE,
    WATCHDOG_GAP_THRESHOLD_SECONDS,
    WATCHDOG_INTERVAL_SECONDS,
    AppState,
    SessionConfig,
    SessionStartBlocked,
    TradingRunner,
)
from .telemetry import SessionTelemetry

__all__ = [
    "ACCOUNT_CONFIRMED_TAKER_RATE",
    "AUTHORIZED_CAPITAL",
    "BLOCKER_GUIDANCE",
    "DEFAULT_SLIPPAGE_BPS",
    "MAX_DEPLOYMENT",
    "PREFLIGHT_FAIL",
    "PREFLIGHT_NOT_RUN",
    "PREFLIGHT_PASS",
    "SINGLE_ORDER_CAP",
    "STOP_BLOCKED_RECOVERY",
    "STOP_KILL_SWITCH",
    "STOP_LOSS_LIMIT",
    "STOP_MAX_DURATION",
    "STOP_MAX_ORDERS",
    "STOP_OPERATOR",
    "STOP_RUNNER_FAILURE",
    "WATCHDOG_GAP_THRESHOLD_SECONDS",
    "WATCHDOG_INTERVAL_SECONDS",
    "AppState",
    "AutoFundOrchestrator",
    "DemoAutonomousRunner",
    "ProductionAutonomousRunner",
    "SafeIdleRunner",
    "SessionConfig",
    "SessionStartBlocked",
    "TradingRunner",
]


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
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._watchdog_tick: float | None = None
        self._last_watchdog_at: str | None = None
        self._runtime_gaps: list[dict[str, Any]] = []
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
                # An acknowledged order whose outcome is not established must fail
                # closed: halt new financial writes while market observation and the
                # research scanner continue.
                self.runner._on_blocked = self._on_order_blocked
            self.runner.observability.begin(self.session_id, self.session_started_at)
            self.start_watchdog()
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
        # Refresh the read-only wallet view so the exported snapshot is current,
        # never a startup read presented as live.
        self.refresh_wallet()
        snap = self.runner.snapshot()
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
                    "max_deployment_mxn": str(MAX_DEPLOYMENT), "single_order_cap_mxn": str(SINGLE_ORDER_CAP),
                    # Economic admission is part of the session configuration
                    # fingerprint: two runs with different economic policies made
                    # different financial decisions and must not compare as equal.
                    "economic_policy": self.runner.economic_policy.public()},
            time_facts={"actual_runtime_seconds": view["actual_runtime_seconds"],
                        "configured_duration_seconds": view["max_duration_seconds"]},
            learning=learning,
            facts={"metrics": metrics, "portfolio": snap.get("position"),
                   "wallet": snap.get("wallet"), "execution": snap.get("execution"),
                   "execution_financial_truth": (snap.get("execution") or {}).get("financial_truth"),
                   "initial_equity_mxn": str(AUTHORIZED_CAPITAL),
                   "final_equity_mxn": str(snap.get("equity_mxn", AUTHORIZED_CAPITAL)),
                   "realized_pnl_mxn": str(snap.get("realized_pnl_mxn", "0")),
                   "unrealized_pnl_mxn": str(snap.get("unrealized_pnl_mxn", "0")),
                   "max_deployment_mxn": str(snap.get("deployed_mxn", "0")),
                   "execution_quality": {}, "halts": 1 if result == "HALTED" else 0})

    def refresh_wallet(self) -> None:
        """Refresh the read-only wallet view so an export is never a stale snapshot."""
        refresh = getattr(self.runner, "refresh_wallet", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                pass  # A wallet read failure never affects financial state.

    def _on_order_blocked(self, origins: tuple[str, ...]) -> None:
        """Fail closed when an order outcome is unresolved.

        The UI must not keep presenting a normally RUNNING session while a REAL
        order has an unknown financial outcome. Automatic execution is disabled
        and the application enters HALTED; market observation and the read-only
        scanner are deliberately unaffected, and no new Production order is
        possible in this state.
        """
        with self._lock:
            if self.state is not AppState.RUNNING:
                return
            self.auto_execution = False
            self.last_error = "BLOCKED_RECOVERY_UNRESOLVED_ORDER"
            self._requested_stop = STOP_BLOCKED_RECOVERY
            self.runner.stop()
            self._checkpoint("AUTO_HALT_TRIGGERED", component="execution", level="CRITICAL",
                             message="Acknowledged order outcome unresolved; new financial writes blocked",
                             origins=list(origins), reconciliation_state="BLOCKED")
            self._transition(AppState.HALTED)
            self._finish("HALTED")

    def _watchdog_loop(self) -> None:
        """Bounded liveness watchdog, independent of HTTP/SSE polling.

        The duration guard previously ran only when a client polled a snapshot, so
        a suspended or starved process could pass its configured duration without
        the guard ever being evaluated. This thread detects the gap, reports it and
        enforces the guard the moment execution resumes. It never trades and never
        submits anything.
        """
        while not self._watchdog_stop.wait(WATCHDOG_INTERVAL_SECONDS):
            self._watchdog_loop_once()

    def _watchdog_loop_once(self) -> None:
        """One watchdog tick. Exposed so liveness handling is directly testable."""
        now = monotonic()
        last = self._watchdog_tick
        self._watchdog_tick = now
        if last is None:
            return
        gap = now - last
        if gap > WATCHDOG_GAP_THRESHOLD_SECONDS:
            next_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            self._runtime_gaps.append({"last_event_at": self._last_watchdog_at, "next_event_at": next_at,
                                       "gap_seconds": str(gap)})
            self._checkpoint("RUNTIME_GAP", component="orchestrator", level="WARNING",
                             message="No executing process observed; wall time is not monitoring",
                             gap_seconds=str(gap), last_event_at=self._last_watchdog_at,
                             next_event_at=next_at)
        self._last_watchdog_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self._enforce_duration_guard()

    def _enforce_duration_guard(self) -> None:
        with self._lock:
            if self.state is not AppState.RUNNING or self.session_config is None or self._session_monotonic is None:
                return
            if monotonic() - self._session_monotonic >= self.session_config.max_session_duration_seconds:
                self._requested_stop = STOP_MAX_DURATION
                self.stop(STOP_MAX_DURATION)

    def _monitored_seconds(self) -> int:
        """Wall elapsed minus detected gaps: time the process actually ran."""
        view = self._session_view()
        elapsed = view.get("elapsed_seconds") or 0
        gaps = sum((Decimal(str(gap["gap_seconds"])) for gap in self._runtime_gaps), Decimal("0"))
        return max(0, int(Decimal(elapsed) - gaps))

    def shutdown(self) -> None:
        """Release the backend publication thread; never changes financial state."""
        self._stop_watchdog()
        self.runner.observability.close()
        self.stop_scanner()

    def start_watchdog(self) -> None:
        self._watchdog_tick = monotonic()
        self._last_watchdog_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop.clear()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, name="mvp-runtime-watchdog",
                                                 daemon=True)
        self._watchdog_thread.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        thread, self._watchdog_thread = self._watchdog_thread, None
        if thread is not None:
            thread.join(timeout=2)

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

    def production_markets_view(self) -> dict[str, Any]:
        """Production Markets contract: per-market evidence, certification and why.

        Read-only and off the trading path. It reports the certification state, the
        eligibility verdict and - crucially - a deterministic reason code for every
        market that is not trading, so an operator is never shown an ambiguous "not
        selected". It cannot activate a market, and multi-market Production stays
        DISABLED until an operator authorises a session.

        Multi-market Production remains disabled by policy in this milestone, so the
        universe is reported as built and the selector as evaluated, but no intent is
        ever created from this view.
        """
        from .certification import CERTIFICATION_VERSION, SELECTABLE_STATES
        from .evidence import STORE_VERSION
        from .profiles import classify_market
        from .universe import SELECTOR_VERSION, UNIVERSE_VERSION

        scanner = self._scanner
        candidates = scanner.candidates if scanner is not None else ()
        shadow = scanner.shadow if scanner is not None else {}
        runner = self.runner
        taker_fee = getattr(runner, "_taker_fee_rate", Decimal("0"))

        markets: list[dict[str, Any]] = []
        for candidate in candidates:
            market = candidate.book.replace("_", "/").upper()
            row = shadow.get(candidate.book) or {}
            tier = row.get("certification_state") or "RESEARCH_ONLY"
            markets.append({
                "book": candidate.book,
                "market": market,
                "market_class": classify_market(candidate.book),
                "research_status": candidate.status,
                "strategy_compatibility": candidate.strategy_compatibility,
                "evidence_provenance": row.get("data_source", "NONE"),
                "closed_candles": row.get("candles", 0),
                "evaluations": row.get("evaluations", 0),
                "signals": row.get("signals", 0),
                "shadow_round_trips": row.get("shadow_trades", 0),
                "shadow_net_pnl_mxn": row.get("net_pnl_mxn", "0"),
                "shadow_fees_mxn": row.get("fees_mxn", "0"),
                "shadow_drawdown_mxn": row.get("max_drawdown_mxn", "0"),
                "economic_reject_rate": row.get("economic_reject_rate", "0"),
                "certification_state": tier,
                "certified": tier in SELECTABLE_STATES,
                "current_strategy_decision": row.get("decision", "NO_SIGNAL"),
                "expected_net_edge_bps": row.get("expected_net_edge_bps"),
                # Evidence realism (MVP 0.2.1). A round-trip count is only meaningful
                # alongside the fill model and window provenance that produced it, so
                # the panel can never show a number whose assumptions are hidden.
                "fill_model": row.get("fill_model", "UNKNOWN"),
                "evidence_quality": row.get("evidence_quality", "UNKNOWN"),
                "experiment_fingerprint": row.get("experiment_fingerprint", ""),
                "window_provenance": row.get("window_provenance", "NONE"),
                "certification_reason": (row.get("certification_reason")
                                         or "NOT_CERTIFIED"),
                "executable_fill_model": bool(row.get("executable_fill_model", False)),
                "production_eligible": (tier in SELECTABLE_STATES
                                        and candidate.status == "ELIGIBLE"),
                "position_status": ("POSITION_OPEN"
                                    if self._market_has_position(market) else "NO_POSITION"),
                "reason_code": self._market_reason_code(
                    market=market, tier=tier, row=row, status=candidate.status),
                "spread_bps": candidate.spread_bps,
                "depth_mxn": candidate.depth_mxn,
                "taker_fee": candidate.taker_fee,
            })

        return {
            "research_version": SELECTOR_VERSION,
            "certification_version": CERTIFICATION_VERSION,
            "universe_version": UNIVERSE_VERSION,
            "evidence_store_version": STORE_VERSION,
            "promotion": "DISABLED",
            "multi_market_production": "DISABLED",
            "production_market": "btc_mxn",
            "account_fee_confirmed": taker_fee > Decimal("0"),
            "taker_fee_rate": str(taker_fee),
            "markets": markets,
            "certified_markets": [row["market"] for row in markets if row["certified"]],
            "selectable_markets": [row["market"] for row in markets if row["production_eligible"]],
            "selection_outcome": ("NO_TRADE" if not any(row["production_eligible"]
                                                        for row in markets) else "CANDIDATE"),
            "one_unresolved_order_globally": True,
            "llm_influences_selection": False,
        }

    def _market_has_position(self, market: str) -> bool:
        """Whether AutoFund owns inventory in a market. Never reads the exchange wallet."""
        execution = getattr(self.runner, "execution", None)
        if execution is None:
            return False
        try:
            return market in execution.wallet.positions
        except Exception:
            return False

    def _market_reason_code(self, *, market: str, tier: str, row: dict[str, Any],
                            status: str) -> str:
        """One deterministic reason a market is not trading, never an ambiguous blank.

        Ordered so the *first* unmet requirement is reported, which is what an operator
        needs to act on. The vocabulary matches the selector's, so the UI and the
        selector can never disagree about why nothing was chosen.
        """
        from .universe import (
            REASON_ALREADY_HOLDING,
            REASON_ECONOMIC_GUARD_REJECT,
            REASON_INSUFFICIENT_EVIDENCE,
            REASON_NO_MARKET_DATA,
            REASON_NO_SIGNAL,
            REASON_NOT_CERTIFIED,
            REASON_NOT_VIABLE,
            REASON_STALE_DATA,
        )

        if status != "ELIGIBLE":
            return REASON_NO_MARKET_DATA
        if self._market_has_position(market):
            return REASON_ALREADY_HOLDING
        if tier == "NOT_VIABLE":
            return REASON_NOT_VIABLE
        if tier in ("INSUFFICIENT_EVIDENCE", "ACCUMULATING_EVIDENCE", "RESEARCH_ONLY"):
            return (REASON_NOT_CERTIFIED if tier == "RESEARCH_ONLY"
                    else REASON_INSUFFICIENT_EVIDENCE)
        if tier == "SUSPENDED":
            return REASON_STALE_DATA
        if row.get("decision") != "BUY":
            return REASON_NO_SIGNAL
        net_edge = row.get("expected_net_edge_bps")
        if net_edge is None or Decimal(str(net_edge)) <= Decimal("0"):
            return REASON_ECONOMIC_GUARD_REJECT
        return "CURRENT_OPPORTUNITY"

    def learning_view(self) -> dict[str, Any]:
        scanner = self._scanner.scanner_evidence() if self._scanner is not None else None
        return dict(self.adaptive.learning_view(scanner=scanner))

    def strategy_research_view(self) -> dict[str, Any]:
        """Strategy Research contract for the Learning/Scanner UI.

        Read-only and off the trading path. The sections are built in ``research_view``.
        """
        from .research_view import build_strategy_research_view

        return build_strategy_research_view(runner=self.runner, adaptive=self.adaptive,
                                            scanner=self._scanner)

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
            snapshot_signals = live.get("signals") or {"admitted": 0, "suppressed_pending_order": 0,
                                                       "strategy_buy_decisions": 0, "strategy_sell_decisions": 0}
            snapshot_blocked = live.get("blocked_recovery") or {"blocked": False, "origins": []}
            return {"schema_version": "autofund.mvp.v1", "product_version": PRODUCT_VERSION,
                    "demo_mode": self.demo, "app_state": self.state, "mode": "REAL MONEY",
                    "auto_execution": self.auto_execution, "session_id": self.session_id,
                    "session_started_at": self.session_started_at, "authorized_capital_mxn": "50",
                    "max_deployment_mxn": "25", "single_order_cap_mxn": "11",
                    "session_config": asdict(self.session_config) if self.session_config else None,
                    "session": self._session_view(),
                    "monitored_seconds": self._monitored_seconds(),
                    "runtime_gaps": list(self._runtime_gaps),
                    "signals": snapshot_signals,
                    "blocked_recovery": snapshot_blocked,
                    "wallet": live.get("wallet"),
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
                    "strategy_research": self.strategy_research_view(),
                    "production_markets": self.production_markets_view(),
                    "candles": observability.candles(),
                    "metrics": observability.metrics(),
                    "champion": {**asdict(self.adaptive.champion), "fingerprint": self.adaptive.champion.fingerprint},
                    "market_regime": "NORMAL", "auto_promotion": False, "challengers": self.adaptive.challengers,
                    "telemetry": rows, **live}

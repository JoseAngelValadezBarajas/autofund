"""Trading runners: the idle, demo and Production-autonomous implementations of ``TradingRunner``.

The Production runner is the only code here that can place an order, and it does so through the
permit-gated execution engine. Moved out of ``orchestrator.py`` unchanged.
"""

import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from autofund.live.models import LiveError

from .champion import DECISION_BUY, DECISION_SELL, evaluate_champion, exit_boundary
from .economics import DEFAULT_POLICY as DEFAULT_ECONOMIC_POLICY
from .economics import (
    INSUFFICIENT_ECONOMIC_EVIDENCE,
    PROFIT_TAKING,
    EconomicPolicy,
    economic_entry_model,
    economic_exit_model,
)
from .observability import MvpObservability
from .session_types import (
    BLOCKER_GUIDANCE,
    DEFAULT_SLIPPAGE_BPS,
    PREFLIGHT_FAIL,
    PREFLIGHT_NOT_RUN,
    PREFLIGHT_PASS,
    SINGLE_ORDER_CAP,
    SessionConfig,
    SessionStartBlocked,
)


def journal_blocker_for(exc: BaseException) -> str:
    """Name the real reason the live journal could not be opened."""
    text = str(exc)
    if text == "LIVE_JOURNAL_NEEDS_MIGRATION":
        return "LIVE_JOURNAL_NEEDS_MIGRATION"  # an old journal: the operator migrates it on purpose
    if text == "LIVE_JOURNAL_PATH_REDIRECTED":
        return "EXECUTION_JOURNAL_INTEGRITY_FAILURE"  # a symlink/junction in the journal path
    if "already in use" in text:
        return "EXECUTION_JOURNAL_IN_USE"  # another AutoFund process owns the single-writer journal
    return "EXECUTION_JOURNAL_INTEGRITY_FAILURE"  # corrupt, truncated, or authentication stripped


class SafeIdleRunner:
    """Safe runner shell: production adapters can stream decisions into it.

    It owns no exchange transport, so it can never claim Production readiness.
    """

    def __init__(self, *, demo: bool = False) -> None:
        self.running = False
        self.killed = False
        self.observability = MvpObservability(demo=demo)
        # Read-only wallet observation state. Present on every runner so the read
        # model has one shape regardless of mode.
        self.wallet_balances: tuple[Any, ...] = ()
        self.wallet_status = "UNAVAILABLE"
        self.wallet_error: str | None = None
        self.wallet_observed_at: str | None = None
        # Economic admission state. The guard gates financial intents only; it
        # never changes strategy thresholds or the Champion fingerprint.
        self._economic_policy = DEFAULT_ECONOMIC_POLICY
        self._taker_fee_rate = Decimal("0")
        self._slippage_bps = DEFAULT_SLIPPAGE_BPS
        self._economically_rejected = 0
        # Latest depth observed by the market loop, so the operator's economic
        # view tracks the market without the guard performing its own reads.
        self._last_depth: Any = None
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
                "realized_pnl_mxn": "0", "unrealized_pnl_mxn": "0", "fees_mxn": "0",
                "wallet": {"status": self.wallet_status, "error": self.wallet_error, "read_only": True,
                           "observed_at": self.wallet_observed_at, "balances": []},
                "signals": {"admitted": 0, "suppressed_pending_order": 0,
                            "strategy_buy_decisions": 0, "strategy_sell_decisions": 0,
                            "economically_rejected": 0},
                "economics": {"position_open": False, "classification": "NO_POSITION",
                              "economically_rejected": 0,
                              "policy": self._economic_policy.public()},
                "blocked_recovery": {"blocked": False, "origins": []}}

    def refresh_wallet(self) -> None:
        """Safe-idle runners own no exchange transport, so there is nothing to read."""

    @property
    def economic_policy(self) -> EconomicPolicy:
        """The economic admission policy this runner enforces.

        Reported so a session's admission rules are auditable, never hidden.
        """
        return self._economic_policy


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
        # Signal admission accounting: actionable decisions cannot be silently dropped.
        self._admitted = 0
        self._suppressed = 0
        self._blocked_recovery: tuple[str, ...] = ()
        self._on_blocked: Any = None
        # Actionable strategy decisions, counted independently of admission so a
        # suppressed signal can never be silently discarded.
        self._strategy_buy_decisions = 0
        self._strategy_sell_decisions = 0
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
            self.wallet_observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
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

    def refresh_wallet(self) -> None:
        """Public GET-only wallet refresh, used before exporting diagnostics."""
        self._refresh_wallet()

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
        except Exception as exc:
            # Refusing to proceed is always correct; the blocker says *why*, because the three
            # causes need three different responses from the operator.
            self._block_with(journal_blocker_for(exc))
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
        # The economic guard uses the confirmed account fee and the executable ask
        # from the same preflight, so it needs no market read of its own and can
        # never disagree with the order path about fees, prices or spread.
        if checked.fees.taker_fee_decimal > 0:
            self._taker_fee_rate = checked.fees.taker_fee_decimal
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
        self._admitted = self._suppressed = 0
        self._strategy_buy_decisions = self._strategy_sell_decisions = 0
        self._economically_rejected = 0
        self._blocked_recovery = ()
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
                self._last_depth = depth
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
            self._strategy_buy_decisions += 1
            self.handle_signal("BUY")
        elif evidence.decision == DECISION_SELL:
            self._strategy_sell_decisions += 1
            self.handle_signal("SELL")
        else:
            self._event("NO_SIGNAL", component="strategy", message=evidence.message, **payload)

    def _exit_economics(self, *, checked: Any, target_price: Decimal, exit_class: str,
                        correlation: str) -> Any:
        """Model selling the owned position at `target_price`, using `checked`.

        `checked` is the validated GET-only preflight the execution path itself
        uses, so the guard judges the same fees, prices and spread the order will
        actually face. The guard never performs its own market reads.
        """
        position = self.execution.wallet.positions.get("BTC/MXN")
        if position is None or position.quantity <= 0:
            return None
        model = economic_exit_model(
            book="BTC/MXN", quantity=position.quantity, cost_basis_mxn=position.cost_basis_mxn,
            strategy_exit_price_mxn=target_price, best_bid_mxn=checked.depth.best_bid,
            exit_fee_rate=checked.fees.taker_fee_decimal, spread_bps=checked.depth.spread_bps,
            slippage_bps=self._slippage_bps, policy=self._economic_policy, exit_class=exit_class)
        self._event("ECONOMIC_EXIT_EVALUATED", component="economics", correlation_id=correlation,
                    message=model.outcome, side="SELL", **model.telemetry())
        self._event("ECONOMIC_EXIT_PASS" if model.admissible else "ECONOMIC_EXIT_REJECT",
                    component="economics", correlation_id=correlation,
                    level="INFO" if model.admissible else "WARNING",
                    message=model.reason or model.classification, side="SELL", **model.telemetry())
        return model

    def _economically_admissible(self, side: str, correlation: str) -> bool:
        """Whether a strategy signal may become a financial intent.

        Strategy and economics are separate concerns. The Champion decides from
        price structure alone and its fingerprint is unchanged; this decides
        whether the trade it proposes is expected to be profitable after the
        trading costs AutoFund actually pays. A rejection is recorded with
        structured evidence and the strategy decision stays visible.

        It fails closed: when the economics cannot be established, no intent is
        created. That is deliberate, because the alternative is admitting an
        unresearched financial commitment.
        """
        checked = self.last_preflight
        if checked is None or checked.fees.taker_fee_decimal <= 0:
            self._economics_reject(side, correlation, INSUFFICIENT_ECONOMIC_EVIDENCE)
            return False
        try:
            if side == "SELL":
                position = self.execution.wallet.positions.get("BTC/MXN")
                if position is None or position.quantity <= 0:
                    return True  # no inventory: the existing risk check decides
                target = exit_boundary(position.cost_basis_mxn / position.quantity)
                model = self._exit_economics(checked=checked, target_price=target,
                                             exit_class=PROFIT_TAKING, correlation=correlation)
                if model is not None and not model.admissible:
                    self._economically_rejected += 1
                    return False
                return True
            if side == "BUY":
                budget = checked.budget
                entry_price = checked.depth.best_ask
                if budget <= 0 or entry_price <= 0:
                    self._economics_reject(side, correlation, INSUFFICIENT_ECONOMIC_EVIDENCE)
                    return False
                # Model the position this exact intent would create and price it at
                # the Champion's own exit boundary: the price the strategy will
                # really attempt to sell at. No profit is assumed for the strategy.
                fee = checked.fees.taker_fee_decimal
                owned_major = (budget / entry_price) * (Decimal("1") - fee)
                if owned_major <= 0:
                    self._economics_reject(side, correlation, INSUFFICIENT_ECONOMIC_EVIDENCE)
                    return False
                model = economic_entry_model(
                    book="BTC/MXN", budget_mxn=budget, buy_price_mxn=entry_price,
                    target_price_mxn=exit_boundary(budget / owned_major),
                    buy_fee_rate=fee, sell_fee_rate=fee, spread_bps=checked.depth.spread_bps,
                    slippage_bps=self._slippage_bps, policy=self._economic_policy)
                self._event("ECONOMIC_EDGE_EVALUATED", component="economics",
                            correlation_id=correlation, message=model.outcome, side="BUY",
                            **model.telemetry())
                self._event("ECONOMIC_EDGE_PASS" if model.admissible else "ECONOMIC_EDGE_REJECT",
                            component="economics", correlation_id=correlation,
                            level="INFO" if model.admissible else "WARNING",
                            message=model.reason or model.outcome, side="BUY",
                            **model.telemetry())
                if not model.admissible:
                    self._economically_rejected += 1
                    return False
                return True
        except Exception:
            self._economics_reject(side, correlation, INSUFFICIENT_ECONOMIC_EVIDENCE)
            return False
        return True

    def _economics_reject(self, side: str, correlation: str, reason: str) -> None:
        """Record an economic refusal that produced no complete model."""
        self._economically_rejected += 1
        entry = side == "BUY"
        self._event("ECONOMIC_EDGE_EVALUATED" if entry else "ECONOMIC_EXIT_EVALUATED",
                    component="economics", correlation_id=correlation, message=reason, side=side)
        self._event("ECONOMIC_EDGE_REJECT" if entry else "ECONOMIC_EXIT_REJECT",
                    component="economics", correlation_id=correlation, level="WARNING",
                    message=reason, side=side)

    def handle_signal(self, side: str) -> None:
        if not self.running or self.killed:
            return
        # An acknowledged order whose outcome is not established must fail closed:
        # no new financial intent, and the session must stop presenting itself as
        # normally RUNNING. Market observation and the research scanner continue.
        blocked = tuple(getattr(self.execution, "blocked", ())) or self.execution.unresolved
        if blocked:
            self._suppressed += 1
            self._event("SIGNAL_SUPPRESSED_PENDING_ORDER", component="execution", level="WARNING",
                        message=f"{side} not admitted: unresolved order", side=side,
                        suppressed_signals=self._suppressed,
                        blocking_origin=blocked[0] if blocked else None,
                        reason="UNRESOLVED_ORDER_BLOCKS_NEW_INTENTS")
            self._blocked_recovery = tuple(blocked)
            if self._on_blocked is not None:
                try:
                    self._on_blocked(tuple(blocked))
                except Exception:
                    pass
            return
        correlation = uuid4().hex
        # Economic admission runs BEFORE this signal is counted as admitted or any
        # durable intent exists. A strategy signal is not a financial decision.
        if not self._economically_admissible(side, correlation):
            return
        self._admitted += 1
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
                           "read_only": True, "observed_at": self.wallet_observed_at,
                           "balances": wallet},
                "signals": {"admitted": self._admitted, "suppressed_pending_order": self._suppressed,
                            "strategy_buy_decisions": self._strategy_buy_decisions,
                            "strategy_sell_decisions": self._strategy_sell_decisions,
                            "economically_rejected": self._economically_rejected},
                "economics": self._economics_view(),
                "blocked_recovery": {"blocked": bool(self._blocked_recovery),
                                     "origins": list(self._blocked_recovery)},
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

    def _economics_view(self) -> dict[str, Any]:
        """Live economic exit model for the open AutoFund position.

        Informational only: it explains why a profit-taking exit may be refused.
        It never creates an intent and never blocks startup.
        """
        policy = self._economic_policy.public()
        position = self.execution.wallet.positions.get("BTC/MXN")
        checked = self.last_preflight
        if position is None or position.quantity <= 0:
            return {"position_open": False, "policy": policy,
                    "economically_rejected": self._economically_rejected,
                    "classification": "NO_POSITION"}
        if checked is None:
            return {"position_open": True, "policy": policy, "classification": "UNKNOWN",
                    "admissible": None, "reason": INSUFFICIENT_ECONOMIC_EVIDENCE,
                    "economically_rejected": self._economically_rejected}
        target = exit_boundary(position.cost_basis_mxn / position.quantity)
        depth = self._last_depth or checked.depth
        model = economic_exit_model(
            book="BTC/MXN", quantity=position.quantity, cost_basis_mxn=position.cost_basis_mxn,
            strategy_exit_price_mxn=target, best_bid_mxn=depth.best_bid,
            exit_fee_rate=checked.fees.taker_fee_decimal, spread_bps=depth.spread_bps,
            slippage_bps=self._slippage_bps, policy=self._economic_policy, exit_class=PROFIT_TAKING)
        return {"position_open": True, "classification": model.classification,
                "admissible": model.admissible, "outcome": model.outcome, "reason": model.reason,
                "policy": policy, "economically_rejected": self._economically_rejected,
                **{k: v for k, v in model.telemetry().items() if k not in {"policy", "version"}}}


class DemoAutonomousRunner(SafeIdleRunner):
    """Deterministic browser fixture; never imports or calls an exchange client."""

    # Demo-fixture values. They exist so the economic exit panel can be rendered
    # and visually verified without any exchange traffic. They are derived from
    # the position this fixture already publishes (average cost 11 MXN/BTC) and
    # are explicitly labelled DEMO wherever they surface: they are not evidence
    # about Production, and no Production decision ever reads them.
    DEMO_QUANTITY = Decimal("0.0000109")
    DEMO_COST_BASIS = Decimal("0.0001199")  # 0.0000109 BTC at 11 MXN/BTC
    DEMO_MARK = Decimal("10.92")
    DEMO_TAKER_FEE = Decimal("0.0078")

    def __init__(self) -> None:
        super().__init__(demo=True)
        self.step = 0
        self.event: Any = None

    @classmethod
    def _demo_economics(cls, quantity: Decimal, cost_basis_mxn: Decimal) -> dict[str, Any]:
        """Clearly-marked DEMO economic exit model.

        The demo runner holds no exchange transport, so it has no confirmed account
        fee; the rate here is an illustrative fixture constant and never a claim
        about a real account.
        """
        target = exit_boundary(cost_basis_mxn / quantity)
        model = economic_exit_model(
            book="BTC/MXN", quantity=quantity, cost_basis_mxn=cost_basis_mxn,
            strategy_exit_price_mxn=target, best_bid_mxn=cls.DEMO_MARK,
            exit_fee_rate=cls.DEMO_TAKER_FEE, exit_class=PROFIT_TAKING)
        return {"position_open": True, "classification": model.classification,
                "admissible": model.admissible, "outcome": model.outcome, "reason": model.reason,
                "demo": True, "source": "DEMO_FIXTURE",
                "economically_rejected": 0, "policy": DEFAULT_ECONOMIC_POLICY.public(),
                **{k: v for k, v in model.telemetry().items()
                   if k not in {"policy", "version"}}}

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
                                  "unrealized_pnl_mxn": "-0.08", "fees_mxn": "0.08", "strategy_version": "0.1"},
                        economics=self._demo_economics(self.DEMO_QUANTITY, self.DEMO_COST_BASIS))
        elif self.step >= 2:
            base.update(cash_mxn="49.80", equity_mxn="49.80", deployed_mxn="0", realized_pnl_mxn="-0.20",
                        fees_mxn="0.16", position=None)
        if "economics" not in base:
            # Always present so the read model has one shape; the demo stands in no
            # position, so it reports no position rather than inventing economics.
            base["economics"] = {"position_open": False, "classification": "NO_POSITION",
                                 "demo": True, "source": "DEMO_FIXTURE",
                                 "economically_rejected": 0,
                                 "policy": DEFAULT_ECONOMIC_POLICY.public()}
        return base

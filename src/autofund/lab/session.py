"""Paper trading session: plan, authorize, execute, reconcile, kill.

Paper only: the client it uses cannot reach a live host. Even so, the session is run like a real
one, because the point is to learn how a strategy behaves with real fills:

* A human authorizes it with a typed phrase, and sees that the strategy is ``UNCERTIFIED`` first.
* Hard caps live in code (capital fraction, per-order size), not in a setting.
* Orders are idempotent: the client order id is derived from the plan, so a repeat cannot double.
* An unknown outcome is reconciled by client order id, and an unresolvable one halts the session.
* ``kill`` cancels open orders and halts. ``flatten`` is a separate, explicitly confirmed action.
"""

import hashlib
import json
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

from autofund.exchanges.alpaca.errors import (
    AlpacaError,
    AlpacaOrderRejected,
    AlpacaUnknownSubmissionOutcome,
)
from autofund.exchanges.alpaca.paper import AlpacaPaperClient, Order

from .journal import Journal
from .signals import STRATEGIES, SignalError, SignalProvider, SignalSet

START_PHRASE = "START PAPER EXPERIMENT"
FLATTEN_PHRASE = "FLATTEN PAPER"
MAX_CAPITAL_FRACTION = Decimal("0.5")
MIN_CAPITAL_FRACTION = Decimal("0.001")
MAX_ORDER_FRACTION = Decimal("0.25")
MIN_ORDER = Decimal("10")
PLAN_MAX_AGE_SECONDS = 600
RECONCILE_WINDOW_SECONDS = 60
CENT = Decimal("0.01")

STOPPED, ACTIVE, HALTED = "STOPPED", "ACTIVE", "HALTED"


class SessionError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SessionConfig:
    strategy_id: str
    capital_fraction: Decimal
    auto_rebalance: bool = False


@dataclass(frozen=True, slots=True)
class PlannedOrder:
    symbol: str
    action: str  # BUY | SELL | CLOSE
    notional: Decimal
    current_value: Decimal
    target_value: Decimal

    def public(self) -> dict[str, str]:
        return {"symbol": self.symbol, "action": self.action, "notional": str(self.notional),
                "current_value": str(self.current_value), "target_value": str(self.target_value)}


@dataclass(frozen=True, slots=True)
class Plan:
    plan_id: str
    created_at: datetime
    config: SessionConfig
    as_of: str
    equity: Decimal
    capital: Decimal
    orders: tuple[PlannedOrder, ...]
    blocked: tuple[str, ...]
    signals: tuple[dict[str, Any], ...]

    def public(self) -> dict[str, Any]:
        return {"plan_id": self.plan_id, "created_at": self.created_at.isoformat(),
                "strategy_id": self.config.strategy_id, "as_of": self.as_of,
                "equity": str(self.equity), "capital": str(self.capital),
                "capital_fraction": str(self.config.capital_fraction),
                "orders": [o.public() for o in self.orders], "blocked": list(self.blocked),
                "signals": list(self.signals), "executable": not self.blocked}


def validate_config(config: SessionConfig) -> None:
    if config.strategy_id not in STRATEGIES:
        raise SessionError("unknown strategy")
    fraction = config.capital_fraction
    # NaN and infinities are not numbers an order can be sized from, and comparing a NaN raises.
    if not fraction.is_finite() or not MIN_CAPITAL_FRACTION <= fraction <= MAX_CAPITAL_FRACTION:
        raise SessionError(
            f"capital fraction must be between {MIN_CAPITAL_FRACTION} and {MAX_CAPITAL_FRACTION}")


class PaperSession:
    def __init__(
        self,
        *,
        root: Path,
        paper: Callable[[], AlpacaPaperClient],
        signals: SignalProvider,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        auto_interval_seconds: float = 60,
    ) -> None:
        self._dir = root / "paper"
        self._paper, self._signals, self._now, self._sleep = paper, signals, now, sleep
        self._auto_interval = auto_interval_seconds
        self.journal = Journal(self._dir / "journal.jsonl")
        self._equity_log = Journal(self._dir / "equity.jsonl")
        self._state_path = self._dir / "state.json"
        self._lock = threading.RLock()
        self._stop_auto = threading.Event()
        self._kill_requested = threading.Event()
        self._auto: threading.Thread | None = None
        self.state = STOPPED
        self.config: SessionConfig | None = None
        self.session_id: str | None = None
        self.started_at: datetime | None = None
        self.halt_reason: str | None = None
        self.last_plan: Plan | None = None
        self.last_execution: dict[str, Any] | None = None
        self._last_equity_at: datetime | None = None
        self._auto_errors = 0

    # ---- persisted rebalance marker ----

    def last_rebalance(self) -> date | None:
        try:
            return date.fromisoformat(json.loads(self._state_path.read_text(encoding="utf-8"))["last_rebalance"])
        except (OSError, ValueError, KeyError):
            return None

    def _mark_rebalanced(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps({"last_rebalance": self._now().date().isoformat()}),
                                    encoding="utf-8")

    # ---- planning ----

    def build_plan(self, config: SessionConfig) -> Plan:
        validate_config(config)
        client = self._paper()
        account = client.get_account()
        if account.trading_blocked or account.status != "ACTIVE":
            raise SessionError("the paper account is not active for trading")
        strategy = STRATEGIES[config.strategy_id]
        sig: SignalSet = self._signals.signals(config.strategy_id)
        held = {p.symbol: p.market_value for p in client.get_positions()}
        capital = (account.equity * config.capital_fraction).quantize(CENT, rounding=ROUND_DOWN)
        per_symbol = (capital / len(strategy.universe)).quantize(CENT, rounding=ROUND_DOWN)
        cap_order = (account.equity * MAX_ORDER_FRACTION).quantize(CENT, rounding=ROUND_DOWN)
        blocked: list[str] = []
        if client.list_orders("open", 50):
            blocked.append("open orders are still pending; wait for them or cancel them first")
        orders: list[PlannedOrder] = []
        for symbol in strategy.universe:
            current = held.get(symbol, Decimal("0"))
            target = per_symbol if sig.position(symbol) else Decimal("0")
            diff = target - current
            if target == 0 and current > 0:
                orders.append(PlannedOrder(symbol, "CLOSE", current, current, target))
            elif diff >= MIN_ORDER:
                orders.append(PlannedOrder(symbol, "BUY", min(diff, cap_order).quantize(CENT, ROUND_DOWN),
                                           current, target))
            elif -diff >= MIN_ORDER and current > 0:
                orders.append(PlannedOrder(symbol, "SELL", min(-diff, current, cap_order).quantize(
                    CENT, ROUND_DOWN), current, target))
        digest = hashlib.sha256(json.dumps(
            [config.strategy_id, str(config.capital_fraction), self._now().date().isoformat(),
             [o.public() for o in orders]], sort_keys=True).encode()).hexdigest()[:12]
        plan = Plan(digest, self._now(), config, sig.as_of, account.equity, capital, tuple(orders),
                    tuple(blocked), tuple(r.public() for r in sig.rows))
        self.last_plan = plan
        self.journal.append("PLAN", plan_id=digest, strategy=config.strategy_id, orders=len(orders),
                            blocked=blocked, equity=account.equity, as_of=sig.as_of)
        return plan

    # ---- lifecycle ----

    def start(self, config: SessionConfig, phrase: str) -> None:
        validate_config(config)
        if phrase != START_PHRASE:
            raise SessionError("authorization phrase does not match")
        with self._lock:
            if self.state == ACTIVE:
                raise SessionError("a session is already active")
            account = self._paper().get_account()
            if account.trading_blocked or account.status != "ACTIVE":
                raise SessionError("the paper account is not active for trading")
            self._kill_requested.clear()
            self.state, self.config = ACTIVE, config
            self.session_id = "paper-" + self._now().strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(2)
            self.started_at, self.halt_reason, self.last_execution = self._now(), None, None
            self._auto_errors = 0
            self.journal.append("SESSION_STARTED", session=self.session_id, strategy=config.strategy_id,
                                capital_fraction=config.capital_fraction, auto=config.auto_rebalance,
                                equity=account.equity, certification="UNCERTIFIED_EXPERIMENT")
            if config.auto_rebalance:
                self._stop_auto.clear()
                self._auto = threading.Thread(target=self._auto_loop, name="autofund-paper-auto",
                                              daemon=False)
                self._auto.start()

    def _end_auto(self) -> None:
        self._stop_auto.set()
        thread = self._auto
        if thread is not None and thread is not threading.current_thread():
            thread.join(10)

    def stop(self, reason: str = "operator") -> None:
        with self._lock:
            if self.state != ACTIVE:
                return
            self._end_auto()
            cancelled = True
            try:
                self._paper().cancel_open_orders()
            except AlpacaError:
                cancelled = False
            self.state = STOPPED
            self.journal.append("SESSION_STOPPED", session=self.session_id, reason=reason,
                                open_orders_cancelled=cancelled)

    def kill(self, reason: str = "operator") -> None:
        """Halt now. It must never wait behind an execution that holds the session lock.

        An execution can sit in the lock for a minute while it reconciles fills; a kill switch that
        queued behind it would not be one. So the kill is signalled first (the execution stops
        placing orders and reconciling at its next step), open orders are cancelled without the
        lock, and only the bookkeeping waits, briefly, for it.
        """
        self._kill_requested.set()
        self._stop_auto.set()
        cancelled = True
        try:
            self._paper().cancel_open_orders()
        except AlpacaError:
            cancelled = False
        got_lock = self._lock.acquire(timeout=3)
        try:
            self.state, self.halt_reason = HALTED, reason
            self.journal.append("SESSION_KILLED", session=self.session_id, reason=reason,
                                open_orders_cancelled=cancelled, waited_for_lock=got_lock)
        finally:
            if got_lock:
                self._lock.release()

    def shutdown(self) -> None:
        """Application exit: end automation. Broker-side orders and positions are untouched."""
        with self._lock:
            self._end_auto()
            if self.state == ACTIVE:
                self.state = STOPPED
                self.journal.append("SESSION_ENDED_BY_APP_EXIT", session=self.session_id)

    def flatten(self, phrase: str) -> list[dict[str, str]]:
        if phrase != FLATTEN_PHRASE:
            raise SessionError("flatten confirmation does not match")
        client = self._paper()
        universe: set[str] = set()
        for s in STRATEGIES.values():
            universe.update(s.universe)
        closed: list[dict[str, str]] = []
        for position in client.get_positions():
            if position.symbol in universe:
                client.close_position(position.symbol)
                closed.append({"symbol": position.symbol, "market_value": str(position.market_value)})
        self.journal.append("FLATTEN", session=self.session_id, closed=closed)
        return closed

    # ---- execution ----

    def execute(self, plan_id: str, *, queue_if_closed: bool = False) -> dict[str, Any]:
        with self._lock:
            if self.state != ACTIVE:
                raise SessionError("start a session before executing a plan")
            plan = self.last_plan
            if plan is None or plan.plan_id != plan_id:
                raise SessionError("plan not found; build a fresh plan")
            if (self._now() - plan.created_at).total_seconds() > PLAN_MAX_AGE_SECONDS:
                raise SessionError("plan is stale; build a fresh plan")
            if plan.blocked:
                raise SessionError("plan is blocked: " + "; ".join(plan.blocked))
            client = self._paper()
            clock = client.get_clock()
            if not clock.is_open and not queue_if_closed:
                raise SessionError("the market is closed; confirm queuing for the next open to proceed")
            results: list[dict[str, Any]] = []
            for order in plan.orders:
                if self._kill_requested.is_set():
                    break
                results.append(self._place(client, plan, order))
                if self.state == HALTED:
                    break
            if self._kill_requested.is_set():
                # An order submitted while the kill was being signalled must not survive it.
                try:
                    client.cancel_open_orders()
                except AlpacaError:
                    pass
            elif clock.is_open:
                self._reconcile(client, results)
            # A rebalance is "done for today" only if every order went through; a rejected or
            # unresolved one must be retried, not silently counted.
            if all(r.get("status") not in ("rejected", "UNKNOWN") for r in results):
                self._mark_rebalanced()
            self.last_execution = {"plan_id": plan_id, "market_open": clock.is_open, "orders": results,
                                   "at": self._now().isoformat()}
            self.journal.append("EXECUTION", plan_id=plan_id, market_open=clock.is_open, orders=results)
            return self.last_execution

    def _place(self, client: AlpacaPaperClient, plan: Plan, order: PlannedOrder) -> dict[str, Any]:
        cid = f"af-{plan.plan_id}-{order.symbol}"
        out: dict[str, Any] = {"symbol": order.symbol, "action": order.action,
                               "notional": str(order.notional), "client_order_id": cid}
        try:
            if order.action == "CLOSE":
                client.close_position(order.symbol)
                out["status"] = "close_requested"
            else:
                placed = client.submit_market_notional(
                    symbol=order.symbol, side=order.action.lower(), notional=order.notional,
                    client_order_id=cid)
                out.update(status=placed.status, order_id=placed.id)
        except AlpacaOrderRejected as exc:
            out["status"] = "rejected"
            out["error"] = str(exc)
        except AlpacaUnknownSubmissionOutcome:
            found = None
            try:
                found = client.get_order_by_client_id(cid)
            except AlpacaError:
                found = None
            if found is None:
                out["status"] = "UNKNOWN"
                self.state, self.halt_reason = HALTED, "unresolved order outcome"
                self.journal.append("HALTED_UNCERTAIN_ORDER", client_order_id=cid)
            else:
                out.update(status=found.status, order_id=found.id, recovered=True)
        self.journal.append("ORDER", **{k: v for k, v in out.items()})
        return out

    def _reconcile(self, client: AlpacaPaperClient, results: list[dict[str, Any]]) -> None:
        deadline = time.monotonic() + RECONCILE_WINDOW_SECONDS
        pending = {r["client_order_id"]: r for r in results if r.get("order_id")}
        while pending and time.monotonic() < deadline and not self._kill_requested.is_set():
            for cid, row in list(pending.items()):
                order: Order | None = client.get_order_by_client_id(cid)
                if order is None:
                    continue
                row.update(status=order.status, filled_qty=str(order.filled_qty),
                           filled_avg_price=None if order.filled_avg_price is None
                           else str(order.filled_avg_price))
                if order.terminal:
                    pending.pop(cid)
            if pending:
                self._sleep(2)

    # ---- observation ----

    def snapshot_equity(self, *, force: bool = False) -> dict[str, Any] | None:
        now = self._now()
        if not force and self._last_equity_at and (now - self._last_equity_at).total_seconds() < 30:
            return None
        client = self._paper()
        account, positions = client.get_account(), client.get_positions()
        self._last_equity_at = now
        row = {"equity": str(account.equity), "cash": str(account.cash),
               "positions_value": str(sum((p.market_value for p in positions), Decimal("0"))),
               "session": self.session_id}
        self._equity_log.append("EQUITY", **row)
        return row

    def equity_history(self, limit: int = 500) -> list[dict[str, Any]]:
        return self._equity_log.tail(limit)

    def status(self) -> dict[str, Any]:
        last = self.last_rebalance()
        return {"state": self.state, "session_id": self.session_id,
                "strategy_id": self.config.strategy_id if self.config else None,
                "capital_fraction": str(self.config.capital_fraction) if self.config else None,
                "auto_rebalance": bool(self.config and self.config.auto_rebalance),
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "halt_reason": self.halt_reason,
                "last_rebalance": last.isoformat() if last else None,
                "last_plan": self.last_plan.public() if self.last_plan else None,
                "last_execution": self.last_execution, "start_phrase": START_PHRASE,
                "flatten_phrase": FLATTEN_PHRASE,
                "limits": {"max_capital_fraction": str(MAX_CAPITAL_FRACTION),
                           "max_order_fraction": str(MAX_ORDER_FRACTION),
                           "min_order": str(MIN_ORDER)}}

    # ---- automatic daily evaluation (only while the app and session are running) ----

    def _auto_loop(self) -> None:
        while not self._stop_auto.wait(self._auto_interval):
            try:
                self.auto_tick()
                self._auto_errors = 0
            except (AlpacaError, SessionError, SignalError) as exc:
                self._auto_errors += 1
                self.journal.append("AUTO_ERROR", error=f"{type(exc).__name__}: {exc}"[:200])
                if self._auto_errors >= 3:
                    self.kill("three consecutive automatic evaluation errors")
                    return

    def auto_tick(self) -> str:
        """One automatic evaluation. Returns what it did, for tests and the journal."""
        with self._lock:
            if self.state != ACTIVE or self.config is None:
                return "inactive"
            client = self._paper()
            if not client.get_clock().is_open:
                return "market_closed"
            today = self._now().date()
            last = self.last_rebalance()
            if last == today:
                return "already_evaluated_today"
            strategy = STRATEGIES[self.config.strategy_id]
            due = self._signals.signals(self.config.strategy_id).bars_since(last) >= strategy.rebalance_every
            if last is not None and not due:
                return "not_due"
            plan = self.build_plan(self.config)
            if plan.blocked:
                self.journal.append("AUTO_BLOCKED", blocked=list(plan.blocked))
                return "blocked"
            if plan.orders:
                self.execute(plan.plan_id)
                return "rebalanced"
            self._mark_rebalanced()
            self.journal.append("AUTO_EVALUATION", plan_id=plan.plan_id, orders=0)
            return "no_change"

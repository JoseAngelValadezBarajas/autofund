"""MVP operator observability.

This is a *projection*, not a second observability subsystem. It reuses the F4.6
runtime contract (`autofund.dashboard.models.RuntimeView` / `RuntimeEvent`) so the
browser sees one consistent runtime shape across the legacy dashboard and the MVP
control plane.

The projection is read-only and best-effort: it never feeds strategy, risk,
execution or accounting, and any failure inside it is contained. It publishes no
data of its own invention — every value derives from a real exchange read or an
authoritative MVP telemetry checkpoint.
"""

import threading
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from autofund.dashboard.models import RuntimeEvent, RuntimeView
from autofund.replay.serialization import canonical_json

# F4.6 backend-defined semantics, reused verbatim for the MVP.
HEARTBEAT_DISCONNECT_SECONDS = 10.0
MARKET_STALE_SECONDS = 15.0
# A RUNNING real-money session that loses BOTH internal runtime heartbeat
# visibility and operator publication is no longer truthfully "healthy".
OBSERVABILITY_DEGRADED_SECONDS = 15.0
PUBLICATION_INTERVAL_SECONDS = 2.0
ACTIVE_STATES = frozenset({"STARTING", "RUNNING", "STOPPING"})
# Terminal states: the runtime is intentionally inactive. Ageing is expected and
# must never be reported as an incident.
TERMINAL_STATES = frozenset({"STOPPED", "HALTED"})
# Pipeline stage semantics.
STAGE_WAITING = "WAITING"          # an active RUNNING pipeline may still reach it
STAGE_NOT_APPLICABLE = "NOT_APPLICABLE"  # a completed path terminated before it
STAGE_UNKNOWN = "UNKNOWN"          # genuinely undeterminable
MAX_CANDLES = 40
MAX_EVENTS = 200

# market → candle → strategy → signal → capital → risk → final market check →
# order → fill → reconciliation → ledger
PIPELINE_STAGES = ("MARKET", "CANDLE", "STRATEGY", "SIGNAL", "CAPITAL", "RISK",
                   "FINAL_MARKET_CHECK", "ORDER", "FILL", "RECONCILIATION", "LEDGER")
# Telemetry checkpoint → pipeline stage and its result. Only authoritative
# checkpoints the application already emits are mapped; nothing is synthesized.
STAGE_BY_EVENT: dict[str, tuple[str, str]] = {
    "MARKET_CONNECTED": ("MARKET", "PASS"), "MARKET_DISCONNECTED": ("MARKET", "REJECT"),
    "CANDLE_CLOSED": ("CANDLE", "PASS"),
    "STRATEGY_EVALUATED": ("STRATEGY", "PASS"),
    "NO_SIGNAL": ("SIGNAL", "NONE"), "SIGNAL_GENERATED": ("SIGNAL", "PASS"),
    "CAPITAL_CHECK_PASS": ("CAPITAL", "PASS"), "CAPITAL_CHECK_REJECT": ("CAPITAL", "REJECT"),
    "RISK_CHECK_PASS": ("RISK", "PASS"), "RISK_CHECK_REJECT": ("RISK", "REJECT"),
    "FINAL_MARKET_CHECK_PASS": ("FINAL_MARKET_CHECK", "PASS"),
    "FINAL_MARKET_CHECK_REJECT": ("FINAL_MARKET_CHECK", "REJECT"),
    "ORDER_INTENT_CREATED": ("ORDER", "INTENT_CREATED"), "ORDER_SUBMITTING": ("ORDER", "SUBMITTING"),
    "ORDER_ACKNOWLEDGED": ("ORDER", "ACKNOWLEDGED"), "ORDER_OUTCOME_UNKNOWN": ("ORDER", "OUTCOME_UNKNOWN"),
    "PARTIAL_FILL": ("FILL", "PARTIAL"), "FILL": ("FILL", "PASS"),
    "RECONCILIATION_STARTED": ("RECONCILIATION", "STARTED"), "RECONCILIATION_PASS": ("RECONCILIATION", "PASS"),
    "RECONCILIATION_FAIL": ("RECONCILIATION", "REJECT"), "LEDGER_UPDATED": ("LEDGER", "PASS"),
}
COUNTER_BY_EVENT: dict[str, str] = {
    "CANDLE_CLOSED": "closed_candles", "STRATEGY_EVALUATED": "strategy_evaluations",
    "NO_SIGNAL": "no_signal", "SIGNAL_GENERATED": "signals",
    "CAPITAL_CHECK_PASS": "capital_pass", "CAPITAL_CHECK_REJECT": "capital_reject",
    "RISK_CHECK_PASS": "risk_pass", "RISK_CHECK_REJECT": "risk_reject",
    "FINAL_MARKET_CHECK_PASS": "final_market_pass", "FINAL_MARKET_CHECK_REJECT": "final_market_reject",
    "ORDER_INTENT_CREATED": "order_intents", "ORDER_SUBMITTING": "order_submissions",
    "ORDER_ACKNOWLEDGED": "order_acknowledgements", "FILL": "fills", "PARTIAL_FILL": "partial_fills",
    "RECONCILIATION_PASS": "reconciliations", "RECONCILIATION_FAIL": "reconciliation_failures",
    "LEDGER_UPDATED": "ledger_updates",
    "AUTO_HALT_TRIGGERED": "halts", "LOSS_LIMIT_HIT": "halts", "KILL_SWITCH_TRIGGERED": "halts",
}
REJECT_EVENTS = frozenset(event for event, (_, status) in STAGE_BY_EVENT.items() if status == "REJECT")


def _age(now: datetime, at: datetime | None) -> float | None:
    return None if at is None else max(0.0, (now - at).total_seconds())


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else None


def _quality(market_age: float | None) -> str:
    if market_age is None:
        return "VALID"
    return "VALID" if market_age <= MARKET_STALE_SECONDS else "DEGRADED" if market_age <= 60 else "INVALID"


class MvpObservability:
    """Bounded, in-memory operator view of a live MVP session."""

    def __init__(self, *, market: str = "btc_mxn", demo: bool = False) -> None:
        self.market = market
        self.demo = demo
        self.session_id: str | None = None
        self.status = "STOPPED"
        self.started_at: datetime | None = None
        self._lock = threading.RLock()
        self._heartbeat_at: datetime | None = None      # internal runtime liveness
        self._published_at: datetime | None = None      # operator publication
        self._market_at: datetime | None = None
        self._closed_candle_at: datetime | None = None
        self._market_snapshot: dict[str, Any] | None = None
        self._open_candle: dict[str, Any] | None = None
        self._candles: deque[dict[str, Any]] = deque(maxlen=MAX_CANDLES)
        self._events: deque[RuntimeEvent] = deque(maxlen=MAX_EVENTS)
        self._event_id = 0
        self._stages: dict[str, dict[str, Any]] = {
            stage: {"stage": stage, "status": "WAITING", "at": None, "event": None, "detail": None}
            for stage in PIPELINE_STAGES}
        self._counters: dict[str, int] = dict.fromkeys(set(COUNTER_BY_EVENT.values()), 0)
        self._durations_ms: dict[str, list[float]] = {}
        self._last_decision: dict[str, Any] | None = None
        self._last_evaluation_at: datetime | None = None
        self._market_events = 0
        self._market_unavailable = 0
        self._last_known_quality: str | None = None
        self._final_quality: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- session
    def begin(self, session_id: str, started_at: datetime) -> None:
        with self._lock:
            self.session_id, self.started_at, self.status = session_id, started_at, "STARTING"
            self._heartbeat_at = self._published_at = started_at
        self._start_publication()

    def _start_publication(self) -> None:
        """Backend publication heartbeat, independent of the trading loop.

        If this thread can no longer publish, the operator view is stale even
        though trading may still be running — that is exactly the condition the
        degraded guard reports. It never influences trading.
        """
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._publish_loop, name="mvp-observability-publish", daemon=True)
            self._thread.start()

    def _publish_loop(self) -> None:
        while not self._stop.wait(PUBLICATION_INTERVAL_SECONDS):
            self.publish()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def end(self, status: str) -> None:
        """Terminal transition: freeze market health as a historical fact.

        Once stopped, the runtime no longer publishes market events, so ageing
        must not be reinterpreted as STALE or INVALID. The last observed quality
        is retained, and stages the completed decision path never needed become
        NOT_APPLICABLE rather than UNKNOWN.
        """
        with self._lock:
            self.status = status
            if status in TERMINAL_STATES:
                # Freeze the last live quality as a historical fact before ageing
                # could otherwise reinterpret deliberate inactivity as INVALID.
                self._final_quality = _quality(_age(datetime.now(UTC), self._market_at))
            if status != "DISCONNECTED":
                self._finalize_stages()

    def _finalize_stages(self) -> None:
        """Resolve never-entered stages using the decision path actually taken."""
        order = list(PIPELINE_STAGES)
        last = -1
        for index, stage in enumerate(order):
            if self._stages[stage]["status"] != STAGE_WAITING:
                last = index
        if last < 0:
            return
        for index in range(last + 1, len(order)):
            stage = order[index]
            if self._stages[stage]["status"] == STAGE_WAITING:
                self._stages[stage] = {"stage": stage, "status": STAGE_NOT_APPLICABLE, "at": None,
                                       "event": None,
                                       "detail": "Decision path terminated before this stage was required"}

    def heartbeat(self, *, status: str | None = None, at: datetime | None = None) -> None:
        """Internal runtime liveness; called by the runner loop itself.

        This is deliberately independent of operator publication: the loop can
        still be trading while the publication path is broken, and vice versa.
        """
        with self._lock:
            self._heartbeat_at = at or datetime.now(UTC)
            if status is not None:
                self.status = status

    def publish(self, *, at: datetime | None = None) -> None:
        """Record that the operator publication path produced a fresh view.

        Driven by the backend, never by whether a browser tab happens to be
        open, so a closed browser cannot masquerade as an observability failure.
        """
        with self._lock:
            self._published_at = at or datetime.now(UTC)

    def duration(self, name: str, milliseconds: float) -> None:
        with self._lock:
            self._durations_ms[name] = [*self._durations_ms.get(name, [])[-49:], round(milliseconds, 3)]

    # ----------------------------------------------------------------- market
    def observe_market(self, depth: Any, *, latency_ms: float | None = None) -> None:
        """Record one real GET-only order book observation. Observational only."""
        try:
            self._observe_market(depth, latency_ms)
        except Exception:
            pass  # Operator visibility can never affect trading.

    def _observe_market(self, depth: Any, latency_ms: float | None) -> None:
        with self._lock:
            now = datetime.now(UTC)
            self._market_at = now
            self._market_events += 1
            self._market_snapshot = {"last_price_mxn": str(depth.best_bid), "best_bid_mxn": str(depth.best_bid),
                                     "best_ask_mxn": str(depth.best_ask),
                                     "spread_mxn": str(depth.best_ask - depth.best_bid),
                                     "spread_bps": str(depth.spread_bps), "orderbook_at": _iso(depth.timestamp),
                                     "sequence": depth.sequence,
                                     "request_latency_ms": None if latency_ms is None else round(latency_ms)}
            self._last_known_quality = _quality(0.0)
            self._set_stage("MARKET", "PASS", "Public BTC/MXN depth observed")
            self._fold_open_candle(depth, now)
            if latency_ms is not None:
                self.duration("market_request_rtt", latency_ms)

    def market_unavailable(self, detail: str) -> None:
        with self._lock:
            self._market_unavailable += 1
            self._set_stage("MARKET", "REJECT", detail)
            self._record(datetime.now(UTC), "MARKET_UNAVAILABLE", "market", "ERROR", detail)

    def _fold_open_candle(self, depth: Any, now: datetime) -> None:
        """Fold real polled prices into the current UTC minute.

        OBSERVATIONAL ONLY. The strategy keeps using the existing closed-minute
        series; nothing here is ever a strategy input.
        """
        start = now.replace(second=0, microsecond=0)
        end = start.replace(second=59, microsecond=999999)
        price = depth.best_bid
        current = self._open_candle
        if current is None or self._parse(current["interval_start"]) != start:
            if current is not None:
                self._close_candle(current)
            self._open_candle = {"status": "OPEN", "interval_start": _iso(start), "interval_end": _iso(end),
                                 "open": str(price), "high": str(price), "low": str(price), "last": str(price),
                                 "volume": "0", "trade_count": 1}
            return
        current["high"] = str(max(Decimal(current["high"]), price))
        current["low"] = str(min(Decimal(current["low"]), price))
        current["last"] = str(price)
        current["trade_count"] = int(current["trade_count"]) + 1

    def _close_candle(self, candle: dict[str, Any]) -> None:
        self._candles.append({**candle, "status": "CLOSED", "close": candle["last"]})
        self._closed_candle_at = datetime.now(UTC)

    # -------------------------------------------------------------- telemetry
    def observe_checkpoint(self, row: dict[str, Any]) -> None:
        """Project one authoritative MVP telemetry checkpoint onto this view."""
        try:
            self._observe_checkpoint(row)
        except Exception:
            pass

    def _observe_checkpoint(self, row: dict[str, Any]) -> None:
        event = str(row.get("event", ""))
        at = self._parse(row.get("timestamp_utc")) or datetime.now(UTC)
        component = str(row.get("component", ""))
        level = str(row.get("level", "INFO"))
        correlation_id = row.get("correlation_id")
        with self._lock:
            self._heartbeat_at = at
            self._record(at, event, component, level, str(row.get("message", "")),
                         correlation_id if isinstance(correlation_id, str) else None,
                         checkpoint_id=str(row["checkpoint_id"]) if row.get("checkpoint_id") else None)
            if event in COUNTER_BY_EVENT:
                self._counters[COUNTER_BY_EVENT[event]] += 1
            if event in STAGE_BY_EVENT:
                stage, status = STAGE_BY_EVENT[event]
                self._set_stage(stage, status, str(row.get("message", "")), event=event)
            if event == "CANDLE_CLOSED":
                self._last_evaluation_at = at
            elif event == "STRATEGY_EVALUATED":
                self._last_evaluation_at = at
            elif event == "NO_SIGNAL":
                self._last_decision = {"at": _iso(at), "decision": "NO_SIGNAL", "signal": None,
                                       "reason": str(row.get("message", "")), "correlation_id": None}
            elif event == "SIGNAL_GENERATED":
                self._last_decision = {"at": _iso(at), "decision": "SIGNAL", "signal": str(row.get("message", "")),
                                       "reason": "Signal generated from a closed candle",
                                       "correlation_id": correlation_id}
            elif event in {"LOSS_LIMIT_HIT", "AUTO_HALT_TRIGGERED", "KILL_SWITCH_TRIGGERED"}:
                self.status = "HALTED"

    def _record(self, at: datetime, event: str, component: str, level: str, message: str,
                correlation_id: str | None = None, *, checkpoint_id: str | None = None) -> None:
        self._event_id += 1
        self._events.append(RuntimeEvent.model_validate({
            "event_id": self._event_id, "timestamp": at, "event_type": event,
            "session_id": self.session_id or "af-mvp-pending", "market": self.market,
            "summary": message or component, "outcome": "REJECT" if event in REJECT_EVENTS else None,
            "component": component or None, "level": level or None,
            "correlation_id": correlation_id, "checkpoint_id": checkpoint_id}))

    def _set_stage(self, stage: str, status: str, detail: str, *, event: str | None = None) -> None:
        self._stages[stage] = {"stage": stage, "status": status, "at": _iso(datetime.now(UTC)),
                               "event": event, "detail": detail}

    @staticmethod
    def _parse(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return None

    # ------------------------------------------------------------------ views
    def runtime(self, *, now: datetime | None = None) -> dict[str, Any]:
        with self._lock:
            now = now or datetime.now(UTC)
            heartbeat_age = _age(now, self._heartbeat_at)
            market_age = _age(now, self._market_at)
            active = self.status in ACTIVE_STATES
            terminal = self.status in TERMINAL_STATES
            status = "DISCONNECTED" if active and heartbeat_age is not None and heartbeat_age > HEARTBEAT_DISCONNECT_SECONDS else self.status
            if terminal:
                # A deliberately stopped runtime is INACTIVE, never STALE or
                # DISCONNECTED. Ageing is not an incident once trading has ended.
                heartbeat = "INACTIVE"
                market_stream = "INACTIVE"
                runtime_state = "INACTIVE"
            else:
                heartbeat = "DISCONNECTED" if status == "DISCONNECTED" else (
                    "LIVE" if market_age is not None and market_age <= MARKET_STALE_SECONDS else "STALE")
                market_stream = "ACTIVE" if market_age is not None and market_age <= MARKET_STALE_SECONDS else "INACTIVE"
                runtime_state = "RUNNING"
            elapsed = int((now - self.started_at).total_seconds()) if self.started_at else 0
            raw = {"demo_mode": self.demo, "snapshot_ready": True, "session_id": self.session_id, "status": status,
                   "mode": "MVP-AUTONOMOUS", "started_at": _iso(self.started_at), "elapsed_seconds": max(0, elapsed),
                   "last_event_at": _iso(self._heartbeat_at), "last_market_event_at": _iso(self._market_at),
                   "last_closed_candle_at": _iso(self._closed_candle_at), "last_state_update_at": _iso(now),
                   "market": self.market, "interval": "1m", "strategy_id": None,
                   "current_candle": self._open_candle, "market_snapshot": self._market_snapshot,
                   # Retained historical facts, not a live health judgement.
                   # While active, quality tracks the live market age (existing
                   # F4.6 degradation semantics). Once terminal, the final observed
                   # quality is retained and is never re-derived from inactivity.
                   "quality": (self._final_quality or _quality(market_age)) if terminal else _quality(market_age),
                   "last_known_quality": self._last_known_quality or _quality(market_age),
                   "last_known_market_at": _iso(self._market_at),
                   "runtime_state": runtime_state, "market_stream": market_stream,
                   "risk_status": "HALTED" if self.status == "HALTED" else "NORMAL",
                   "accounting_status": "PASS", "heartbeat": heartbeat,
                   "events": [event.model_dump(mode="json") for event in self._events]}
            return RuntimeView.model_validate_json(canonical_json(raw)).model_dump(mode="json")

    def market_state(self, *, now: datetime | None = None) -> str:
        """First-minute UX: distinguish waiting / processing / stale / disconnected.

        STALE means the loop is alive but market data is aging; DISCONNECTED means
        the loop itself stopped reporting. The two bands do not overlap.
        """
        with self._lock:
            if self.status not in {"STARTING", "RUNNING"}:
                return "NOT_RUNNING"
            now = now or datetime.now(UTC)
            heartbeat_age = _age(now, self._heartbeat_at)
            if heartbeat_age is not None and heartbeat_age > HEARTBEAT_DISCONNECT_SECONDS:
                return "MARKET_DATA_DISCONNECTED"
            market_age = _age(now, self._market_at)
            if market_age is None:
                return "WAITING_FOR_FIRST_MARKET_EVENT"
            if market_age > MARKET_STALE_SECONDS:
                return "MARKET_DATA_STALE"
            return "PROCESSING_MARKET_DATA"

    def health(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Backend-authoritative observability health. Never halts trading."""
        with self._lock:
            now = now or datetime.now(UTC)
            heartbeat_age = _age(now, self._heartbeat_at)
            publication_age = _age(now, self._published_at)
            running = self.status in ACTIVE_STATES
            degraded = bool(running and heartbeat_age is not None and publication_age is not None
                            and heartbeat_age > OBSERVABILITY_DEGRADED_SECONDS
                            and publication_age > OBSERVABILITY_DEGRADED_SECONDS)
            return {"status": "OBSERVABILITY_DEGRADED" if degraded else "HEALTHY", "degraded": degraded,
                    "heartbeat_age_seconds": heartbeat_age, "publication_age_seconds": publication_age,
                    "degraded_after_seconds": OBSERVABILITY_DEGRADED_SECONDS,
                    "scope": "BACKEND_TELEMETRY_RUNTIME_HEALTH", "trading_effect": "NONE",
                    "browser_tab_closure_effect": "NONE"}

    def pipeline(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(self._stages[stage]) for stage in PIPELINE_STAGES]

    def strategy(self) -> dict[str, Any]:
        with self._lock:
            return {"last_evaluation_at": _iso(self._last_evaluation_at), "last_decision": self._last_decision,
                    "evaluations": self._counters["strategy_evaluations"], "signals": self._counters["signals"],
                    "no_signal": self._counters["no_signal"], "market_state": self.market_state()}

    def candles(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._candles)

    def metrics(self) -> dict[str, Any]:
        with self._lock:
            durations = {name: {"count": len(values), "last_ms": values[-1], "max_ms": max(values)}
                         for name, values in self._durations_ms.items()}
            return {**self._counters, "market_events": self._market_events,
                    "market_unavailable": self._market_unavailable, "durations_ms": durations}

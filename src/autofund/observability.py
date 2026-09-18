"""Ephemeral, bounded shadow projections. Never changes durable financial state."""

import json
import logging
import threading
from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from autofund.decimal_utils import financial
from autofund.observer.source import MarketFrame, MarketNotice
from autofund.replay.serialization import canonical_json
from autofund.shadow.aggregation import minute
from autofund.shadow.session import ShadowSession

LOG = logging.getLogger(__name__)
DEFAULT_RUNTIME_PATH = Path(__file__).parents[2] / "artifacts" / "runtime" / "current.json"


class RuntimePublisher:
    """One coalesced snapshot slot; important events remain in a bounded history.

    The writer has no references to mutable domain objects. Files are atomically
    replaced, never appended or used by replay. Failures cannot reach the runner.
    """

    def __init__(self, path: Path = DEFAULT_RUNTIME_PATH, *, market: str = "btc_mxn") -> None:
        self.path, self.market = path, market
        self.session_id = uuid4().hex
        self.started_at = datetime.now(UTC)
        self.events: deque[dict[str, Any]] = deque(maxlen=500)
        self.sequence = 0
        self.counts = (0, 0, 0, 0, 0)
        self.quality = "VALID"
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()
        self._stop = threading.Event()
        self._latest: dict[str, Any] = {}
        self._event("SESSION_STARTING", "Shadow session starting", self.started_at)
        self._latest = json.loads(canonical_json(self._base("STARTING", self.started_at)))
        self._thread = threading.Thread(target=self._writer, name="autofund-observability", daemon=True)
        self._thread.start()

    def _event(self, kind: str, summary: str, at: datetime, **fields: object) -> None:
        self.sequence += 1
        self.events.append({"event_id": self.sequence, "timestamp": at, "event_type": kind,
                            "session_id": self.session_id, "market": self.market, "summary": summary, **fields})

    def _base(self, status: str, at: datetime) -> dict[str, Any]:
        return {"schema_version": "autofund.runtime.v1", "session_id": self.session_id,
                "status": status, "mode": "SHADOW", "started_at": self.started_at,
                "last_event_at": at, "last_state_update_at": at,
                "last_market_event_at": None, "last_closed_candle_at": None,
                "market": self.market, "interval": "1m", "strategy_id": None,
                "current_candle": None, "market_snapshot": None,
                "snapshot_ready": False, "risk_status": "UNKNOWN", "accounting_status": "UNKNOWN",
                "quality": "VALID", "events": list(self.events)}

    def observe(self, session: ShadowSession, frame: MarketFrame | MarketNotice | None = None,
                *, status: str = "RUNNING", recovery: bool = False) -> None:
        try:
            self._observe(session, frame, status=status, recovery=recovery)
        except Exception:
            LOG.warning("Runtime observation unavailable; durable session unaffected")

    @financial
    def _observe(self, session: ShadowSession, frame: MarketFrame | MarketNotice | None,
                 *, status: str, recovery: bool) -> None:
        at = frame.observed_at if frame else datetime.now(UTC)
        candles, signals, executions, ledger, rejections = self.counts
        if not candles and not signals and not executions and frame is None and status == "RUNNING":
            self._event("RECOVERY" if recovery else "SESSION_STARTED", "Shadow session running", at)
        if isinstance(frame, MarketFrame):
            self._event("MARKET_EVENT", f"Public snapshot; {len(frame.trades)} received trades", at)
        elif isinstance(frame, MarketNotice):
            self._event("MARKET_UNAVAILABLE", frame.kind, at)
        for candle in session.candles[candles:]:
            self._event("CANDLE_CLOSED", "Accepted closed UTC 1m candle", at, source_candle_timestamp=candle.timestamp)
        for signal in session.signals[signals:]:
            self._event("STRATEGY_EVALUATED", "Reference strategy evaluated closed candle", at,
                        source_candle_timestamp=signal["candle"])
            candidate = signal["intent"]
            if candidate is None:
                self._event("NO_SIGNAL", "No actionable intent", at)
            else:
                self._event("SIGNAL_GENERATED", str(signal["decision"]), at, side=signal["decision"],
                            strategy_id=session.strategy.identity.fingerprint, source_candle_timestamp=signal["candle"])
                self._event("SHADOW_ORDER_INTENT", "Intent awaits next eligible snapshot", at, side=signal["decision"],
                            strategy_id=session.strategy.identity.fingerprint, source_candle_timestamp=signal["candle"])
        for execution in session.executions[executions:]:
            # Success proves the core checks passed; no checks are rerun here.
            self._event("CAPITAL_CHECK", "PASS: core accepted shadow allocation", at, outcome="PASS")
            self._event("RISK_CHECK", "PASS: core accepted shadow fill", at, outcome="PASS")
            self._event("SHADOW_FILL", "Virtual fill only", at, side=execution.fill.side.value,
                        amount_mxn=execution.fill.gross_notional_mxn, price=execution.fill.execution_price_mxn)
        for rejection in session.rejections[rejections:]:
            # F4 exposes a combined result, not the outcome of every intermediate check.
            self._event("SHADOW_REJECTED", str(rejection["reason"]), at, outcome="REJECT")
            if str(rejection["reason"]).startswith("RISK"):
                self._event("RISK_CHECK", str(rejection["reason"]), at, outcome="REJECT")
        for entry in session.wallet.ledger[ledger:]:
            self._event("LEDGER_UPDATED", entry.type.value, at, ledger_entry_id=entry.entry_id)
        if session.quality != self.quality:
            self._event("QUALITY_CHANGED", f"{self.quality} -> {session.quality}", at, quality=session.quality)
            self.quality = session.quality
        if session.halt_reason:
            status = "HALTED"
            if self._latest.get("runtime", {}).get("status") != "HALTED":
                self._event("SESSION_HALTED", session.halt_reason, at)
        elif status == "STOPPED":
            self._event("SESSION_STOPPED", "Session completed", at)
        self.counts = (len(session.candles), len(session.signals), len(session.executions),
                       len(session.wallet.ledger), len(session.rejections))
        view = self._base(status, at)
        view["snapshot_ready"] = True
        view.update(strategy_id=session.strategy.identity.fingerprint, quality=session.quality,
                    risk_status="HALTED" if session.halt_reason else "NORMAL",
                    accounting_status="FAIL" if session.halt_reason == "ACCOUNTING_HALT" else "PASS")
        latest = session.last_frame
        if latest:
            view["last_market_event_at"] = latest.observed_at
            depth = latest.depth
            view["market_snapshot"] = {"last_price_mxn": max(session.aggregator.seen.values(), key=lambda t: (t.timestamp, t.trade_id)).price
                                       if session.aggregator.seen else None,
                                       "best_bid_mxn": depth.best_bid, "best_ask_mxn": depth.best_ask,
                                       "spread_mxn": depth.best_ask - depth.best_bid, "spread_bps": depth.spread_bps,
                                       "orderbook_at": depth.timestamp}
            bucket = minute(latest.observed_at)
            trades = sorted(session.aggregator.pending.get(bucket, []), key=lambda t: (t.timestamp, t.trade_id))
            if trades:
                prices = [t.price for t in trades]
                view["current_candle"] = {"interval_start": bucket, "interval_end": bucket + timedelta(minutes=1),
                                          "open": prices[0], "high": max(prices), "low": min(prices),
                                          "last": prices[-1], "volume": sum((t.amount for t in trades), Decimal("0")),
                                          "trade_count": len(trades), "status": "OPEN"}
        view["last_closed_candle_at"] = session.candles[-1].timestamp if session.candles else None
        payload = {"runtime": view, "header": {"config": session.config, "limits": session.limits,
                   "fee": session.fee, "start": session.start}, "result": session.result(), "depth": latest.depth if latest else None}
        # Freeze all mutable domain references before handing them to the writer.
        frozen: dict[str, Any] = json.loads(canonical_json(payload))
        with self._lock:
            self._latest = frozen

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snapshot: dict[str, Any] = json.loads(json.dumps(self._latest))
            return snapshot

    def _writer(self) -> None:
        while not self._stop.wait(0.5):
            self.flush()

    def flush(self) -> bool:
        with self._file_lock:
            return self._flush()

    def _flush(self) -> bool:
        try:
            payload = self.snapshot()
            if "runtime" not in payload:
                payload = {"runtime": json.loads(canonical_json(payload))}
            payload["runtime"]["last_state_update_at"] = datetime.now(UTC).isoformat()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + "." + self.session_id + ".tmp")
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            temporary.replace(self.path)
            return True
        except PermissionError:
            # Windows readers may briefly deny atomic replacement. The next
            # coalesced writer tick retries without blocking market processing.
            return False
        except (OSError, ValueError, TypeError):
            LOG.warning("Runtime publication unavailable; durable session unaffected")
            return False

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        with self._lock:
            view = self._latest.get("runtime", self._latest)
            if view.get("status") not in {"STOPPED", "HALTED"}:
                at = datetime.now(UTC)
                self._event("SESSION_HALTED", "Session ended before clean finalization", at)
                view.update(status="HALTED", last_event_at=at.isoformat(),
                            events=json.loads(canonical_json(list(self.events))))
        for _ in range(5):
            if self.flush():
                break
            threading.Event().wait(0.05)
        else:
            LOG.warning("Final runtime snapshot unavailable; durable session unaffected")

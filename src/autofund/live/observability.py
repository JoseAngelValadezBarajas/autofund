"""Best-effort atomic publication of OWN live ledger data, never balances."""

import json
import os
import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from autofund.dashboard.live import DEFAULT_RUNTIME_PATH
from autofund.dashboard.models import RuntimeEvent, RuntimeView
from autofund.replay.serialization import canonical_json


class LivePublisher:
    def __init__(self, path: Path = DEFAULT_RUNTIME_PATH) -> None:
        self.path = path
        self.started = datetime.now(UTC)
        self.session_id = "af-live-session-" + uuid4().hex[:16]
        self.events: deque[RuntimeEvent] = deque(maxlen=500)
        self.counter = 0
        self.market_at: datetime | None = None
        self._last: RuntimeView | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._heartbeat, daemon=True, name="live-monitor-heartbeat")
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _write(self, view: RuntimeView) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp-" + self.session_id)
        temporary.write_text(canonical_json({"runtime": view.model_dump(mode="json")}), encoding="utf-8")
        os.replace(temporary, self.path)

    def _heartbeat(self) -> None:
        while not self._stop.wait(1):
            try:
                with self._lock:
                    if self._last and self._last.status == "RUNNING":
                        self._write(self._last.model_copy(update={"last_state_update_at": datetime.now(UTC)}))
            except Exception:
                pass

    def event(self, state: str, accounting: dict[str, Any], *, status: str = "RUNNING") -> None:
        try:
            now = datetime.now(UTC)
            self.counter += 1
            event_types = {"PREFLIGHT_STARTED": "LIVE_PREFLIGHT_STARTED", "PREFLIGHT_PASS": "LIVE_PREFLIGHT_PASSED",
                "PREFLIGHT_REJECTED": "LIVE_PREFLIGHT_REJECTED", "INTENT_CREATED": "REAL_ORDER_INTENT_CREATED",
                "AWAITING_OPERATOR": "AWAITING_OPERATOR_CONFIRMATION", "SUBMITTING": "REAL_ORDER_SUBMITTING",
                "ACKNOWLEDGED": "REAL_ORDER_ACKNOWLEDGED", "OUTCOME_UNKNOWN": "REAL_ORDER_OUTCOME_UNKNOWN",
                "PARTIALLY_FILLED": "REAL_PARTIAL_FILL", "FILLED": "REAL_FILL", "RECONCILED": "REAL_RECONCILED",
                "HALTED": "REAL_EXECUTION_HALTED"}
            self.events.append(RuntimeEvent(event_id=self.counter, timestamp=now,
                event_type=event_types.get(state, state), session_id=self.session_id, market="btc_mxn", summary=state))
            # Validation is a privacy allowlist: MicroLiveView strips any
            # unknown fields before the payload can reach the browser.
            raw = {"demo_mode": False, "snapshot_ready": True, "session_id": self.session_id,
                "status": "HALTED" if state == "HALTED" else status, "mode": "MICRO-LIVE",
                "started_at": self.started, "elapsed_seconds": int((now - self.started).total_seconds()),
                "last_event_at": now, "last_market_event_at": self.market_at, "last_state_update_at": now,
                "risk_status": "UNKNOWN" if state == "PREFLIGHT_STARTED" else "HALTED" if state in {"HALTED", "PREFLIGHT_REJECTED"} else "NORMAL",
                "accounting_status": "PASS", "events": [x.model_dump() for x in self.events], "micro_live": accounting}
            view = RuntimeView.model_validate(json.loads(canonical_json(raw)))
            with self._lock:
                self._last = view
                self._write(view)
        except Exception:
            pass

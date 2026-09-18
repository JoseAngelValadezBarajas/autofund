"""Explicit deterministic display scenario; no engine or exchange is invoked."""
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .models import OpenCandle, RuntimeEvent, RuntimeView


class DemoRuntime:
    def __init__(self) -> None:
        self.started = time.monotonic()

    def snapshot(self) -> RuntimeView:
        step = min(4, int(((time.monotonic() - self.started) % 24) / 4))
        at = datetime(2026, 9, 18, 0, 0, tzinfo=UTC)
        kinds = ["SESSION_STARTED", "MARKET_EVENT"]
        if step >= 2:
            kinds += ["CANDLE_CLOSED", "STRATEGY_EVALUATED", "NO_SIGNAL"]
        if step >= 3:
            kinds += ["QUALITY_CHANGED"]
        if step >= 4:
            kinds += ["SESSION_STOPPED"]
        return RuntimeView(demo_mode=True, session_id="demo-runtime", status="STOPPED" if step == 4 else "RUNNING",
                           started_at=at, elapsed_seconds=step * 4, last_event_at=at + timedelta(seconds=step * 4),
                           last_market_event_at=at + timedelta(seconds=step * 4), last_state_update_at=at + timedelta(seconds=step * 4),
                           last_closed_candle_at=at if step >= 2 else None, strategy_id="DEMO reference strategy",
                           heartbeat="LIVE" if step < 4 else "STALE", quality="INVALID" if step >= 3 else "DEGRADED",
                           risk_status="NORMAL", accounting_status="PASS",
                           current_candle=OpenCandle.model_validate(json.loads((Path(__file__).with_name("fixtures") / "runtime_candle.json").read_text())) if step < 2 else None,
                           events=[RuntimeEvent(event_id=i + 1, timestamp=at + timedelta(seconds=i), event_type=kind,
                                                session_id="demo-runtime", market="btc_mxn", summary="DEMO " + kind) for i, kind in enumerate(kinds)])

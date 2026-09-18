"""Read-only ephemeral snapshot reader, independent of the trading engine."""
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import RuntimeView

DEFAULT_RUNTIME_PATH = Path(__file__).parents[3] / "artifacts" / "runtime" / "current.json"


def read_runtime(path: Path) -> dict[str, Any] | None:
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        RuntimeView.model_validate(payload["runtime"])
        return payload
    except (OSError, ValueError, KeyError, TypeError):
        return None


def project_runtime(raw: dict[str, Any], *, now: datetime | None = None) -> RuntimeView:
    view = RuntimeView.model_validate(raw)
    now = now or datetime.now(UTC)
    age = max(0, (now - view.last_state_update_at).total_seconds()) if view.last_state_update_at else float("inf")
    market_age = max(0, (now - view.last_market_event_at).total_seconds()) if view.last_market_event_at else float("inf")
    active = view.status in {"STARTING", "RUNNING", "STOPPING"}
    status = "DISCONNECTED" if active and age > 10 else view.status
    heartbeat = "DISCONNECTED" if age > 10 or status == "DISCONNECTED" else "STALE" if market_age > 15 else "LIVE"
    if status in {"STOPPED", "HALTED"}:
        heartbeat = "STALE"
    end = now if active and age <= 10 else (view.last_event_at or now)
    elapsed = max(0, int((end - view.started_at).total_seconds())) if view.started_at else 0
    return RuntimeView.model_validate({**view.model_dump(), "status": status, "heartbeat": heartbeat, "elapsed_seconds": elapsed})

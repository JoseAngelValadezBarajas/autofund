"""Append-only JSON-lines journal. One writer per process, safe across threads."""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append(self, event: str, **fields: Any) -> dict[str, Any]:
        row = {"t": datetime.now(UTC).isoformat(timespec="milliseconds"), "event": event, **fields}
        line = json.dumps(row, default=str, separators=(",", ":"), sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
        return row

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue  # a torn final line never blocks reading the rest
        return out

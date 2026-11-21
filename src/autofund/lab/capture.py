"""Quote capture that you start and stop. It lives inside the app process and dies with it.

There is no scheduler and no background service: ``start()`` spawns one thread owned by this
object, ``stop()`` (or the application exiting) ends it, and ``status()`` always says whether it is
running, since when, and how much it has written. Nothing resumes by itself after a restart.
"""

import json
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autofund.exchanges.alpaca.client import AlpacaDataClient
from autofund.exchanges.alpaca.errors import AlpacaAuthenticationError, AlpacaError
from autofund.mvp.alpaca_economics import half_spread_bps, in_regular_session

MIN_INTERVAL, MAX_INTERVAL = 5, 3600


class CaptureError(Exception):
    pass


class CaptureController:
    def __init__(
        self,
        *,
        root: Path,
        data_client: Callable[[], AlpacaDataClient],
        symbols: tuple[str, ...],
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._dir = root / "capture"
        self._client_factory, self._symbols, self._now = data_client, symbols, now
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._interval = 30
        self._reset()

    def _reset(self) -> None:
        self._started_at: datetime | None = None
        self._stopped_at: datetime | None = None
        self._stop_reason: str | None = None
        self._polls = 0
        self._written = 0
        self._in_session = 0
        self._errors = 0
        self._last_error: str | None = None
        self._last_poll_at: datetime | None = None
        self._last_seen: dict[str, tuple[str, str, str]] = {}

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, interval_seconds: int = 30) -> dict[str, Any]:
        if not MIN_INTERVAL <= interval_seconds <= MAX_INTERVAL:
            raise CaptureError(f"interval must be {MIN_INTERVAL}-{MAX_INTERVAL} seconds")
        with self._lock:
            if self.running:
                raise CaptureError("capture is already running")
            self._reset()
            self._interval = interval_seconds
            self._started_at = self._now()
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="autofund-capture", daemon=False)
            self._thread.start()
        return self.status()

    def stop(self, reason: str = "operator", *, timeout: float = 15) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                self._stop_reason = reason
                self._stop.set()
        if thread is not None:
            thread.join(timeout)
        return self.status()

    def poll_once(self) -> int:
        """One sampling pass. Returns how many new quotes were written."""
        client = self._client_factory()
        quotes = client.get_latest_quotes(list(self._symbols), feed="iex")
        taken = self._now()
        lines: list[str] = []
        in_session = 0
        for symbol, q in quotes.items():
            key = (q.at.isoformat(), str(q.bid), str(q.ask))
            if self._last_seen.get(symbol) == key:
                continue  # an unchanged quote is one observation, not several
            self._last_seen[symbol] = key
            inside = in_regular_session(q.at)
            in_session += inside
            lines.append(json.dumps({
                "taken_at": taken.isoformat(), "quote_at": q.at.isoformat(), "symbol": symbol,
                "bid": str(q.bid), "ask": str(q.ask), "half_spread_bps": f"{half_spread_bps(q):.4f}",
                "in_session": inside, "feed": "iex"}, separators=(",", ":")))
        if lines:
            self._dir.mkdir(parents=True, exist_ok=True)
            path = self._dir / f"quotes-{taken.date().isoformat()}.jsonl"
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\n".join(lines) + "\n")
        self._polls += 1
        self._written += len(lines)
        self._in_session += in_session
        self._last_poll_at = taken
        return len(lines)

    def _run(self) -> None:
        backoff = 0
        while not self._stop.is_set():
            try:
                self.poll_once()
                backoff = 0
            except AlpacaAuthenticationError:
                self._errors += 1
                self._last_error = "authentication failed; capture stopped"
                self._stop_reason = "authentication"
                break
            except (AlpacaError, OSError) as exc:
                self._errors += 1
                self._last_error = f"{type(exc).__name__}: {exc}"[:200]
                backoff = min(backoff * 2 or 5, 120)
            self._stop.wait(self._interval + backoff)
        self._stopped_at = self._now()

    def status(self) -> dict[str, Any]:
        files = sorted(self._dir.glob("quotes-*.jsonl")) if self._dir.is_dir() else []
        return {
            "running": self.running,
            "interval_seconds": self._interval,
            "symbols": list(self._symbols),
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "stopped_at": self._stopped_at.isoformat() if self._stopped_at else None,
            "stop_reason": self._stop_reason,
            "polls": self._polls,
            "quotes_written": self._written,
            "in_session_quotes": self._in_session,
            "errors": self._errors,
            "last_error": self._last_error,
            "last_poll_at": self._last_poll_at.isoformat() if self._last_poll_at else None,
            "files": len(files),
            "bytes": sum(f.stat().st_size for f in files),
            "directory": "research/capture",
            "note": ("Capture runs only while this application is open and stops when it exits."),
        }


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Test helper: poll a condition without sleeping a fixed time."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()

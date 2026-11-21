"""Monitoring failures must never change financial behavior, and must never be invisible either.

The order path emits state to dashboards and journals for observers. If an observer breaks, the order
path has to carry on, so those calls are wrapped. Wrapping them in a bare ``except: pass`` made a
broken monitor look exactly like a healthy one. :func:`swallow` keeps the "carry on" and adds the two
things that were missing: a count anyone can read, and a log line (at most one per site per interval)
naming the *kind* of failure. It never records the exception message or the payload, because either
can contain order or account data.
"""

import logging
import threading
import time
from collections import Counter

LOGGER = logging.getLogger("autofund.monitoring")
LOG_INTERVAL_SECONDS = 60.0

_lock = threading.Lock()
_counts: Counter[str] = Counter()
_last_logged: dict[str, float] = {}


def swallow(site: str, exc: BaseException) -> None:
    """Record that ``site`` failed with ``exc``'s type, without letting it propagate."""
    now = time.monotonic()
    with _lock:
        _counts[site] += 1
        count = _counts[site]
        due = now - _last_logged.get(site, -LOG_INTERVAL_SECONDS) >= LOG_INTERVAL_SECONDS
        if due:
            _last_logged[site] = now
    if due:
        LOGGER.warning("monitoring failure at %s (%s); %d so far. Trading is unaffected.",
                       site, type(exc).__name__, count)


def failure_counts() -> dict[str, int]:
    with _lock:
        return dict(_counts)


def reset() -> None:
    """Test helper."""
    with _lock:
        _counts.clear()
        _last_logged.clear()

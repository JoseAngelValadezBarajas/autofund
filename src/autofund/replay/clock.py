from datetime import datetime

from .errors import ReplayValidationError
from .serialization import utc_timestamp


class ReplayClock:
    """Only the engine advances the clock, once per dataset candle label."""

    __slots__ = ("_current",)

    def __init__(self) -> None:
        self._current: datetime | None = None

    @property
    def current(self) -> datetime:
        if self._current is None:
            raise ReplayValidationError("replay clock has not started")
        return self._current

    def advance(self, timestamp: datetime) -> None:
        timestamp = utc_timestamp(timestamp)
        if self._current is not None and timestamp <= self._current:
            raise ReplayValidationError("replay clock must advance strictly")
        self._current = timestamp

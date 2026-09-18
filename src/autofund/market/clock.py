import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .errors import ConfigurationError


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class NetworkPolicy:
    stale_after: int = 15
    connect_timeout: int = 10
    initial_delay: int = 1
    maximum_delay: int = 30
    multiplier: int = 2
    max_reconnects: int = 5
    metadata_attempts: int = 3

    def __post_init__(self) -> None:
        for name in (
            "stale_after",
            "connect_timeout",
            "initial_delay",
            "maximum_delay",
            "multiplier",
            "metadata_attempts",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ConfigurationError(f"{name} must be a positive integer")
        if (
            type(self.max_reconnects) is not int
            or self.max_reconnects < 0
            or self.maximum_delay < self.initial_delay
            or self.multiplier < 1
        ):
            raise ConfigurationError("invalid retry configuration")

    def delay(self, attempt: int) -> int:
        if attempt < 0:
            raise ConfigurationError("negative retry attempt")
        delay = self.initial_delay
        for _ in range(attempt):
            delay = min(self.maximum_delay, delay * self.multiplier)
            if delay == self.maximum_delay:
                break
        return delay


class StaleMonitor:
    def __init__(self, clock: Clock, stale_after: int) -> None:
        if stale_after <= 0:
            raise ConfigurationError("stale_after must be positive")
        self._clock = clock
        self._threshold = stale_after
        self._last = clock.monotonic()

    def seen(self) -> None:
        self._last = self._clock.monotonic()

    @property
    def remaining(self) -> float:
        return max(0.0, self._threshold - (self._clock.monotonic() - self._last))

    @property
    def is_stale(self) -> bool:
        return self.remaining == 0

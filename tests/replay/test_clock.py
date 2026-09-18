from datetime import UTC, datetime, timedelta

import pytest

from autofund.replay import ReplayClock, ReplayValidationError


def test_clock_only_explicit_monotonic_utc():
    clock = ReplayClock()
    with pytest.raises(ReplayValidationError):
        _ = clock.current
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    clock.advance(timestamp)
    assert clock.current == timestamp
    for invalid in (
        timestamp,
        timestamp - timedelta(seconds=1),
        timestamp.replace(tzinfo=None),
    ):
        with pytest.raises(ReplayValidationError):
            clock.advance(invalid)
        assert clock.current == timestamp
    clock.advance(timestamp + timedelta(days=1))
    assert clock.current == timestamp + timedelta(days=1)

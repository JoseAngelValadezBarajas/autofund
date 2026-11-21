"""A broken monitor is carried past, counted, and logged by kind: never silent, never leaking."""

import logging

import pytest

from autofund import monitoring


@pytest.fixture(autouse=True)
def clean():
    monitoring.reset()
    yield
    monitoring.reset()


def test_failures_are_counted_per_site():
    for _ in range(3):
        monitoring.swallow("a", ValueError("x"))
    monitoring.swallow("b", KeyError("y"))
    assert monitoring.failure_counts() == {"a": 3, "b": 1}


def test_logging_names_the_kind_but_never_the_message_or_payload(caplog):
    secret = "order-id-af-live-deadbeef balance=12.34"
    with caplog.at_level(logging.WARNING, logger="autofund.monitoring"):
        monitoring.swallow("live.execution.emit", RuntimeError(secret))
    text = caplog.text
    assert "live.execution.emit" in text and "RuntimeError" in text
    assert secret not in text and "deadbeef" not in text and "12.34" not in text


def test_logging_is_rate_limited_per_site(caplog, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(monitoring.time, "monotonic", lambda: clock[0])
    with caplog.at_level(logging.WARNING, logger="autofund.monitoring"):
        for _ in range(50):
            monitoring.swallow("noisy", ValueError())
        assert caplog.text.count("monitoring failure at noisy") == 1
        clock[0] += monitoring.LOG_INTERVAL_SECONDS + 1
        monitoring.swallow("noisy", ValueError())
        assert caplog.text.count("monitoring failure at noisy") == 2
    assert monitoring.failure_counts()["noisy"] == 51  # every failure counted, only the log is thinned


def test_the_live_executor_survives_a_failing_observer_and_records_it(tmp_path):
    from decimal import Decimal

    from autofund.live.client import BitsoProductionLiveClient, LiveCredentials
    from autofund.live.execution import LiveExecution
    from autofund.live.journal import LiveExecutionJournal
    from autofund.live.models import LiveConfig

    def broken_observer(event, payload):
        raise RuntimeError("dashboard exploded: " + str(payload)[:40])

    class NoNetwork:
        def request(self, *a, **k):  # the executor must not touch the network here
            raise AssertionError("no network in this test")

    journal = LiveExecutionJournal(tmp_path / "live.jsonl")
    try:
        client = BitsoProductionLiveClient(LiveCredentials("FAKE", "FAKE", True), transport=NoNetwork())
        engine = LiveExecution(client, journal, LiveConfig(slippage_tolerance=Decimal("0.5")),
                               emit=broken_observer)
        origin = "af-live-" + "a" * 32
        engine.intents[origin] = {"origin_id": origin}  # a known intent, so the state change is legal
        engine.states[origin] = "PREFLIGHT_PASS"
        engine.state(origin, "AWAITING_OPERATOR")  # would raise if the observer's error escaped
        engine.emit("SOMETHING", x=1)
        assert any(r["kind"] == "LIVE_STATE" for r in journal.records)  # the real record was written
        counts = monitoring.failure_counts()
        assert counts["live.execution.state"] == 1 and counts["live.execution.emit"] == 1
    finally:
        journal.close()

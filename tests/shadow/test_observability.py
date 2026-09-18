import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from autofund.dashboard.api import create_app
from autofund.dashboard.live import project_runtime
from autofund.dashboard.service import DashboardDataProvider
from autofund.observability import RuntimePublisher
from autofund.shadow.session import ShadowSession


def test_projection_does_not_change_financial_truth(session, frames, tmp_path):
    baseline = ShadowSession(session.config, session.limits, session.fee, session.start)
    publisher = RuntimePublisher(tmp_path / "runtime.json")
    try:
        publisher.observe(session)
        for frame in frames:
            baseline.process(frame)
            session.process(frame)
            publisher.observe(session, frame)
        assert session.result() == baseline.result()
        publisher.observe(session, status="STOPPED")
        publisher.close()
        raw = json.loads(publisher.path.read_text())
        assert raw["result"]["result_fingerprint"] == session.result()["result_fingerprint"]
        runtime = raw["runtime"]
        assert runtime["status"] == "STOPPED"
        assert runtime["current_candle"] is None
        kinds = [e["event_type"] for e in runtime["events"]]
        for kind in ("MARKET_EVENT", "CANDLE_CLOSED", "STRATEGY_EVALUATED", "NO_SIGNAL", "SHADOW_FILL", "LEDGER_UPDATED", "SESSION_STOPPED"):
            assert kind in kinds
        assert [e["event_id"] for e in runtime["events"]] == sorted(set(e["event_id"] for e in runtime["events"]))
        client = TestClient(create_app(DashboardDataProvider(tmp_path, runtime_path=publisher.path)))
        for endpoint in ("runtime", "overview", "market", "activity", "ledger", "shadow-fills", "signals", "sessions"):
            assert client.get("/api/v1/" + endpoint).status_code == 200
    finally:
        publisher.close()


def test_open_candle_only_accepted_pending_trades(session, frames, tmp_path):
    publisher = RuntimePublisher(tmp_path / "runtime.json")
    trade = frames[0].trades[0]
    high = replace(trade, trade_id=2, timestamp=trade.timestamp + timedelta(seconds=1), price=Decimal("1002"))
    low = replace(trade, trade_id=3, timestamp=trade.timestamp + timedelta(seconds=2), price=Decimal("999"))
    observed = session.start + timedelta(seconds=20)
    frame = replace(frames[0], observed_at=observed, depth=replace(frames[0].depth, timestamp=observed), trades=(low, trade, high, trade))
    try:
        session.process(frame)
        before = session.result()
        publisher.observe(session, frame)
        candle = publisher.snapshot()["runtime"]["current_candle"]
        assert candle["status"] == "OPEN"
        assert (candle["open"], candle["high"], candle["low"], candle["last"]) == ("1000", "1002", "999", "999")
        assert candle["volume"] == "0.03"
        assert candle["trade_count"] == 3
        assert session.result() == before
        assert not session.candles and not session.signals
    finally:
        publisher.close()


def test_buffer_and_io_failure_are_isolated(session, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    publisher = RuntimePublisher(blocker / "runtime.json")
    try:
        publisher.observe(session)
        for _ in range(600):
            publisher._event("MARKET_EVENT", "snapshot", publisher.started_at)
        publisher.observe(session, status="STOPPED")
        assert len(publisher.snapshot()["runtime"]["events"]) == 500
        publisher.flush()  # No exception, no accounting mutation.
        assert session.wallet.ledger[0].entry_id == 1
    finally:
        publisher.close()


def test_backend_health_ages_and_clean_stop():
    from datetime import UTC, datetime

    at = datetime(2026, 9, 18, tzinfo=UTC)
    raw = {"status": "RUNNING", "started_at": at, "last_event_at": at,
           "last_market_event_at": at, "last_state_update_at": at}
    assert project_runtime(raw, now=at).heartbeat == "LIVE"
    assert project_runtime({**raw, "last_state_update_at": at + timedelta(seconds=20)}, now=at + timedelta(seconds=20)).heartbeat == "STALE"
    assert project_runtime(raw, now=at + timedelta(seconds=11)).status == "DISCONNECTED"
    stopped = project_runtime({**raw, "status": "STOPPED"}, now=at + timedelta(hours=1))
    assert stopped.status == "STOPPED" and stopped.elapsed_seconds == 0


def test_failed_startup_is_halted_without_inventing_accounting(tmp_path):
    publisher = RuntimePublisher(tmp_path / "runtime.json")
    publisher.close()
    view = json.loads(publisher.path.read_text())["runtime"]
    assert view["status"] == "HALTED"
    assert view["accounting_status"] == "UNKNOWN"
    assert view["events"][-1]["event_type"] == "SESSION_HALTED"

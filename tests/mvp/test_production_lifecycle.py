"""MVP lifecycle integration with the existing F5 GET-only Production preflight.

Offline only: the F5 transport is replaced by the certified fake used by the live
suite. Nothing here may reach Bitso and no order POST is permitted.
"""

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from live.test_execution import FakeTransport

from autofund.mvp.api import create_mvp_app
from autofund.mvp.orchestrator import (
    AppState,
    AutoFundOrchestrator,
    DemoAutonomousRunner,
    ProductionAutonomousRunner,
    SessionConfig,
    SessionStartBlocked,
)

D = Decimal
CONFIRMATION = "START AUTOFUND REAL 50"
ORDER_BOOK = "GET /api/v3/order_book?book=btc_mxn"
START_BODY = {"confirmation": CONFIRMATION, "max_session_loss_mxn": "10",
              "max_session_duration_seconds": 3600, "max_orders_per_session": 10}


@pytest.fixture
def production(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_API_KEY", "FAKE")
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_API_SECRET", "FAKE")
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_PERMISSIONS_CONFIRMED", "true")
    transport = FakeTransport()
    monkeypatch.setattr("autofund.live.client.BitsoProductionLiveTransport", lambda: transport)
    runner = ProductionAutonomousRunner(tmp_path / "live.jsonl")
    orchestrator = AutoFundOrchestrator(tmp_path / "artifacts", runner)
    yield orchestrator, runner, transport
    if runner.journal is not None:
        runner.journal.close()


def test_startup_preflight_is_get_only_and_reaches_stopped_ready(production):
    app, runner, transport = production
    app.startup()
    assert app.state is AppState.STOPPED and app.auto_execution is False
    readiness = app.snapshot()["production_preflight"]
    assert readiness["ready"] is True and readiness["label"] == "READY"
    assert readiness["production_post_count"] == 0 and readiness["production_get_count"] == 4
    assert runner.last_preflight.ready
    assert transport.posts == 0
    assert {call for call in transport.calls} == {
        "GET /api/v3/balance", "GET /api/v3/fees", "GET /api/v3/available_books", ORDER_BOOK}


def test_start_runs_a_fresh_preflight_and_enables_auto_execution_without_posting(production):
    app, runner, transport = production
    app.startup()
    startup_preflight = runner.last_preflight
    app.start(SessionConfig(), CONFIRMATION)
    try:
        assert app.state is AppState.RUNNING and app.auto_execution is True
        assert transport.posts == 0
        # A start decision never reuses the startup result: the F5 preflight ran again.
        assert runner.last_preflight is not startup_preflight
        assert runner.last_preflight.checked_at >= startup_preflight.checked_at
        assert runner.last_preflight.depth.sequence >= startup_preflight.depth.sequence
        assert sum(call == ORDER_BOOK for call in transport.calls) >= 2
        assert app.snapshot()["production_preflight"]["production_post_count"] == 0
    finally:
        app.stop()
    assert app.state is AppState.STOPPED and app.auto_execution is False


def test_start_preflight_failure_blocks_without_posting_or_halting(production):
    app, _runner, transport = production
    app.startup()
    transport.stale = True  # startup was healthy; the fresh start-time GET is not
    with pytest.raises(SessionStartBlocked) as blocked:
        app.start(SessionConfig(), CONFIRMATION)
    assert blocked.value.blockers == ("STALE_MARKET",)
    assert "STALE_MARKET" in str(blocked.value)
    assert app.state is AppState.STOPPED and app.auto_execution is False
    assert app.last_error == "PRODUCTION_PREFLIGHT_BLOCKED: STALE_MARKET"
    assert transport.posts == 0
    readiness = app.snapshot()["production_preflight"]
    assert readiness["label"] == "BLOCKED" and readiness["reason"] == "STALE_MARKET"


def test_control_start_reports_exact_blocker_and_remains_retryable(production):
    app, _runner, transport = production
    app.startup()
    http = TestClient(create_mvp_app(app))
    token = http.get("/api/v1/control/session").json()["control_token"]
    headers = {"Origin": "http://testserver", "X-AutoFund-Control-Token": token}
    transport.wide = True  # ask far from bid; only the spread guard fails
    response = http.post("/api/v1/control/start", json=START_BODY, headers=headers)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "PRODUCTION_PREFLIGHT_BLOCKED" and detail["blockers"] == ["SPREAD_GUARD"]
    assert app.state is AppState.STOPPED and app.auto_execution is False
    assert transport.posts == 0
    assert http.get("/api/v1/mvp").json()["production_preflight"]["label"] == "BLOCKED"
    # The blocker clearing without a restart is enough for the normal product flow.
    transport.wide = False
    assert http.post("/api/v1/control/start", json=START_BODY, headers=headers).status_code == 200
    assert app.state is AppState.RUNNING and app.auto_execution is True
    assert transport.posts == 0


def test_startup_preflight_failure_still_reaches_stopped_without_trading(production):
    app, _runner, transport = production
    transport.minimum = "60"  # exchange minimum above the 11 MXN single-order cap
    app.startup()
    assert app.state is AppState.STOPPED and app.auto_execution is False
    readiness = app.snapshot()["production_preflight"]
    assert readiness["ready"] is False and readiness["label"] == "BLOCKED"
    assert "VALUE_LIMITS_FAIL" in readiness["reason"]
    with pytest.raises(SessionStartBlocked):
        app.start(SessionConfig(), CONFIRMATION)
    assert app.state is AppState.STOPPED
    assert transport.posts == 0


def test_demo_runner_never_claims_production_readiness(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    assert app.snapshot()["production_preflight"]["label"] == "NOT APPLICABLE"
    app.start(SessionConfig(), CONFIRMATION)
    assert app.state is AppState.RUNNING and app.auto_execution is True


def test_missing_production_credentials_block_without_any_exchange_write(tmp_path, monkeypatch):
    for name in ("AUTOFUND_BITSO_LIVE_API_KEY", "AUTOFUND_BITSO_LIVE_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    calls: list[str] = []
    monkeypatch.setattr("autofund.live.client.BitsoProductionLiveTransport.request",
                        lambda *args, **kwargs: calls.append("EXCHANGE"))
    runner = ProductionAutonomousRunner(tmp_path / "live.jsonl")
    app = AutoFundOrchestrator(tmp_path / "artifacts", runner)
    app.startup()
    assert app.state is AppState.HALTED
    assert app.last_error == "LIVE_CREDENTIALS_MISSING_OR_INVALID"
    readiness = app.snapshot()["production_preflight"]
    assert readiness["blockers"] == ["LIVE_CREDENTIALS_MISSING_OR_INVALID"]
    assert readiness["ready"] is False and readiness["guidance"]
    with pytest.raises(Exception):
        app.start(SessionConfig(), CONFIRMATION)
    assert app.state is AppState.HALTED and app.auto_execution is False
    assert calls == []

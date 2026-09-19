"""Deterministic operator-observability tests for the MVP lifecycle.

Offline only: the F5 transport is replaced by the certified fake from the live
suite. No Bitso reachability and no order POST is permitted.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from live.test_execution import FakeTransport

from autofund.mvp.observability import MvpObservability
from autofund.mvp.orchestrator import (
    AppState,
    AutoFundOrchestrator,
    DemoAutonomousRunner,
    ProductionAutonomousRunner,
    SessionConfig,
)
from autofund.observer.models import Level, OrderBookSnapshot

D = Decimal
CONFIRMATION = "START AUTOFUND REAL 50"


def depth(price: str = "999900", *, at: datetime | None = None, sequence: int = 11) -> OrderBookSnapshot:
    bid = D(price)
    return OrderBookSnapshot("btc_mxn", at or datetime.now(UTC), sequence,
                             (Level(bid, D("1")),), (Level(bid + D("100"), D("1")),))


def checkpoint(event: str, *, at: datetime | None = None, **fields: object) -> dict[str, object]:
    return {"timestamp_utc": (at or datetime.now(UTC)).isoformat(), "event": event,
            "component": fields.pop("component", "strategy"), "level": fields.pop("level", "INFO"),
            "message": fields.pop("message", ""), **fields}


@pytest.fixture
def projection() -> MvpObservability:
    view = MvpObservability()
    view.begin("mvp-test", datetime.now(UTC))
    yield view
    view.close()


def test_market_events_expose_price_spread_and_open_candle(projection):
    projection.observe_market(depth("999900"), latency_ms=12.6)
    runtime = projection.runtime()
    assert runtime["market_snapshot"]["best_bid_mxn"] == "999900"
    assert runtime["market_snapshot"]["best_ask_mxn"] == "1000000"
    assert runtime["market_snapshot"]["request_latency_ms"] == 13
    assert runtime["current_candle"]["status"] == "OPEN"
    assert runtime["current_candle"]["open"] == "999900"
    assert runtime["heartbeat"] == "LIVE"
    assert projection.market_state() == "PROCESSING_MARKET_DATA"


def test_open_candle_tracks_high_low_and_never_closes_into_strategy(projection):
    projection.observe_market(depth("1000000"))
    projection.observe_market(depth("990000"))
    candle = projection.runtime()["current_candle"]
    assert candle["high"] == "1000000" and candle["low"] == "990000" and candle["last"] == "990000"
    assert candle["trade_count"] == 2
    # Observational only: the strategy still has no closed candles.
    assert projection.candles() == []


def test_waiting_for_first_market_event_is_distinguishable(projection):
    assert projection.market_state() == "WAITING_FOR_FIRST_MARKET_EVENT"
    projection.heartbeat()
    assert projection.runtime()["heartbeat"] == "STALE"
    projection.observe_market(depth())
    assert projection.market_state() == "PROCESSING_MARKET_DATA"


def test_stale_and_disconnected_market_are_distinct(projection):
    now = datetime.now(UTC)
    projection.observe_market(depth(at=now), latency_ms=1.0)
    # Loop still alive, but the last real market read is aging -> STALE.
    projection.heartbeat(at=now + timedelta(seconds=20))
    assert projection.market_state(now=now + timedelta(seconds=20)) == "MARKET_DATA_STALE"
    assert projection.runtime(now=now + timedelta(seconds=20))["heartbeat"] == "STALE"
    assert projection.runtime(now=now + timedelta(seconds=20))["quality"] == "DEGRADED"
    # Loop itself stopped reporting -> DISCONNECTED.
    assert projection.market_state(now=now + timedelta(seconds=31)) == "MARKET_DATA_DISCONNECTED"
    assert projection.runtime(now=now + timedelta(seconds=31))["heartbeat"] == "DISCONNECTED"
    assert projection.runtime(now=now + timedelta(seconds=31))["status"] == "DISCONNECTED"


def test_no_signal_loop_records_decision_and_pipeline(projection):
    now = datetime.now(UTC)
    for event in ("MARKET_CONNECTED", "CANDLE_CLOSED", "STRATEGY_EVALUATED", "NO_SIGNAL"):
        projection.observe_checkpoint(checkpoint(event, at=now, message="Champion chose no trade"))
    strategy = projection.strategy()
    assert strategy["last_decision"]["decision"] == "NO_SIGNAL"
    assert strategy["evaluations"] == 1 and strategy["no_signal"] == 1 and strategy["signals"] == 0
    stages = {row["stage"]: row["status"] for row in projection.pipeline()}
    assert stages["MARKET"] == "PASS" and stages["CANDLE"] == "PASS"
    assert stages["STRATEGY"] == "PASS" and stages["SIGNAL"] == "NONE"
    assert stages["ORDER"] == "WAITING" and stages["LEDGER"] == "WAITING"
    assert projection.metrics()["closed_candles"] == 1


def test_signal_pipeline_reaches_ledger_with_one_correlation(projection):
    now = datetime.now(UTC)
    chain = ("CANDLE_CLOSED", "STRATEGY_EVALUATED", "SIGNAL_GENERATED", "CAPITAL_CHECK_PASS", "RISK_CHECK_PASS",
             "FINAL_MARKET_CHECK_PASS", "ORDER_INTENT_CREATED", "ORDER_SUBMITTING", "ORDER_ACKNOWLEDGED",
             "FILL", "LEDGER_UPDATED")
    for event in chain:
        projection.observe_checkpoint(checkpoint(event, at=now, component="execution",
                                                 correlation_id="corr-1", message=event))
    stages = {row["stage"]: row["status"] for row in projection.pipeline()}
    assert stages == {"MARKET": "WAITING", "CANDLE": "PASS", "STRATEGY": "PASS", "SIGNAL": "PASS",
                      "CAPITAL": "PASS", "RISK": "PASS", "FINAL_MARKET_CHECK": "PASS",
                      "ORDER": "ACKNOWLEDGED", "FILL": "PASS", "RECONCILIATION": "WAITING", "LEDGER": "PASS"}
    assert projection.strategy()["last_decision"]["decision"] == "SIGNAL"
    counters = projection.metrics()
    assert counters["fills"] == 1 and counters["order_intents"] == 1 and counters["ledger_updates"] == 1
    # Newest first is the UI's job; the projection keeps chronological order.
    assert [row["event_type"] for row in projection.runtime()["events"]][-1] == "LEDGER_UPDATED"


def test_rejections_are_counted_and_do_not_look_like_success(projection):
    projection.observe_checkpoint(checkpoint("RISK_CHECK_REJECT", component="risk", level="WARNING",
                                             message="No AutoFund inventory"))
    stages = {row["stage"]: row["status"] for row in projection.pipeline()}
    assert stages["RISK"] == "REJECT"
    assert projection.metrics()["risk_reject"] == 1 and projection.metrics()["risk_pass"] == 0
    assert projection.runtime()["events"][-1]["outcome"] == "REJECT"


def test_observability_degraded_requires_both_signals_and_never_is_default(projection):
    now = datetime.now(UTC)
    projection.observe_market(depth(at=now), latency_ms=5.0)
    projection.observe_checkpoint(checkpoint("STRATEGY_EVALUATED", at=now))
    assert projection.health(now=now)["status"] == "HEALTHY"
    # Only the market went quiet but the runtime heartbeat and publication are
    # current -> not degraded.
    projection.heartbeat(at=now + timedelta(seconds=30))
    assert projection.health(now=now + timedelta(seconds=30))["degraded"] is False
    # Both the internal heartbeat and operator publication stopped.
    projection.close()
    stale = projection.health(now=now + timedelta(seconds=60))
    assert stale["degraded"] is True and stale["status"] == "OBSERVABILITY_DEGRADED"
    assert stale["scope"] == "BACKEND_TELEMETRY_RUNTIME_HEALTH"
    # Backend-authoritative: the guard never halts trading and does not depend
    # on whether a browser tab is open.
    assert stale["trading_effect"] == "NONE"
    assert stale["browser_tab_closure_effect"] == "NONE"
    assert projection.health(now=now + timedelta(seconds=60))["heartbeat_age_seconds"] > 15


def test_stopped_session_is_not_reported_as_degraded(projection):
    projection.observe_market(depth(), latency_ms=1.0)
    projection.end("STOPPED")
    now = datetime.now(UTC) + timedelta(minutes=5)
    assert projection.health(now=now)["degraded"] is False
    assert projection.market_state(now=now) == "NOT_RUNNING"


def test_projection_never_invents_demo_data_in_real_money_mode(projection):
    projection.observe_checkpoint(checkpoint("STRATEGY_EVALUATED", message="real"))
    runtime = projection.runtime()
    assert runtime["demo_mode"] is False
    assert runtime["current_candle"] is None and runtime["market_snapshot"] is None
    assert [row["event_type"] for row in runtime["events"]] == ["STRATEGY_EVALUATED"]


def test_demo_runner_projection_is_marked_demo(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    app.snapshot()
    runtime = app.snapshot()["runtime"]
    assert runtime["demo_mode"] is True
    assert runtime["current_candle"]["status"] == "OPEN"
    app.stop()


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


def test_running_session_exposes_live_operator_state_without_posting(production):
    app, _runner, transport = production
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        view = app.snapshot()
        assert view["app_state"] is AppState.RUNNING and view["auto_execution"] is True
        assert view["runtime"]["status"] == "RUNNING" and view["runtime"]["mode"] == "MVP-AUTONOMOUS"
        assert view["runtime"]["demo_mode"] is False
        assert view["session"]["elapsed_seconds"] is not None
        assert view["session"]["remaining_seconds"] <= view["session"]["max_duration_seconds"]
        assert view["session"]["max_orders_per_session"] == 10
        assert view["observability"]["status"] == "HEALTHY"
        assert view["market_state"] in {"WAITING_FOR_FIRST_MARKET_EVENT", "PROCESSING_MARKET_DATA"}
        assert next(row["stage"] for row in view["pipeline"]) == "MARKET"
        assert view["pipeline"][-1]["stage"] == "LEDGER"
        assert transport.posts == 0
    finally:
        app.stop()
    stopped = app.snapshot()
    assert stopped["runtime"]["status"] == "STOPPED"
    assert stopped["market_state"] == "NOT_RUNNING"
    assert transport.posts == 0


def test_halted_session_reports_halted_runtime(production):
    app, _runner, transport = production
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    app.kill("operator emergency kill")
    view = app.snapshot()
    assert view["app_state"] is AppState.HALTED
    assert view["runtime"]["status"] == "HALTED" and view["runtime"]["risk_status"] == "HALTED"
    assert view["auto_execution"] is False and transport.posts == 0


def test_second_instance_reports_journal_lock_without_reaching_the_exchange(tmp_path, monkeypatch):
    """Single-writer journal ownership must fail closed and explain itself."""
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_API_KEY", "FAKE")
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_API_SECRET", "FAKE")
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_PERMISSIONS_CONFIRMED", "true")
    calls: list[str] = []
    monkeypatch.setattr("autofund.live.client.BitsoProductionLiveTransport.request",
                        lambda *args, **kwargs: calls.append("EXCHANGE"))
    journal = tmp_path / "live.jsonl"
    first = ProductionAutonomousRunner(journal)
    app = AutoFundOrchestrator(tmp_path / "artifacts", first)
    second = ProductionAutonomousRunner(journal)  # same journal path, second owner
    other = AutoFundOrchestrator(tmp_path / "artifacts", second)
    try:
        app.startup()
        before = len(calls)
        other.startup()
        view = other.snapshot()
        assert view["app_state"] is AppState.HALTED
        assert view["last_error"] == "EXECUTION_JOURNAL_IN_USE"
        assert view["production_preflight"]["blockers"] == ["EXECUTION_JOURNAL_IN_USE"]
        assert view["production_preflight"]["guidance"]
        assert view["runtime"]["status"] == "STOPPED"
        # The blocked instance never reached the exchange at all.
        assert len(calls) == before
    finally:
        if first.journal is not None:
            first.journal.close()

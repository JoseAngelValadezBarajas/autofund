"""Post-session semantics and scanner-to-Production isolation, end to end."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from autofund.mvp import scanner as sc
from autofund.mvp.api import create_mvp_app
from autofund.mvp.champion import ENTRY_CONDITION_NOT_MET
from autofund.mvp.observability import MvpObservability
from autofund.mvp.orchestrator import (
    STOP_KILL_SWITCH,
    STOP_MAX_DURATION,
    STOP_OPERATOR,
    AutoFundOrchestrator,
    DemoAutonomousRunner,
    SessionConfig,
)
from autofund.observer.models import Level, OrderBookSnapshot

NOW = datetime.now(UTC)


def depth(price="999900", *, at=None, sequence=7):
    bid = D(price)
    return OrderBookSnapshot("btc_mxn", at or NOW, sequence,
                             (Level(bid, D("1")),), (Level(bid + D("100"), D("1")),))


def promoted(projection: MvpObservability, now=NOW) -> MvpObservability:
    """Drive a projection through a complete no-signal evaluation path."""
    projection.begin("mvp-1", now)
    projection.observe_market(depth(at=now), latency_ms=4.0)
    for event, extra in (("CANDLE_CLOSED", {}),
                         ("STRATEGY_EVALUATED", {"eligible": True, "reason_code": ENTRY_CONDITION_NOT_MET,
                                                 "distance_to_signal": "0.002"}),
                         ("NO_SIGNAL", {"reason_code": ENTRY_CONDITION_NOT_MET})):
        projection.observe_checkpoint({"timestamp_utc": now.isoformat(), "event": event,
                                       "component": "strategy", "level": "INFO",
                                       "message": event, **extra})
    return projection


# ------------------------------------------------------------- post-session
def test_stopped_session_reports_inactive_runtime_and_market_stream():
    projection = promoted(MvpObservability())
    before = projection.runtime(now=NOW)
    assert before["runtime_state"] == "RUNNING" and before["market_stream"] == "ACTIVE"
    projection.end("STOPPED")
    after = projection.runtime(now=NOW + timedelta(minutes=10))
    assert after["status"] == "STOPPED"
    assert after["heartbeat"] == "INACTIVE"
    assert after["runtime_state"] == "INACTIVE"
    assert after["market_stream"] == "INACTIVE"
    projection.close()


def test_stopped_session_retains_last_known_market_quality_and_is_not_stale():
    projection = promoted(MvpObservability())
    projection.end("STOPPED")
    later = projection.runtime(now=NOW + timedelta(hours=1))
    # Deliberate inactivity is not an operational incident.
    assert later["quality"] == "VALID"
    assert later["last_known_quality"] == "VALID"
    assert later["last_known_market_at"] is not None
    assert later["heartbeat"] != "STALE"
    assert projection.health(now=NOW + timedelta(hours=1))["degraded"] is False
    projection.close()


def test_running_session_still_uses_active_heartbeat_semantics():
    projection = promoted(MvpObservability())
    stale = projection.runtime(now=NOW + timedelta(seconds=30))
    assert stale["runtime_state"] == "RUNNING"
    projection.close()


def test_no_signal_path_marks_downstream_stages_not_applicable():
    projection = promoted(MvpObservability())
    projection.end("STOPPED")
    stages = {row["stage"]: row["status"] for row in projection.pipeline()}
    assert stages["MARKET"] == "PASS" and stages["CANDLE"] == "PASS"
    assert stages["STRATEGY"] == "PASS" and stages["SIGNAL"] == "NONE"
    for stage in ("CAPITAL", "RISK", "FINAL_MARKET_CHECK", "ORDER", "FILL", "RECONCILIATION", "LEDGER"):
        assert stages[stage] == "NOT_APPLICABLE", stage
    assert "UNKNOWN" not in stages.values()
    projection.close()


def test_active_pipeline_still_shows_waiting_for_unreached_stages():
    projection = MvpObservability()
    projection.begin("mvp-1", NOW)
    projection.observe_market(depth(at=NOW), latency_ms=1.0)
    stages = {row["stage"]: row["status"] for row in projection.pipeline()}
    # While RUNNING, unreached stages may still be reached.
    assert stages["ORDER"] == "WAITING"
    projection.close()


def test_exercised_stages_are_never_reported_as_not_applicable():
    projection = MvpObservability()
    projection.begin("mvp-1", NOW)
    for event in ("CANDLE_CLOSED", "STRATEGY_EVALUATED", "SIGNAL_GENERATED", "CAPITAL_CHECK_PASS",
                  "RISK_CHECK_PASS", "FINAL_MARKET_CHECK_PASS", "ORDER_INTENT_CREATED", "FILL"):
        projection.observe_checkpoint({"timestamp_utc": NOW.isoformat(), "event": event,
                                       "component": "execution", "level": "INFO", "message": event})
    projection.end("STOPPED")
    stages = {row["stage"]: row["status"] for row in projection.pipeline()}
    assert stages["CAPITAL"] == "PASS" and stages["FILL"] == "PASS"
    assert stages["RECONCILIATION"] == "NOT_APPLICABLE"
    projection.close()


# ------------------------------------------------------------ orchestrator
def test_max_duration_session_freezes_elapsed_and_records_stop_reason(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    app.start(SessionConfig(max_session_duration_seconds=60), "START AUTOFUND REAL 50")
    app.snapshot()
    app._requested_stop = STOP_MAX_DURATION
    app.stop(STOP_MAX_DURATION)
    first = app.snapshot()["session"]
    assert first["frozen"] is True
    assert first["stop_reason"] == STOP_MAX_DURATION
    assert first["ended_at"] is not None
    assert first["actual_runtime_seconds"] == first["elapsed_seconds"]
    frozen = first["elapsed_seconds"]
    import time
    time.sleep(1.1)
    assert app.snapshot()["session"]["elapsed_seconds"] == frozen


def test_operator_stop_and_kill_have_distinct_canonical_reasons(tmp_path):
    for action, expected in ((lambda a: a.stop("operator"), STOP_OPERATOR),
                             (lambda a: a.kill("emergency"), STOP_KILL_SWITCH)):
        app = AutoFundOrchestrator(tmp_path / expected, DemoAutonomousRunner(), demo=True)
        app.startup()
        app.start(SessionConfig(), "START AUTOFUND REAL 50")
        action(app)
        assert app.snapshot()["session"]["stop_reason"] == expected


def test_completed_session_artifacts_are_written(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    app.start(SessionConfig(), "START AUTOFUND REAL 50")
    app.snapshot()
    app.stop("operator")
    folder = tmp_path / app.session_id
    for name in ("report.json", "handoff.json", "checkpoint_summary.json"):
        assert (folder / name).is_file()
    import json
    report = json.loads((folder / "report.json").read_text())
    assert report["time"]["stop_reason"] == STOP_OPERATOR
    assert report["identity"]["market"] == "btc_mxn"


# ------------------------------------------------------- scanner isolation
class Limits:
    def __init__(self, book, minimum_value="10"):
        self.book, self.minimum_value, self.minimum_amount = book, D(minimum_value), D("0.000001")


class Level2:
    def __init__(self, price, amount):
        self.price, self.amount = D(price), D(amount)


class Tick:
    def __init__(self, book, bid, ask, high, low):
        self.book, self.bid, self.ask = book, D(bid), D(ask)
        self.high, self.low, self.volume, self.vwap = D(high), D(low), D("100"), D("100")


class Dep:
    def __init__(self, book, bid, ask, amount):
        self.book, self.timestamp, self.sequence = book, NOW, 1
        self.bids, self.asks = (Level2(bid, amount),), (Level2(ask, amount),)

    @property
    def best_bid(self):
        return self.bids[0].price

    @property
    def best_ask(self):
        return self.asks[0].price

    @property
    def spread_bps(self):
        mid = (self.best_ask + self.best_bid) / D("2")
        return (self.best_ask - self.best_bid) / mid * D("10000")


class Fee:
    rate = D("0.0078")


class FakeSource:
    def __init__(self):
        self.calls = []

    def available_books(self):
        self.calls.append("GET available_books")
        return (Limits("btc_mxn"), Limits("usd_mxn"))

    def ticker(self, book):
        self.calls.append(f"GET ticker {book}")
        return Tick(book, "18", "18.001", "18.1", "17.9") if book == "usd_mxn" \
            else Tick(book, "1000000", "1000010", "1010000", "990000")

    def order_book(self, book):
        self.calls.append(f"GET order_book {book}")
        return Dep(book, "18", "18.001", "100") if book == "usd_mxn" \
            else Dep(book, "1000000", "1000010", "0.0002")

    def fee_schedule(self, book):
        self.calls.append(f"GET fees {book}")
        return Fee()


def test_scanner_never_changes_the_live_market_or_champion(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    champion_before = app.adaptive.champion.fingerprint
    source = FakeSource()
    app.start_scanner(sc.MarketScanner(source), interval_seconds=3600)
    try:
        app.scan_markets(now=NOW)
        evidence = app.scanner_evidence()
        assert evidence["live_market"] == "btc_mxn"
        assert evidence["execution_path_to_production"] == "NOT_PRESENT"
        assert app.adaptive.champion.fingerprint == champion_before
        assert app.snapshot()["production_preflight"]["label"] != ""
        assert all(call.startswith("GET ") for call in source.calls)
    finally:
        app.stop_scanner()


def test_scanner_failure_does_not_halt_a_running_session(tmp_path):
    class Broken:
        def available_books(self):
            raise RuntimeError("scanner down")

    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    app.start(SessionConfig(), "START AUTOFUND REAL 50")
    app.start_scanner(sc.MarketScanner(Broken()), interval_seconds=3600)
    try:
        app.scan_markets(now=NOW)
        view = app.snapshot()
        # Trading state is untouched by a research failure.
        assert view["app_state"] == "RUNNING"
        assert view["auto_execution"] is True
        assert app.scanner_evidence()["degraded"] is True
        assert app.scanner_evidence()["error"] == "scanner down"
    finally:
        app.stop_scanner()
        app.stop("operator")
    assert app.snapshot()["app_state"] == "STOPPED"


def test_scanner_scan_does_not_mutate_the_ledger(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    source = FakeSource()
    app.start_scanner(sc.MarketScanner(source), interval_seconds=3600)
    try:
        before = app.runner.snapshot()
        app.scan_markets(now=NOW)
        after = app.runner.snapshot()
        assert before["cash_mxn"] == after["cash_mxn"]
        assert before["orders"] == after["orders"]
        assert before["fills"] == after["fills"]
    finally:
        app.stop_scanner()


def test_scanner_api_is_get_only_and_read_only(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    http = TestClient(create_mvp_app(app))
    response = http.get("/api/v1/mvp/scanner")
    assert response.status_code == 200
    body = response.json()
    assert body["read_only"] is True
    assert body["live_market"] == "btc_mxn"
    learning = http.get("/api/v1/mvp/learning")
    assert learning.status_code == 200
    assert learning.json()["promotion"] == "MANUAL"
    assert learning.json()["auto_promotion"] == "DISABLED"


def test_snapshot_exposes_learning_and_scanner_namespaces(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    view = app.snapshot()
    assert "learning" in view and "scanner" in view
    assert view["learning"]["champion"]["profile_id"] == "mean-reversion-safe"
    assert view["learning"]["auto_promotion"] == "DISABLED"
    assert view["scanner"]["execution_path_to_production"] == "NOT_PRESENT"


@pytest.mark.parametrize("attribute", ["authorized_capital_mxn", "max_deployment_mxn", "single_order_cap_mxn"])
def test_adaptive_engine_cannot_touch_hard_safety_settings(tmp_path, attribute):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    before = app.snapshot()[attribute]
    app.adaptive.observe_session(rows=[], metrics={})
    assert app.snapshot()[attribute] == before
    # The engine exposes no setter for any hard bound.
    for name in ("set_capital", "set_cap", "set_loss_limit", "promote", "disable_kill_switch"):
        if hasattr(app.adaptive, name):
            with pytest.raises((PermissionError, AttributeError, TypeError)):
                getattr(app.adaptive, name)(null_cap=None) if name == "promote" else getattr(app.adaptive, name)()

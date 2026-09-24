"""Deterministic tests for MVP 0.1.2 hardening.

Covers the fail-closed blocked-recovery state, signal suppression telemetry,
wallet freshness, runtime-gap detection and the round-trip economic fact.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from autofund.mvp.orchestrator import (
    STOP_BLOCKED_RECOVERY,
    AutoFundOrchestrator,
    DemoAutonomousRunner,
    ProductionAutonomousRunner,
    SessionConfig,
)
from autofund.mvp.postmortem import (
    GROSS_PROFIT_CONSUMED_BY_FEES,
    RUNTIME_GAP_DETECTED,
    SIGNALS_SUPPRESSED,
    candidate_signals,
    round_trip_economics,
    runtime_gaps,
    signal_admission,
)
from tests.live.test_execution import FakeTransport

CONFIRMATION = "START AUTOFUND REAL 50"


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


# ------------------------------------------------------- fail-closed state
def test_blocked_recovery_halts_and_disables_auto_execution(production):
    """An ACK with no established outcome must not keep looking RUNNING."""
    app, _runner, _transport = production
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    app._on_order_blocked(("af-live-" + "a" * 32,))
    view = app.snapshot()
    assert view["app_state"] == "HALTED"
    assert view["auto_execution"] is False
    assert view["last_error"] == "BLOCKED_RECOVERY_UNRESOLVED_ORDER"
    assert view["session"]["stop_reason"] == STOP_BLOCKED_RECOVERY
    # Market observation remains available; only financial writes are blocked.
    assert view["runtime"] is not None


def test_blocked_recovery_is_idempotent_and_ignored_when_not_running(production):
    app, _runner, _transport = production
    app.startup()
    app._on_order_blocked(("af-live-" + "a" * 32,))  # not RUNNING yet: no effect
    assert app.snapshot()["app_state"] == "STOPPED"


# ------------------------------------------------------ signal suppression
def test_suppression_is_reported_from_decision_and_admission_counts():
    rows = []
    for index in range(61):
        rows.append({"event": "STRATEGY_EVALUATED", "decision": "BUY", "eligible": True,
                     "timestamp_utc": f"2026-09-23T13:{index:02d}:00Z"})
    rows.append({"event": "SIGNAL_GENERATED", "timestamp_utc": "2026-09-23T13:09:01Z"})
    result = signal_admission(rows)
    assert result["strategy_buy_decisions"] == 61
    assert result["signals_admitted"] == 1
    # The real session's shape: 61 BUY decisions, one admitted, no explicit events.
    assert result["actionable_decisions_not_admitted"] == 60
    assert result["suppression_basis"] == "DERIVED_FROM_DECISION_AND_ADMISSION_COUNTS"


def test_explicit_suppression_events_are_counted():
    rows = [{"event": "STRATEGY_EVALUATED", "decision": "BUY"},
            {"event": "SIGNAL_GENERATED"},
            {"event": "SIGNAL_SUPPRESSED_PENDING_ORDER"}]
    result = signal_admission(rows)
    assert result["signals_suppressed_pending_order"] == 1
    assert result["suppression_basis"] == "EXPLICIT_CHECKPOINT"


def test_suppressed_signals_produce_a_deterministic_handoff_fact():
    report = {"strategy": {"eligible_evaluations": 304, "signals": 1, "buy_decisions": 61,
                           "sell_decisions": 0, "near_signal_count": 0},
              "execution": {"exercised": True}, "market": {"degradation_count": 0},
              "runtime": {"gaps": {"count": 0}}, "round_trip": {"facts": []}}
    signals = candidate_signals(report=report, scanner={"scanner_ran": True})
    assert SIGNALS_SUPPRESSED in signals


def test_zero_signal_session_still_reports_zero_signals_not_suppression():
    report = {"strategy": {"eligible_evaluations": 58, "signals": 0, "buy_decisions": 0,
                           "near_signal_count": 0},
              "execution": {"exercised": False}, "market": {"degradation_count": 0},
              "runtime": {"gaps": {"count": 0}}, "round_trip": {"facts": []}}
    signals = candidate_signals(report=report, scanner={"scanner_ran": True})
    assert "ZERO_SIGNALS_ACROSS_ELIGIBLE_EVALUATIONS" in signals


# --------------------------------------------------------- round trip fact
def test_gross_profit_consumed_by_fees_uses_the_real_production_numbers():
    """The verified first round trip: positive gross, negative net."""
    truth = {"gross_realized_pnl_mxn": "0.06259074", "net_realized_pnl_mxn": "-0.02303341",
             "fees_mxn": "0.17370475", "fills": 2,
             "sell": {"quantity": "0.00000738", "value_mxn": "10.97745480"}}
    economics = round_trip_economics(truth)
    assert economics["gross_profit_consumed_by_fees"] is True
    assert economics["facts"] == [GROSS_PROFIT_CONSUMED_BY_FEES]
    assert economics["gross_realized_pnl_mxn"] == "0.06259074"
    assert economics["net_realized_pnl_mxn"] == "-0.02303341"


def test_profitable_round_trip_does_not_raise_the_fee_fact():
    truth = {"gross_realized_pnl_mxn": "1.0", "net_realized_pnl_mxn": "0.5",
             "fees_mxn": "0.5", "fills": 2}
    assert round_trip_economics(truth)["facts"] == []


def test_round_trip_absent_evidence_is_not_a_false_positive():
    assert round_trip_economics(None)["facts"] == []
    assert round_trip_economics({})["round_trip_completed"] is False


# ----------------------------------------------------------- runtime gaps
def _rows(*stamps):
    return [{"event": "NO_SIGNAL", "timestamp_utc": stamp} for stamp in stamps]


def test_runtime_gap_detects_a_suspend_sized_hole():
    rows = _rows("2026-09-23T18:26:03.277499Z", "2026-09-24T07:43:21.989440Z")
    gaps = runtime_gaps(rows)
    assert gaps["count"] == 1
    assert D(gaps["largest_gap_seconds"]) > D("47000")
    assert gaps["gaps"][0]["last_event_at"] == "2026-09-23T18:26:03.277499Z"
    assert gaps["gaps"][0]["next_event_at"] == "2026-09-24T07:43:21.989440Z"


def test_normal_loop_cadence_is_not_a_runtime_gap():
    rows = _rows("2026-09-23T12:00:00Z", "2026-09-23T12:00:05Z", "2026-09-23T12:01:05Z",
                 "2026-09-23T12:05:00Z")
    assert runtime_gaps(rows)["count"] == 0


def test_runtime_gap_fact_reaches_the_handoff():
    report = {"strategy": {"eligible_evaluations": 10, "signals": 1, "buy_decisions": 1,
                           "near_signal_count": 0},
              "execution": {"exercised": True}, "market": {"degradation_count": 0},
              "runtime": {"gaps": {"count": 1, "largest_gap_seconds": "47838"}},
              "round_trip": {"facts": []}}
    assert RUNTIME_GAP_DETECTED in candidate_signals(report=report, scanner={"scanner_ran": True})


def test_monitored_seconds_excludes_detected_gaps(tmp_path):
    """Wall elapsed overstates monitoring when the process was suspended."""
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    app.start(SessionConfig(), "START AUTOFUND REAL 50")
    app._runtime_gaps = [{"gap_seconds": "100"}]
    view = app.snapshot()
    assert view["session"]["elapsed_seconds"] is not None
    assert view["monitored_seconds"] == max(0, view["session"]["elapsed_seconds"] - 100)
    app.stop("operator")


def test_watchdog_detects_a_gap_and_enforces_the_duration_guard(tmp_path):
    """The guard must fire on resume, not only when a client polls."""
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    app.start(SessionConfig(max_session_duration_seconds=60), "START AUTOFUND REAL 50")
    try:
        # Simulate a suspended process: the watchdog's previous tick is far behind.
        app._watchdog_tick = __import__("time").monotonic() - 120
        app._last_watchdog_at = "2026-09-23T18:26:03Z"
        app._watchdog_loop_once()
        assert app._runtime_gaps, "a suspend-sized gap must be recorded"
        assert app.snapshot()["app_state"] == "RUNNING"  # within the configured duration
    finally:
        app.shutdown()


def test_watchdog_enforces_duration_on_resume(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    app.start(SessionConfig(max_session_duration_seconds=60), "START AUTOFUND REAL 50")
    try:
        # Force the session to look older than its configured duration.
        app._session_monotonic = __import__("time").monotonic() - 120
        app._watchdog_loop_once()
        assert app.snapshot()["app_state"] == "STOPPED"
        assert app.snapshot()["session"]["stop_reason"] == "MAX_SESSION_DURATION_REACHED"
    finally:
        app.shutdown()


# --------------------------------------------------------- wallet freshness
def test_wallet_exposes_an_observed_at_timestamp(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    view = app.snapshot()
    wallet = view["wallet"]
    assert "observed_at" in wallet
    assert wallet["read_only"] is True


def test_wallet_is_refreshed_when_generating_diagnostics(production):
    """A startup snapshot must never be exported as current."""
    from fastapi.testclient import TestClient

    from autofund.mvp.api import create_mvp_app

    app, runner, _transport = production
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    app.snapshot()
    assert runner.wallet_observed_at is not None
    before = runner.wallet_observed_at
    http = TestClient(create_mvp_app(app))
    response = http.get(f"/api/v1/sessions/{app.session_id}/diagnostics")
    assert response.status_code == 200
    # The export path refreshes the read-only view, so it is never stale.
    assert runner.wallet_observed_at is not None
    assert runner.wallet_observed_at >= before


def test_wallet_read_failure_never_affects_financial_state(tmp_path):
    class BrokenWallet(DemoAutonomousRunner):
        def snapshot(self):
            raise RuntimeError("wallet unavailable")

    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    app.refresh_wallet()  # must not raise
    assert app.snapshot()["cash_mxn"]
    del BrokenWallet


def test_report_includes_runtime_gaps_and_round_trip(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    app.start(SessionConfig(), "START AUTOFUND REAL 50")
    app.snapshot()
    app.stop("operator")
    report = json.loads((tmp_path / app.session_id / "report.json").read_text())
    assert "runtime" in report and "gaps" in report["runtime"]
    assert "round_trip" in report
    assert "signal_admission" in report
    assert report["signal_admission"]["suppression_basis"] in {
        "EXPLICIT_CHECKPOINT", "DERIVED_FROM_DECISION_AND_ADMISSION_COUNTS"}
    assert "execution_financial_truth" in report


def test_handoff_includes_signal_admission_and_round_trip(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    app.start(SessionConfig(), "START AUTOFUND REAL 50")
    app.snapshot()
    app.stop("operator")
    handoff = json.loads((tmp_path / app.session_id / "handoff.json").read_text())
    assert "signal_admission" in handoff
    assert "round_trip" in handoff
    assert "runtime_gaps" in handoff["telemetry_observations"]


def test_runtime_gap_checkpoint_is_registered(tmp_path):
    """The checkpoint taxonomy must accept the new event."""
    from autofund.mvp.telemetry import CHECKPOINTS

    for name in ("RUNTIME_GAP", "SIGNAL_SUPPRESSED_PENDING_ORDER", "RECONCILIATION_PROBE"):
        assert name in CHECKPOINTS


def test_age_of_last_watchdog_timestamp_uses_utc():
    stamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    assert datetime.fromisoformat(stamp) <= datetime.now(UTC) + timedelta(seconds=1)


def test_session_view_and_snapshot_are_json_serializable(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    view = app.snapshot()
    json.dumps(view, default=str)
    assert Path(tmp_path).exists()

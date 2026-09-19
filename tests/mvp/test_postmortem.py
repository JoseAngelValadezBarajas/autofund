"""Replay a sanitized session fixture into telemetry, then verify the artifacts.

The fixture mirrors the shape of the first real Production session: ~60 closed
candles, eligible evaluations, zero signals, zero orders and a normal duration
stop. It contains no credentials or account data.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from autofund.mvp.adaptive import INSUFFICIENT_EVIDENCE, AdaptiveEngine
from autofund.mvp.champion import (
    ENTRY_CONDITION_NOT_MET,
    INSUFFICIENT_HISTORY,
    NO_CLOSED_CANDLE,
    evaluate_champion,
)
from autofund.mvp.orchestrator import STOP_MAX_DURATION
from autofund.mvp.postmortem import (
    EXECUTION_NOT_EXERCISED,
    ZERO_SIGNALS,
    build_handoff,
    build_report,
    candidate_signals,
)
from autofund.mvp.telemetry import SessionTelemetry

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures" / "mvp" / "first_production_session.json").read_text())
BASE = datetime(2026, 9, 18, 21, 0, tzinfo=UTC)


def build_rows() -> list[dict[str, Any]]:
    """Deterministic telemetry rows with the fixture's decision distribution."""
    counts = FIXTURE["no_signal_breakdown"]
    rows: list[dict[str, Any]] = []

    def add(event: str, component: str, message: str, offset: int, **extra: Any) -> None:
        rows.append({"timestamp_utc": (BASE + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z"),
                     "event": event, "component": component, "level": extra.pop("level", "INFO"),
                     "message": message, "checkpoint_id": f"CP-{len(rows) + 1:06d}", **extra})

    add("SESSION_START_REQUESTED", "control", "Operator authorized bounded real session", 0)
    add("SESSION_STARTED", "orchestrator", "Automatic execution enabled", 1)
    add("MARKET_CONNECTED", "market", "BTC/MXN market stream ready", 2)
    for index in range(counts[NO_CLOSED_CANDLE]):
        add("NO_SIGNAL", "strategy", "No closed candle observed yet", 3 + index,
            reason_code=NO_CLOSED_CANDLE, eligible=False)
    for index in range(counts[INSUFFICIENT_HISTORY]):
        add("NO_SIGNAL", "strategy", "Insufficient closed-candle evidence", 5 + index,
            reason_code=INSUFFICIENT_HISTORY, eligible=False)
    offset = 10
    for index in range(FIXTURE["counts"]["closed_candles"]):
        add("CANDLE_CLOSED", "market", "BTC/MXN closed candle accepted", offset, spread_bps="8.5")
        offset += 50
        if index >= 2:
            distance = Decimal("0.0031") + Decimal(index) * Decimal("0.0003")
            add("STRATEGY_EVALUATED", "strategy", "Champion chose no trade", offset,
                reason_code=ENTRY_CONDITION_NOT_MET, eligible=True,
                distance_to_signal=str(distance), near_signal=False,
                market_regime="NORMAL", strategy_fingerprint="fp-champion", profile_id="mean-reversion-safe")
            add("NO_SIGNAL", "strategy", "Champion chose no trade", offset + 1,
                reason_code=ENTRY_CONDITION_NOT_MET, eligible=True, distance_to_signal=str(distance))
    add("SESSION_STOP_REQUESTED", "control", "maximum session duration reached", 3600,
        stop_reason=STOP_MAX_DURATION)
    add("SESSION_STOPPED", "orchestrator", "Session finalized", 3601)
    return rows


def build_telemetry(tmp_path: Path) -> SessionTelemetry:
    telemetry = SessionTelemetry(tmp_path, FIXTURE["session_id"])
    telemetry.rows = build_rows()
    return telemetry


def finalize(telemetry: SessionTelemetry, tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    telemetry.finalize(result="STOPPED", stop_reason=STOP_MAX_DURATION,
                       identity={"market": "btc_mxn", "profile_id": "mean-reversion-safe",
                                 "strategy_id": "mean_reversion", "champion_fingerprint": "fp-champion"},
                       config={"max_session_duration_seconds": 3600, "max_orders_per_session": 10,
                               "max_session_loss_mxn": "10"},
                       time_facts={"actual_runtime_seconds": 3600, "configured_duration_seconds": 3600},
                       learning={"classification": INSUFFICIENT_EVIDENCE, "observations": []},
                       facts={"metrics": {"orders": 0, "fills": 0, "net_pnl_mxn": "0", "fees_mxn": "0"},
                              "initial_equity_mxn": "50", "final_equity_mxn": "50",
                              "unrealized_pnl_mxn": "0", "max_deployment_mxn": "0",
                              "execution_quality": {}, "halts": 0})
    folder = tmp_path / FIXTURE["session_id"]
    return (json.loads((folder / "report.json").read_text()),
            json.loads((folder / "handoff.json").read_text()),
            json.loads((folder / "checkpoint_summary.json").read_text()))


@pytest.fixture
def artifacts(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    return finalize(build_telemetry(tmp_path), tmp_path)


def test_report_captures_complete_lifecycle(artifacts):
    report, _, _ = artifacts
    assert report["schema"] == "autofund.session-report.v2"
    assert report["time"]["stop_reason"] == STOP_MAX_DURATION
    assert report["time"]["actual_runtime_seconds"] == 3600
    assert report["time"]["configured_duration_seconds"] == 3600
    assert report["identity"]["market"] == "btc_mxn"
    assert report["market"]["candles"] == FIXTURE["counts"]["closed_candles"]
    assert report["strategy"]["eligible_evaluations"] == FIXTURE["eligible_evaluations"]
    assert report["strategy"]["signals"] == 0
    assert report["strategy"]["reason_distribution"][ENTRY_CONDITION_NOT_MET] > 0
    assert report["execution"]["exercised"] is False
    assert report["operations"]["halts"] == 0
    # Latency is reported as a group summary, never as a fabricated measurement.
    assert "duration_ms" in report["market"] or report["market"]["latency"] == {}


def test_report_and_handoff_are_deterministic(artifacts):
    _report_a, handoff_a, summary_a = artifacts
    rows = build_rows()
    scanner = {"scanner_ran": True, "degraded": False, "eligible_count": 2,
               "candidates": [], "rejections": []}
    report_b = build_report(rows, identity={"market": "btc_mxn"}, time_facts={}, config={},
                            facts={"metrics": {}}, scanner=scanner)
    handoff_b = build_handoff(rows, report=report_b, result="STOPPED", stop_reason=STOP_MAX_DURATION,
                              scanner=scanner, learning={})
    # Same rows always produce the same aggregates.
    assert json.dumps(report_b, sort_keys=True) == json.dumps(
        build_report(build_rows(), identity={"market": "btc_mxn"}, time_facts={}, config={},
                     facts={"metrics": {}}, scanner=scanner), sort_keys=True)
    assert handoff_b["strategy_observations"]["reason_distribution"] == \
        handoff_a["strategy_observations"]["reason_distribution"]
    assert summary_a["stop_reason"] == STOP_MAX_DURATION


def test_zero_signal_session_yields_deterministic_improvement_signals(artifacts):
    _report, handoff, _ = artifacts
    signals = handoff["candidate_improvement_signals"]
    assert ZERO_SIGNALS in signals
    assert EXECUTION_NOT_EXERCISED in signals
    assert handoff["strategy_observations"]["conclusion"] == "NO_ACTIONABLE_SIGNAL"
    assert handoff["execution_observations"]["exercised"] is False
    # Deterministic facts only: no speculative natural-language advice.
    for signal in signals:
        assert signal == signal.upper()
        assert " " not in signal


def test_handoff_has_all_structured_sections(artifacts):
    _, handoff, _ = artifacts
    for section in ("what_happened", "stop_reason", "strategy_observations", "execution_observations",
                    "risk_observations", "market_observations", "telemetry_observations",
                    "learning_observations", "candidate_improvement_signals"):
        assert section in handoff


def test_checkpoint_summary_aggregates(artifacts):
    _, _, summary = artifacts
    assert summary["schema"] == "autofund.checkpoint-summary.v2"
    assert summary["counts_by_event"]["CANDLE_CLOSED"] == FIXTURE["counts"]["closed_candles"]
    assert summary["counts_by_component"]["strategy"] > 0
    assert summary["session_lifecycle"]["SESSION_STARTED"] == 1
    assert summary["execution_exercised"] is False
    assert summary["strategy"]["reason_distribution"][ENTRY_CONDITION_NOT_MET] > 0
    assert summary["learning"]["classification"] == INSUFFICIENT_EVIDENCE


def test_disabled_scanner_is_reported_without_corrupting_the_report(artifacts):
    report, handoff, _ = artifacts
    # Scanner failure is isolated: the financial report stays complete.
    assert report["scanner"]["scanner_ran"] is False
    assert report["financial"]["net_pnl_mxn"] == "0"
    assert "MARKET_SCANNER_EVIDENCE_UNAVAILABLE" in handoff["candidate_improvement_signals"]


def test_insufficient_evidence_does_not_invent_a_challenger(artifacts):
    _, handoff, _ = artifacts
    engine = AdaptiveEngine()
    observation = engine.observe_session(rows=build_rows(), metrics={"net_pnl_mxn": "0", "fees_mxn": "0"},
                                         stop_reason=STOP_MAX_DURATION)
    assert observation["classification"] == INSUFFICIENT_EVIDENCE
    assert observation["champion_changed"] is False
    assert observation["auto_promotion"] is False
    assert observation["challengers_created"] == 0
    assert engine.challengers == []
    assert engine.champion.profile_id == "mean-reversion-safe"
    view = engine.learning_view()
    assert view["sessions_observed"] == 1
    assert view["eligible_evaluations"] == FIXTURE["eligible_evaluations"]
    assert view["auto_promotion"] == "DISABLED"
    assert view["promotion"] == "MANUAL"
    assert view["market_promotion"] == "DISABLED"
    # The learning observation is visible in the handoff namespace.
    assert handoff["learning_observations"]


def test_learning_view_records_reason_distribution_and_distance(artifacts):
    engine = AdaptiveEngine()
    engine.observe_session(rows=build_rows(), metrics={}, stop_reason=STOP_MAX_DURATION)
    view = engine.learning_view()
    assert view["reason_distribution"][ENTRY_CONDITION_NOT_MET] > 0
    assert view["distance_to_signal_min"] is not None
    assert view["challenger_count"] == 0
    assert view["observations"][0]["observations"]


def test_candidate_signals_are_pure_functions_of_report_and_scanner():
    report = {"strategy": {"eligible_evaluations": 10, "signals": 0, "near_signal_count": 0},
              "execution": {"exercised": False}, "market": {"degradation_count": 0}}
    scanner = {"scanner_ran": True}
    assert candidate_signals(report=report, scanner=scanner) == [ZERO_SIGNALS, EXECUTION_NOT_EXERCISED]


def test_champion_evidence_is_deterministic_and_uses_real_inputs():
    closes = (Decimal("1000000"), Decimal("1000000"), Decimal("1000000"))
    first = evaluate_champion(market="btc_mxn", closes=closes)
    second = evaluate_champion(market="btc_mxn", closes=closes)
    assert first.telemetry() == second.telemetry()
    assert first.reason_code == ENTRY_CONDITION_NOT_MET
    assert first.eligible is True
    # Evidence reports the actual inputs the decision used.
    assert first.features["mean_mxn"] == "1000000"
    assert first.thresholds["entry_threshold"] == "0.001"
    assert Decimal(first.features["entry_boundary_mxn"]) == Decimal("999000.000")

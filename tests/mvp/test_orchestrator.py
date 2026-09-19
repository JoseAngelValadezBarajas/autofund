import json
from decimal import Decimal

import pytest

from autofund.live.models import LiveError
from autofund.mvp.adaptive import AdaptiveEngine, MarketRegime
from autofund.mvp.orchestrator import (
    AppState,
    AutoFundOrchestrator,
    DemoAutonomousRunner,
    SessionConfig,
)


def ready(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    assert app.state is AppState.STOPPED and not app.auto_execution
    return app


def test_state_machine_start_stop_and_restart_defaults_safe(tmp_path):
    app = ready(tmp_path)
    app.start(SessionConfig(), "START AUTOFUND REAL 50")
    assert app.state is AppState.RUNNING and app.auto_execution
    app.stop()
    assert app.state is AppState.STOPPED and not app.auto_execution
    restarted = ready(tmp_path)
    assert restarted.state is AppState.STOPPED and not restarted.auto_execution


def test_invalid_transitions_and_strong_confirmation(tmp_path):
    app = ready(tmp_path)
    with pytest.raises(LiveError):
        app.stop()
    with pytest.raises(LiveError):
        app.start(SessionConfig(), "OK")
    app.start(SessionConfig(), "START AUTOFUND REAL 50")
    app.kill()
    assert app.state is AppState.HALTED and not app.auto_execution
    with pytest.raises(LiveError):
        app.start(SessionConfig(), "START AUTOFUND REAL 50")


def test_loss_limit_halts_and_prevents_session_writes(tmp_path):
    app = ready(tmp_path)
    app.start(SessionConfig(Decimal("2")), "START AUTOFUND REAL 50")
    app.observe_equity(Decimal("48"))
    assert app.state is AppState.HALTED and not app.auto_execution
    assert any(row["event"] == "LOSS_LIMIT_HIT" for row in app.telemetry.rows)


def test_telemetry_is_ordered_correlated_and_generates_handoff(tmp_path):
    app = ready(tmp_path)
    app.start(SessionConfig(), "START AUTOFUND REAL 50")
    app.snapshot()
    app.snapshot()
    app.stop()
    rows = app.telemetry.rows
    assert [r["checkpoint_sequence"] for r in rows] == list(range(1, len(rows) + 1))
    assert any(r["correlation_id"] == "demo-trade-1" for r in rows)
    folder = tmp_path / app.session_id
    assert (folder / "checkpoint_summary.json").is_file()
    assert (folder / "handoff.json").is_file()
    assert "api_secret" not in (folder / "telemetry.jsonl").read_text()
    assert json.loads((folder / "handoff.json").read_text())["schema"] == "autofund.handoff.v2"


@pytest.mark.parametrize("loss", [Decimal("0"), Decimal("50.01")])
def test_hard_bounds_reject_invalid_loss(loss):
    with pytest.raises(ValueError):
        SessionConfig(loss)


def test_adaptation_is_certified_or_do_not_trade_and_never_promotes():
    engine = AdaptiveEngine()
    assert engine.select(MarketRegime.NORMAL).certification_status == "CERTIFIED"
    assert engine.select(MarketRegime.HIGH_SPREAD) is None
    challenger = engine.add_challenger({"window": 5}, {"replay": "PASS"})
    assert challenger["status"] == "RESEARCH_ONLY"
    with pytest.raises(PermissionError):
        engine.promote(challenger)

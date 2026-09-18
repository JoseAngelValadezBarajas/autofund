import asyncio
from pathlib import Path

from autofund.dashboard.api import create_app
from autofund.dashboard.service import DashboardDataProvider


def test_sse_envelope_contains_runtime_and_bounded_activity(monkeypatch):
    provider = DashboardDataProvider(Path("unused"), demo=True, demo_live=True)
    provider.demo_runtime.started -= 10
    app = create_app(provider)
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/v1/stream")
    class Request:
        calls = 0
        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 1
    async def no_wait(_): pass
    monkeypatch.setattr(asyncio, "sleep", no_wait)
    async def consume():
        response = await endpoint(Request(), provider)
        return [message async for message in response.body_iterator]
    messages = asyncio.run(consume())
    assert len(messages) == 1
    assert "event: snapshot" in messages[0]
    assert '"demo_mode":true' in messages[0]
    assert '"runtime"' in messages[0] and "STRATEGY_EVALUATED" in messages[0]


def test_corrupt_runtime_retains_last_financial_snapshot(tmp_path):
    import json
    from datetime import UTC, datetime, timedelta

    from autofund.dashboard.live import project_runtime
    from autofund.dashboard.models import RuntimeView

    raw = json.loads((Path(__file__).parents[2] / "src/autofund/dashboard/fixtures/golden_dashboard.json").read_text())
    for point in raw["result"]["equity_curve"]:
        point["deployed"] = "0"
    at = datetime.now(UTC) - timedelta(seconds=20)
    raw["runtime"] = RuntimeView(session_id="live", status="RUNNING", started_at=at, last_state_update_at=at).model_dump(mode="json")
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(raw))
    provider = DashboardDataProvider(tmp_path, runtime_path=path)
    equity = provider.overview().current_shadow_equity_mxn
    assert provider.runtime().status == "DISCONNECTED"
    path.write_text("truncated")
    assert provider.overview().current_shadow_equity_mxn == equity
    assert provider.runtime().status == "DISCONNECTED"
    assert project_runtime(raw["runtime"]).status == "DISCONNECTED"

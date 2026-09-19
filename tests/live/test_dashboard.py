from pathlib import Path

from fastapi.testclient import TestClient

from autofund.dashboard.api import create_app
from autofund.dashboard.service import DashboardDataProvider
from autofund.live.observability import LivePublisher


def test_micro_demo_is_typed_private_and_get_only():
    provider = DashboardDataProvider(Path("unused"), demo=True, demo_micro_live=True)
    client = TestClient(create_app(provider))
    data = client.get("/api/v1/runtime").json()
    assert data["mode"] == "MICRO-LIVE" and data["demo_mode"] is True
    assert data["micro_live"]["auto_execution"] == "DISABLED"
    assert isinstance(data["micro_live"]["allocated_capital"], str)
    assert "balances" not in str(data).lower()
    assert client.get("/api/v1/health").json()["execution_mode"] == "MICRO-LIVE"
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/api/v1/runtime").status_code == 405
    assert provider.demo_micro.snapshot(4).micro_live.inventory_btc == "0.000005"


def test_publication_failure_is_isolated_and_secret_unknown_fields_stripped(tmp_path):
    provider = DashboardDataProvider(Path("unused"), demo=True, demo_micro_live=True)
    own = provider.runtime().micro_live.model_dump()
    own["balances"] = "PERSONAL_BALANCE"
    publisher = LivePublisher(tmp_path / "runtime.json")
    publisher.event("PREFLIGHT_PASS", own)
    assert publisher.path.exists()
    assert "PERSONAL_BALANCE" not in publisher.path.read_text()
    publisher.path = tmp_path
    publisher.event("HALTED", own)  # Failed publication cannot throw.

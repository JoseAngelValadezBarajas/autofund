from pathlib import Path

from fastapi.testclient import TestClient

from autofund.dashboard.api import create_app
from autofund.dashboard.service import DashboardDataProvider


def client() -> TestClient:
    return TestClient(create_app(DashboardDataProvider(Path(__file__).parents[2] / "artifacts" / "f4" / "live_session")))


def test_get_only_models_keep_money_as_strings() -> None:
    app = client()
    for path in ("health", "overview", "portfolio", "market", "candles", "order-book", "signals", "shadow-fills", "ledger", "risk", "quality", "sessions", "reports/current", "reports/weekly"):
        response = app.get("/api/v1/" + path)
        assert response.status_code == 200
        assert "api_secret" not in response.text.lower()
        assert "real_exchange_balances" not in response.text
    assert isinstance(app.get("/api/v1/overview").json()["current_shadow_equity_mxn"], str)


def test_mutations_and_invalid_pagination_rejected() -> None:
    app = client()
    for method in ("post", "put", "patch", "delete"):
        assert getattr(app, method)("/api/v1/overview").status_code == 405
    assert app.get("/api/v1/ledger?limit=201").status_code == 422


def test_equity_is_utc_strings_and_demo_contract() -> None:
    demo = TestClient(create_app(DashboardDataProvider(Path("unused"), demo=True)))
    assert demo.get("/api/v1/health").json()["demo_mode"] is True
    points = demo.get("/api/v1/equity").json()
    assert points and isinstance(points[0]["equity_mxn"], str)
    assert points[0]["timestamp"].endswith("Z")

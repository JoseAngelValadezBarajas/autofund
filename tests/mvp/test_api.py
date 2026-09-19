from fastapi.testclient import TestClient

from autofund.mvp.api import create_mvp_app
from autofund.mvp.orchestrator import AutoFundOrchestrator, DemoAutonomousRunner


def client(tmp_path):
    orchestrator = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    orchestrator.startup()
    return TestClient(create_mvp_app(orchestrator)), orchestrator


def auth(http):
    token = http.get("/api/v1/control/session").json()["control_token"]
    return {"Origin": "http://testserver", "X-AutoFund-Control-Token": token}


def test_control_api_requires_origin_token_confirmation_and_bounds(tmp_path):
    http, app = client(tmp_path)
    body = {"confirmation": "START AUTOFUND REAL 50", "max_session_loss_mxn": "10",
            "max_session_duration_seconds": 3600, "max_orders_per_session": 10}
    assert http.post("/api/v1/control/start", json=body).status_code == 403
    headers = auth(http)
    assert http.post("/api/v1/control/start", json={**body, "confirmation": "OK"}, headers=headers).status_code == 409
    assert http.post("/api/v1/control/start", json={**body, "max_session_loss_mxn": "51"}, headers=headers).status_code == 422
    assert http.post("/api/v1/control/start", json=body, headers=headers).status_code == 200
    assert app.auto_execution
    assert http.post("/api/v1/control/stop", json={}, headers=headers).status_code == 200


def test_kill_and_only_control_mutations_exist(tmp_path):
    http, app = client(tmp_path)
    headers = auth(http)
    body = {"confirmation": "START AUTOFUND REAL 50", "max_session_loss_mxn": "10",
            "max_session_duration_seconds": 3600, "max_orders_per_session": 10}
    http.post("/api/v1/control/start", json=body, headers=headers)
    assert http.post("/api/v1/control/kill", json={}, headers=headers).status_code == 200
    assert app.state == "HALTED"
    for path in ("buy", "sell", "order", "execute", "cancel", "withdraw", "transfer"):
        assert http.post("/api/v1/" + path, json={}, headers=headers).status_code == 404
    mutations = {(route.path, method) for route in http.app.routes for method in getattr(route, "methods", set())
                 if method in {"POST", "PUT", "PATCH", "DELETE"}}
    assert mutations == {(f"/api/v1/control/{name}", "POST") for name in ("start", "stop", "kill")}


def test_diagnostic_export_is_sanitized(tmp_path):
    http, app = client(tmp_path)
    headers = auth(http)
    body = {"confirmation": "START AUTOFUND REAL 50", "max_session_loss_mxn": "10",
            "max_session_duration_seconds": 3600, "max_orders_per_session": 10}
    http.post("/api/v1/control/start", json=body, headers=headers)
    http.get("/api/v1/mvp")
    http.post("/api/v1/control/stop", json={}, headers=headers)
    response = http.get(f"/api/v1/sessions/{app.session_id}/diagnostics")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert b"api_secret" not in response.content and b"Authorization" not in response.content

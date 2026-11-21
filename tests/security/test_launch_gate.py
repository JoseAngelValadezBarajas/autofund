"""Another program on this machine cannot use the dashboard without the launch link."""

import pytest
from fastapi.testclient import TestClient

from autofund.exchanges.alpaca.client import AlpacaCredentials
from autofund.lab.app import create_lab_app
from autofund.lab.service import LabService
from autofund.web_security import COOKIE_NAME, LaunchGate

ORIGIN = {"origin": "http://testserver"}


def lab_app(tmp_path, gate):
    service = LabService(root=tmp_path, credentials=lambda: AlpacaCredentials("PKX1", "secret1"),
                         data_client=lambda: None, paper_client=lambda: None)  # type: ignore[arg-type,return-value]
    return create_lab_app(service, gate=gate)


@pytest.fixture
def gate():
    return LaunchGate()


def test_without_the_cookie_nothing_works_not_even_reading_or_the_token(tmp_path, gate):
    with TestClient(lab_app(tmp_path, gate)) as client:
        for path in ("/api/v1/health", "/api/v1/control/session", "/api/v1/lab/status", "/api/v1/lab/journal"):
            response = client.get(path)
            assert response.status_code == 401 and response.json() == {"code": "LAUNCH_AUTH_REQUIRED"}
        page = client.get("/")
        assert page.status_code == 401 and "locked" in page.text
        assert gate.launch_key not in page.text
        # Refused by the outer Host/Origin/token layer (403) before the gate is even consulted.
        assert client.post("/api/v1/lab/session/kill", json={}, headers=ORIGIN).status_code in (401, 403)


def test_the_launch_link_becomes_a_strict_httponly_cookie_and_a_clean_redirect(tmp_path, gate):
    with TestClient(lab_app(tmp_path, gate)) as client:
        response = client.get(f"/?k={gate.launch_key}", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/"
        cookie = response.headers["set-cookie"]
        assert COOKIE_NAME in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict")
        assert gate.launch_key not in response.headers["location"]
        assert client.get("/api/v1/health").status_code == 200  # the jar now carries the session


def test_a_wrong_or_missing_key_is_refused(tmp_path, gate):
    with TestClient(lab_app(tmp_path, gate)) as client:
        for bad in ("", "wrong", gate.launch_key[:-1], gate.launch_key + "x"):
            assert client.get(f"/?k={bad}", follow_redirects=False).status_code == 401
        assert client.get("/api/v1/health").status_code == 401


def test_a_forged_cookie_is_refused(tmp_path, gate):
    with TestClient(lab_app(tmp_path, gate)) as client:
        client.cookies.set(COOKIE_NAME, "guess")
        assert client.get("/api/v1/health").status_code == 401
        client.cookies.set(COOKIE_NAME, gate.launch_key)  # the launch key is not the session value
        assert client.get("/api/v1/health").status_code == 401


def test_the_session_cookie_is_not_the_launch_key(gate):
    assert gate._cookie != gate.launch_key and len(gate._cookie) >= 40


def test_the_redirect_target_can_never_be_attacker_controlled(tmp_path, gate):
    with TestClient(lab_app(tmp_path, gate)) as client:
        for path in (f"//evil.example/?k={gate.launch_key}", f"/\\evil.example?k={gate.launch_key}"):
            response = client.get(path, follow_redirects=False)
            if response.status_code == 303:
                assert response.headers["location"] == "/"


def test_the_host_check_still_runs_first_so_rebinding_is_refused_before_the_gate(tmp_path, gate):
    with TestClient(lab_app(tmp_path, gate)) as client:
        response = client.get(f"/?k={gate.launch_key}", headers={"host": "evil.example"}, follow_redirects=False)
        assert response.status_code == 400 and COOKIE_NAME not in response.headers.get("set-cookie", "")


def test_security_headers_are_present_even_on_refusals(tmp_path, gate):
    with TestClient(lab_app(tmp_path, gate)) as client:
        refused = client.get("/api/v1/health")
        assert refused.status_code == 401 and refused.headers["X-Frame-Options"] == "DENY"


def test_no_gate_means_the_old_behavior_for_tests_and_demo(tmp_path):
    with TestClient(lab_app(tmp_path, None)) as client:
        assert client.get("/api/v1/health").status_code == 200


# ---- the MVP (real-money) application ----

def test_the_mvp_app_is_gated_when_given_a_gate_and_open_without_one(tmp_path):
    from autofund.mvp.api import create_mvp_app
    from autofund.mvp.orchestrator import AutoFundOrchestrator, DemoAutonomousRunner
    orchestrator = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    orchestrator.startup()
    gate = LaunchGate()
    try:
        with TestClient(create_mvp_app(orchestrator, None, gate=gate)) as client:
            assert client.get("/api/v1/mvp").status_code == 401
            assert client.get("/api/v1/control/session").status_code == 401
            assert client.post("/api/v1/control/stop", json={}, headers=ORIGIN).status_code in (401, 403)
            client.get(f"/?k={gate.launch_key}", follow_redirects=False)
            assert client.get("/api/v1/mvp").status_code == 200
        with TestClient(create_mvp_app(orchestrator, None)) as client:
            assert client.get("/api/v1/mvp").status_code == 200
    finally:
        orchestrator.shutdown()


def test_real_modes_get_a_gate_and_demo_does_not():
    import inspect

    from autofund.mvp import app as app_module
    source = inspect.getsource(app_module)
    assert "gate = None if demo else LaunchGate()" in source

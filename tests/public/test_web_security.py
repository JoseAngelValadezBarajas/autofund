"""Browser-facing security rules hold on every AutoFund web surface."""

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autofund.web_security import (
    CONTENT_SECURITY_POLICY,
    SECURITY_HEADERS,
    host_allowed,
    token_matches,
)

REPO = Path(__file__).parents[2]


# ---- the rules themselves ----

@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:8000", "localhost", "localhost:8765", "testserver"])
def test_loopback_hosts_are_allowed(host):
    assert host_allowed(host)


@pytest.mark.parametrize("host", [
    "", "evil.example", "evil.example:8000", "127.0.0.1.evil.example", "localhost.evil.example:80",
    "0.0.0.0", "192.168.1.5:8000", "attacker.com:127.0.0.1",
])
def test_any_other_host_is_refused(host):
    assert not host_allowed(host)


def test_token_comparison():
    assert token_matches("abc", "abc")
    assert not token_matches("abd", "abc") and not token_matches(None, "abc") and not token_matches("", "abc")


def test_csp_forbids_framing_and_base_and_foreign_forms():
    for directive in ("frame-ancestors 'none'", "base-uri 'none'", "form-action 'self'", "default-src 'self'",
                      "script-src 'self'"):
        assert directive in CONTENT_SECURITY_POLICY
    assert "unsafe-eval" not in CONTENT_SECURITY_POLICY and "script-src 'unsafe" not in CONTENT_SECURITY_POLICY
    assert SECURITY_HEADERS["X-Frame-Options"] == "DENY"


# ---- the lab ----

def _lab_client(tmp_path):
    from autofund.exchanges.alpaca.client import AlpacaCredentials
    from autofund.lab.app import create_lab_app
    from autofund.lab.service import LabService
    service = LabService(root=tmp_path, credentials=lambda: AlpacaCredentials("PKX1", "secret1"),
                         data_client=lambda: None, paper_client=lambda: None)  # type: ignore[arg-type,return-value]
    return TestClient(create_lab_app(service))


def test_lab_sets_every_security_header_and_refuses_foreign_hosts(tmp_path):
    with _lab_client(tmp_path) as client:
        response = client.get("/api/v1/health")
        for name, value in SECURITY_HEADERS.items():
            assert response.headers[name] == value
        assert client.get("/api/v1/health", headers={"host": "evil.example"}).status_code == 400
        assert client.get("/api/v1/health", headers={"host": "127.0.0.1.evil.example"}).status_code == 400


# ---- the shadow dashboard (it had no Host check before) ----

def _dashboard_client():
    from autofund.dashboard.api import create_app
    from autofund.dashboard.service import DashboardDataProvider
    return TestClient(create_app(DashboardDataProvider(REPO / "artifacts" / "nonexistent", demo=True)))


def test_dashboard_refuses_dns_rebinding_hosts_and_sets_headers():
    client = _dashboard_client()
    ok = client.get("/api/v1/health")
    assert ok.status_code == 200
    for name, value in SECURITY_HEADERS.items():
        assert ok.headers[name] == value
    for bad in ("evil.example", "evil.example:8000", "0.0.0.0:8000"):
        blocked = client.get("/api/v1/health", headers={"host": bad})
        assert blocked.status_code == 400 and blocked.json() == {"code": "INVALID_HOST"}


def test_dashboard_cli_only_binds_loopback():
    result = subprocess.run([sys.executable, "-m", "autofund.f4_cli", "dashboard", "--host", "0.0.0.0", "--demo"],
                            capture_output=True, text=True, cwd=REPO, timeout=60)
    assert result.returncode == 2 and "invalid choice" in result.stderr


# ---- the optimized-mode refusal ----

def test_refuses_to_run_under_python_O():
    result = subprocess.run([sys.executable, "-O", "-m", "autofund.f4_cli", "demo"], capture_output=True,
                            text=True, cwd=REPO, timeout=60)
    assert result.returncode == 2 and "refuses to run" in result.stdout


# ---- the MVP app ----

def test_mvp_app_uses_the_shared_rules():
    source = (REPO / "src" / "autofund" / "mvp" / "api.py").read_text(encoding="utf-8")
    assert "SECURITY_HEADERS" in source and "host_allowed" in source and "token_matches" in source
    assert "!= control_token" not in source

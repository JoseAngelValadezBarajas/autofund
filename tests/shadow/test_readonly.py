import ast
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest

from autofund.exchanges.bitso.auth import BitsoNonceProvider
from autofund.observer import parsing
from autofund.observer.client import (
    CONFIRMATION_MESSAGE,
    PRODUCTION_BASE_URL,
    BitsoProductionReadOnlyClient,
    ProductionCredentials,
    ReadOnlyBitsoTransport,
    ReadResponse,
)
from autofund.observer.errors import (
    AuthenticationUnavailable,
    ReadOnlyViolation,
    StrictReadOnlyLimitation,
)
from autofund.observer.models import FeeSource


class FakeTransport:
    def __init__(self, *payloads):
        self.payloads = iter(payloads)
        self.requests = []

    def request(self, method, path, authorization=""):
        assert method == "GET"
        self.requests.append((method, path, authorization))
        result = next(self.payloads)
        return (
            result
            if isinstance(result, ReadResponse)
            else ReadResponse(
                200, json.dumps({"success": True, "payload": result}).encode()
            )
        )


def credentials():
    return ProductionCredentials(
        "synthetic-production-key", "synthetic-production-secret", True
    )


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "get", "HEAD"])
def test_production_writes_fail_before_http(method, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP construction reached")

    monkeypatch.setattr(httpx, "Client", forbidden)
    with pytest.raises(ReadOnlyViolation):
        ReadOnlyBitsoTransport().request(method, "/api/v3/balance")


@pytest.mark.parametrize(
    "url",
    [
        "https://stage.bitso.com",
        "https://api.bitso.com",
        "http://bitso.com",
        "https://bitso.com.evil",
        "https://bitso.com/",
    ],
)
def test_host_allowlist(url):
    with pytest.raises(ReadOnlyViolation):
        ReadOnlyBitsoTransport(base_url=url)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v3/orders",
        "/api/v3/open_orders",
        "/api/v3/user_trades",
        "/api/v3/withdrawals",
        "/api/v3/transfers",
        "//stage.bitso.com/api/v3/balance",
        "/api/v3/balance?something=1",
        "/api/v3/../orders",
    ],
)
def test_unsafe_get_endpoints_rejected(path):
    with pytest.raises(ReadOnlyViolation):
        ReadOnlyBitsoTransport().request("GET", path)


def test_actual_get_transport_no_redirects_no_proxy(monkeypatch):
    original = httpx.Client
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://stage.bitso.com"})

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        assert kwargs["timeout"].read == 10 and kwargs["timeout"].connect == 5
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    response = ReadOnlyBitsoTransport().request("GET", "/api/v3/available_books")
    assert response.status == 302 and len(seen) == 1
    assert (
        seen[0].method == "GET"
        and str(seen[0].url) == PRODUCTION_BASE_URL + "/api/v3/available_books"
    )


def test_operator_confirmation_mandatory_and_stage_env_ignored(monkeypatch):
    monkeypatch.setenv("AUTOFUND_BITSO_STAGE_API_KEY", "synthetic")
    with pytest.raises(AuthenticationUnavailable, match="Dedicated"):
        ProductionCredentials.from_environment()
    monkeypatch.setenv("AUTOFUND_BITSO_PROD_API_KEY", "synthetic")
    monkeypatch.setenv("AUTOFUND_BITSO_PROD_API_SECRET", "synthetic")
    with pytest.raises(AuthenticationUnavailable) as exc:
        ProductionCredentials.from_environment()
    assert str(exc.value) == CONFIRMATION_MESSAGE
    monkeypatch.setenv("AUTOFUND_BITSO_PROD_READONLY_CONFIRMED", "true")
    assert ProductionCredentials.from_environment().readonly_confirmed is True


def test_reused_hmac_nonce_and_redaction():
    transport = FakeTransport(
        {
            "balances": [
                {
                    "currency": "mxn",
                    "total": "12345.67",
                    "locked": "0",
                    "available": "12345.67",
                }
            ]
        }
    )
    client = BitsoProductionReadOnlyClient(
        credentials(),
        transport=transport,
        nonce=BitsoNonceProvider(lambda: 1780000000900, lambda n: 1),
    )
    balances = client.balances()
    method, path, auth = transport.requests[0]
    nonce = auth.split(":")[1]
    assert len(nonce) == 19
    expected = hmac.new(
        b"synthetic-production-secret", (nonce + method + path).encode(), hashlib.sha256
    ).hexdigest()
    assert auth.endswith(expected)
    assert "synthetic" not in repr(credentials())
    assert "12345" not in repr(balances)


def test_fee_permission_denial_does_not_expand_permissions():
    denied = ReadResponse(
        401,
        json.dumps(
            {"success": False, "error": {"code": "0202", "message": "sensitive-echo"}}
        ).encode(),
    )
    transport = FakeTransport(denied)
    client = BitsoProductionReadOnlyClient(credentials(), transport=transport)
    with pytest.raises(
        StrictReadOnlyLimitation, match="UNAVAILABLE_IN_STRICT_READ_ONLY_MODE"
    ):
        client.fee_schedule("btc_mxn")
    assert [r[1] for r in transport.requests] == ["/api/v3/fees"]


def test_auth_failure_does_not_echo_secret_or_retry():
    transport = FakeTransport(
        ReadResponse(
            401,
            b'{"success":false,"error":{"code":"0201","message":"synthetic-secret"}}',
        )
    )
    client = BitsoProductionReadOnlyClient(credentials(), transport=transport)
    with pytest.raises(AuthenticationUnavailable) as exc:
        client.balances()
    assert "synthetic" not in str(exc.value)
    with pytest.raises(AuthenticationUnavailable):
        client.balances()
    assert len(transport.requests) == 1


def test_observer_and_shadow_import_audit():
    root = Path(__file__).parents[2] / "src/autofund"
    banned = {
        "autofund.exchanges.bitso.client",
        "autofund.exchanges.bitso.execution",
        "autofund.exchanges.bitso.models",
        "autofund.exchanges.bitso.certification",
    }
    bad_names = {
        "BitsoOrderRequest",
        "ExchangeExecutionPort",
        "StageExecutionEngine",
        "place_order",
        "cancel_order",
    }
    for path in [
        *root.joinpath("observer").glob("*.py"),
        *root.joinpath("shadow").glob("*.py"),
    ]:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in banned
            if isinstance(node, ast.Name):
                assert node.id not in bad_names
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name not in bad_names
    assert not any(hasattr(BitsoProductionReadOnlyClient, name) for name in bad_names)


@pytest.mark.parametrize("value", [1.2, 1, True, "NaN", "Infinity", "1e101"])
def test_float_and_nonfinite_rejected(value):
    from autofund.observer.errors import MarketDataInvalid

    with pytest.raises(MarketDataInvalid):
        parsing.number(value)


def test_public_fee_provenance_and_balance_invariant(limits):
    from dataclasses import asdict

    from autofund.observer.errors import MarketDataInvalid

    record = {k: str(v) for k, v in asdict(limits).items()}
    record["fees"] = {"structure": [{"taker": "0.01"}, {"taker": "0.005"}]}
    _, fee = parsing.market([record], "btc_mxn")
    assert fee.source is FeeSource.PUBLIC_SCHEDULE_FEE and str(fee.rate) == "0.01"
    with pytest.raises(MarketDataInvalid):
        parsing.balances(
            {
                "balances": [
                    {"currency": "mxn", "total": "10", "locked": "1", "available": "10"}
                ]
            }
        )

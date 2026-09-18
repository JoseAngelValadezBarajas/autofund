import hashlib
import hmac
import json
import re
from concurrent.futures import ThreadPoolExecutor
from itertools import count

import httpx
import pytest

from autofund.exchanges.bitso.auth import (
    BitsoCredentials,
    BitsoNonceProvider,
    SignedRequest,
    sign,
    signature,
)
from autofund.exchanges.bitso.client import (
    STAGE_BASE_URL,
    BitsoClient,
    HttpxBitsoTransport,
    TransportResponse,
    validate_request,
)
from autofund.exchanges.bitso.errors import (
    BitsoAuthenticationError,
    BitsoInsufficientFundsError,
    BitsoPermissionError,
    BitsoRateLimitError,
    BitsoTransportError,
    BitsoUnknownSubmissionOutcome,
    BitsoValidationError,
)
from autofund.exchanges.bitso.models import BitsoEnvironment


class FakeTransport:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []

    def send(self, request):
        self.requests.append(request)
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


def response(payload, status=200):
    return TransportResponse(
        status, json.dumps({"success": True, "payload": payload}).encode()
    )


def client(transport):
    clock = [0.0]

    def sleep(delay):
        clock[0] += delay

    return BitsoClient(
        BitsoCredentials("synthetic-key", "synthetic-secret"),
        transport=transport,
        sleep=sleep,
        monotonic=lambda: clock[0],
    )


def test_exact_hmac_known_vector():
    # RFC 4231 case 2; exercises same SHA256 construction without real credentials.
    assert (
        signature("Jefe", "", "", "", b"what do ya want for nothing?")
        == "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"
    )


def test_nonce_v2_fake_and_thread_safety():
    salts = count(100000)
    nonce = BitsoNonceProvider(lambda: 1780000000001, lambda n: next(salts))
    assert nonce.next() == "1780000000001100000"
    with ThreadPoolExecutor(8) as pool:
        values = list(pool.map(lambda _: nonce.next(), range(100)))
    assert len(set(values)) == 100
    assert all(re.fullmatch(r"\d{19}", v) for v in values)


def test_nonce_reuse_across_providers_fails_closed():
    first = BitsoNonceProvider(lambda: 1780000000002, lambda n: 42)
    assert first.next().endswith("000042")
    with pytest.raises(BitsoAuthenticationError):
        BitsoNonceProvider(lambda: 1780000000002, lambda n: 42).next()


@pytest.mark.parametrize("stamp", [1, 1700000000, 1.0, -1])
def test_nonce_v1_or_invalid_clock_rejected(stamp):
    with pytest.raises(BitsoAuthenticationError):
        BitsoNonceProvider(lambda: stamp).next()


def test_credentials_and_requests_redacted(monkeypatch):
    credentials = BitsoCredentials(
        "synthetic-sensitive-key", "synthetic-sensitive-secret"
    )
    request = sign(credentials, "1780000000003000001", "GET", "/api/v3/balance", b"")
    assert "sensitive" not in repr(credentials) + repr(request)
    assert request.authorization not in repr(request)
    monkeypatch.setenv("BITSO_API_KEY", "production-must-be-ignored")
    with pytest.raises(BitsoAuthenticationError):
        BitsoCredentials.from_environment()


@pytest.mark.parametrize(
    "url",
    [
        "https://bitso.com",
        "https://api.bitso.com",
        "http://stage.bitso.com",
        "https://stage.bitso.com.evil",
        "https://stage.bitso.com/",
    ],
)
def test_production_and_arbitrary_urls_rejected(url):
    with pytest.raises(BitsoValidationError):
        HttpxBitsoTransport(base_url=url)
    assert list(BitsoEnvironment) == [BitsoEnvironment.STAGE]


@pytest.mark.parametrize(
    "method,path",
    [
        ("POST", "withdrawals"),
        ("POST", "transfers"),
        ("PATCH", "orders/abc"),
        ("DELETE", "orders/all"),
        ("DELETE", "orders"),
        ("POST", "margin/orders"),
        ("GET", "../balance"),
    ],
)
def test_unsafe_endpoints_rejected(method, path):
    with pytest.raises(BitsoValidationError):
        validate_request(method, "/api/v3/" + path, b"")


def test_signed_get_post_delete_exact_bytes(book, fees, instruction):
    from autofund.exchanges.bitso.models import ExecutionPolicy
    from autofund.exchanges.bitso.precision import ExchangePrecisionPolicy

    prepared = ExchangePrecisionPolicy().prepare(
        instruction, book, fees, ExecutionPolicy(), "af-stage-transport"
    )
    transport = FakeTransport(
        response({"balances": []}),
        response({"oid": "remote-1"}),
        response(["remote-1"]),
    )
    api = client(transport)
    api.get_balances()
    api.place_order(prepared.request)
    assert api.cancel_order("remote-1", "af-stage-transport")
    for request in transport.requests:
        nonce = request.authorization.split(":")[1]
        digest = hmac.new(
            b"synthetic-secret",
            (nonce + request.method + request.path).encode() + request.body,
            hashlib.sha256,
        ).hexdigest()
        assert request.authorization.endswith(digest)
    assert [r.method for r in transport.requests] == ["GET", "POST", "DELETE"]
    assert json.loads(transport.requests[1].body) == prepared.request.payload()


@pytest.mark.parametrize(
    "code,expected",
    [
        ("0201", BitsoAuthenticationError),
        ("0206", BitsoAuthenticationError),
        ("0207", BitsoAuthenticationError),
        ("0202", BitsoPermissionError),
        ("0213", BitsoPermissionError),
        ("0379", BitsoInsufficientFundsError),
        ("0403", BitsoValidationError),
        ("0801", BitsoRateLimitError),
    ],
)
def test_semantic_errors_redacted_no_retry(code, expected):
    transport = FakeTransport(
        TransportResponse(
            400,
            json.dumps(
                {"success": False, "error": {"code": code, "message": "SENSITIVE-ECHO"}}
            ).encode(),
        )
    )
    api = client(transport)
    with pytest.raises(expected) as caught:
        api.get_balances()
    assert "SENSITIVE" not in str(caught.value)
    assert len(transport.requests) == 1
    if issubclass(expected, BitsoAuthenticationError):
        with pytest.raises(BitsoAuthenticationError):
            api.get_balances()
        assert len(transport.requests) == 1


def test_get_bounded_retry_and_post_never_blind_retry(book, fees, instruction):
    from autofund.exchanges.bitso.models import ExecutionPolicy
    from autofund.exchanges.bitso.precision import ExchangePrecisionPolicy

    errors = [BitsoTransportError("synthetic timeout") for _ in range(3)]
    transport = FakeTransport(*errors)
    with pytest.raises(BitsoTransportError):
        client(transport).get_balances()
    assert len(transport.requests) == 3
    transport = FakeTransport(*errors)
    prepared = ExchangePrecisionPolicy().prepare(
        instruction, book, fees, ExecutionPolicy(), "af-stage-timeout"
    )
    with pytest.raises(BitsoUnknownSubmissionOutcome):
        client(transport).place_order(prepared.request)
    assert len(transport.requests) == 1


def test_unknown_acknowledgement_is_ambiguous(book, fees, instruction):
    from autofund.exchanges.bitso.models import ExecutionPolicy
    from autofund.exchanges.bitso.precision import ExchangePrecisionPolicy

    prepared = ExchangePrecisionPolicy().prepare(
        instruction, book, fees, ExecutionPolicy(), "af-stage-badack"
    )
    api = client(FakeTransport(response({"unexpected": "not-an-oid"})))
    with pytest.raises(BitsoUnknownSubmissionOutcome):
        api.place_order(prepared.request)


def test_actual_transport_host_redirects_and_exact_body(monkeypatch):
    original = httpx.Client
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://bitso.com"})

    def replacement(**kwargs):
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        assert kwargs["timeout"].connect == 5 and kwargs["timeout"].read == 10
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", replacement)
    result = HttpxBitsoTransport().send(
        SignedRequest("GET", "/api/v3/balance", b"", "synthetic")
    )
    assert result.status == 302 and len(seen) == 1
    assert str(seen[0].url) == STAGE_BASE_URL + "/api/v3/balance"


def test_lookup_query_is_signed_and_cancel_foreign_forbidden():
    transport = FakeTransport(response([]), response([]), response([]))
    api = client(transport)
    api.get_order(origin="af-stage-known")
    api.get_order(oid="remote-1")
    api.get_order_trades(origin="af-stage-known")
    assert [r.path for r in transport.requests] == [
        "/api/v3/orders?origin_ids=af-stage-known",
        "/api/v3/orders/remote-1",
        "/api/v3/order_trades?origin_id=af-stage-known",
    ]
    with pytest.raises(BitsoValidationError):
        api.cancel_order("remote-1", "af-stage-known")

import json
from decimal import Decimal as D

import pytest

from autofund.exchanges.alpaca.client import AlpacaCredentials, TransportResponse
from autofund.exchanges.alpaca.errors import (
    AlpacaAuthenticationError,
    AlpacaOrderRejected,
    AlpacaTransportError,
    AlpacaUnknownSubmissionOutcome,
    AlpacaValidationError,
)
from autofund.exchanges.alpaca.paper import (
    PAPER_BASE_URL,
    AlpacaPaperClient,
    PaperRequest,
    validate_paper_request,
)

CREDS = AlpacaCredentials("PKPAPERKEY1", "PAPERSECRET1")
UUID = "61e69015-8549-4bfd-b9c3-01e75843f47d"
ORDER = {"id": UUID, "client_order_id": "af-1", "symbol": "SPY", "side": "buy", "status": "accepted",
         "notional": "100.00", "filled_qty": "0", "filled_avg_price": None, "submitted_at": "2026-11-23T15:00:00Z"}


def good_body(**over):
    base = {"symbol": "SPY", "notional": "100.00", "side": "buy", "type": "market",
            "time_in_force": "day", "client_order_id": "af-1"}
    base.update(over)
    return json.dumps(base).encode()


class Fake:
    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def send(self, request: PaperRequest):
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(*responses):
    t, clock = Fake(*responses), [0.0]
    return AlpacaPaperClient(CREDS, transport=t, sleep=lambda s: clock.__setitem__(0, clock[0] + s),
                             monotonic=lambda: clock[0]), t


def ok(payload, status=200):
    return TransportResponse(status, json.dumps(payload).encode())


# ---- allowlist ----

@pytest.mark.parametrize("method,path", [
    ("GET", "/v2/account"), ("GET", "/v2/clock"), ("GET", "/v2/positions"),
    ("GET", "/v2/orders?status=open&limit=50"), ("GET", f"/v2/orders/{UUID}"),
    ("GET", "/v2/orders:by_client_order_id?client_order_id=af-1"),
    ("DELETE", "/v2/orders"), ("DELETE", f"/v2/orders/{UUID}"), ("DELETE", "/v2/positions/SPY"),
])
def test_permitted_operations(method, path):
    validate_paper_request(method, path)


@pytest.mark.parametrize("method,path", [
    ("GET", "/v2/account/configurations"), ("PATCH", "/v2/account/configurations"),
    ("DELETE", "/v2/positions"), ("DELETE", "/v2/account"), ("GET", "/v2/assets"),
    ("POST", "/v2/orders/replace"), ("GET", "/v2/orders?status=weird"), ("GET", "/v2/orders?limit=9999"),
    ("GET", "https://api.alpaca.markets/v2/account"), ("GET", "//evil.example/v2/account"),
    ("DELETE", "/v2/positions/spy"), ("GET", "/v2/orders/not-a-uuid"),
])
def test_everything_else_is_refused(method, path):
    with pytest.raises(AlpacaValidationError):
        validate_paper_request(method, path)


def test_order_body_is_constrained():
    validate_paper_request("POST", "/v2/orders", good_body())
    for bad in (good_body(type="limit"), good_body(time_in_force="gtc"), good_body(side="short"),
                good_body(notional="0.5"), good_body(notional="999999"), good_body(notional="abc"),
                good_body(symbol="spy"), good_body(client_order_id="has space"),
                json.dumps({"symbol": "SPY", "qty": "1", "side": "buy", "type": "market",
                            "time_in_force": "day", "client_order_id": "x"}).encode(),
                json.dumps({**json.loads(good_body()), "extended_hours": True}).encode()):
        with pytest.raises(AlpacaValidationError):
            validate_paper_request("POST", "/v2/orders", bad)


def test_the_live_trading_host_is_not_representable():
    assert PAPER_BASE_URL == "https://paper-api.alpaca.markets"
    import autofund.exchanges.alpaca.paper as module
    source = open(module.__file__, encoding="utf-8").read()
    assert "https://api.alpaca.markets" not in source


def test_live_keys_cannot_construct_credentials():
    with pytest.raises(AlpacaAuthenticationError):
        AlpacaCredentials("AKLIVE", "x")


# ---- reads and parsing ----

def test_account_clock_positions_orders():
    c, _ = client(
        ok({"status": "ACTIVE", "equity": "100000.50", "last_equity": "100000", "cash": "90000",
            "buying_power": "180000", "trading_blocked": False}),
        ok({"is_open": False, "timestamp": "t", "next_open": "o", "next_close": "c"}),
        ok([{"symbol": "SPY", "qty": "2", "market_value": "1000", "avg_entry_price": "490",
             "unrealized_pl": "20", "current_price": "500"}]),
        ok([ORDER]),
    )
    account = c.get_account()
    assert account.equity == D("100000.50") and not account.trading_blocked
    assert c.get_clock().is_open is False
    assert c.get_positions()[0].market_value == D("1000")
    order = c.list_orders()[0]
    assert order.status == "accepted" and not order.terminal and order.filled_avg_price is None


def test_malformed_numbers_are_rejected():
    from autofund.exchanges.alpaca.errors import AlpacaParseError
    c, _ = client(ok({"status": "ACTIVE", "equity": "NaN", "last_equity": "1", "cash": "1",
                      "buying_power": "1"}))
    with pytest.raises(AlpacaParseError):
        c.get_account()


# ---- writes ----

def test_submit_is_a_single_idempotent_post():
    c, t = client(ok(ORDER))
    order = c.submit_market_notional(symbol="SPY", side="buy", notional=D("100"), client_order_id="af-1")
    assert order.client_order_id == "af-1"
    sent = json.loads(t.requests[0].body)
    assert sent["notional"] == "100.00" and sent["type"] == "market" and sent["time_in_force"] == "day"
    assert t.requests[0].method == "POST" and len(t.requests) == 1


def test_unknown_outcome_is_not_retried_and_is_reportable():
    c, t = client(AlpacaTransportError("x"))
    with pytest.raises(AlpacaUnknownSubmissionOutcome):
        c.submit_market_notional(symbol="SPY", side="buy", notional=D("100"), client_order_id="af-1")
    assert len(t.requests) == 1  # a blind resubmission would be a double order


def test_server_error_on_post_is_an_unknown_outcome():
    c, _ = client(TransportResponse(503, b""))
    with pytest.raises(AlpacaUnknownSubmissionOutcome):
        c.submit_market_notional(symbol="SPY", side="buy", notional=D("100"), client_order_id="af-1")


def test_rejection_is_explicit():
    c, _ = client(TransportResponse(422, b'{"message":"insufficient buying power secret-body"}'))
    with pytest.raises(AlpacaOrderRejected) as info:
        c.submit_market_notional(symbol="SPY", side="buy", notional=D("100"), client_order_id="af-1")
    assert "secret-body" not in str(info.value)


def test_lookup_by_client_id_returns_none_when_absent():
    c, _ = client(TransportResponse(404, b"{}"))
    assert c.get_order_by_client_id("af-1") is None


def test_reads_retry_but_auth_failure_latches():
    c, t = client(TransportResponse(503, b""), ok({"is_open": True, "timestamp": "t",
                                                    "next_open": "o", "next_close": "c"}))
    assert c.get_clock().is_open and len(t.requests) == 2
    c2, t2 = client(TransportResponse(401, b"nope"))
    for _ in range(2):
        with pytest.raises(AlpacaAuthenticationError):
            c2.get_account()
    assert len(t2.requests) == 1


def test_cancel_and_close():
    c, t = client(TransportResponse(207, b"[]"), TransportResponse(204, b""), ok({}))
    c.cancel_open_orders()
    c.cancel_order(UUID)
    c.close_position("SPY")
    assert [(r.method, r.path) for r in t.requests] == [
        ("DELETE", "/v2/orders"), ("DELETE", f"/v2/orders/{UUID}"), ("DELETE", "/v2/positions/SPY")]

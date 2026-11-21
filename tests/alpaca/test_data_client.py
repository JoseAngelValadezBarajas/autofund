import json
from datetime import date
from decimal import Decimal

import pytest

from autofund.exchanges.alpaca import parsing
from autofund.exchanges.alpaca.client import (
    AlpacaCredentials,
    AlpacaDataClient,
    AlpacaRequest,
    TransportResponse,
    validate_request,
)
from autofund.exchanges.alpaca.errors import (
    AlpacaAuthenticationError,
    AlpacaError,
    AlpacaParseError,
    AlpacaRateLimitError,
    AlpacaTransportError,
    AlpacaValidationError,
)
from autofund.exchanges.alpaca.models import AlpacaBarSeries
from autofund.mvp.historical import ALPACA_HISTORICAL, from_alpaca

KEY, SECRET = "PKTESTKEY123", "TESTSECRET456"
CREDS = AlpacaCredentials(KEY, SECRET)
GOOD = "/v2/stocks/bars?symbols=SPY&timeframe=1Day&adjustment=raw&feed=iex"


def bar(day: str, close: float = 100.5, volume: int = 1000) -> dict:
    # Plain JSON numbers, exactly as the real API sends them.
    return {"t": f"{day}T04:00:00Z", "o": 100.1, "h": 101.2, "l": 99.9, "c": close,
            "v": volume, "n": 10, "vw": 100.4}


def body(bars: dict, token: str | None = None) -> bytes:
    return json.dumps({"bars": bars, "next_page_token": token}).encode()


class FakeTransport:
    def __init__(self, *responses: TransportResponse | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[AlpacaRequest] = []

    def send(self, request: AlpacaRequest) -> TransportResponse:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def client(*responses):
    transport = FakeTransport(*responses)
    clock = [0.0]
    return AlpacaDataClient(
        CREDS, transport=transport, sleep=lambda s: clock.__setitem__(0, clock[0] + s),
        monotonic=lambda: clock[0],
    ), transport


def ok(payload: bytes) -> TransportResponse:
    return TransportResponse(200, payload)


# ---- allowlist ----

@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_only_get_is_permitted(method):
    with pytest.raises(AlpacaValidationError):
        validate_request(method, GOOD)


@pytest.mark.parametrize("path", [
    "/v2/account",
    "/v2/orders",
    "/v2/stocks/quotes?symbols=SPY",
    "/v2/stocks/bars/latest?symbols=SPY",
    "https://paper-api.alpaca.markets/v2/stocks/bars?symbols=SPY",
    "//evil.example/v2/stocks/bars?symbols=SPY",
    "/v2/stocks/bars#frag",
    "/v2/stocks/%62ars?symbols=SPY",
])
def test_unlisted_paths_are_refused(path):
    with pytest.raises(AlpacaValidationError):
        validate_request("GET", path)


@pytest.mark.parametrize("query", [
    "symbols=SPY&timeframe=1Day&adjustment=raw",  # feed missing: no silent default
    "symbols=SPY&timeframe=1Day&feed=iex",  # adjustment missing: no silent default
    "symbols=spy&timeframe=1Day&adjustment=raw&feed=iex",
    "symbols=SPY&timeframe=1Min&adjustment=raw&feed=iex",
    "symbols=SPY&timeframe=1Day&adjustment=raw&feed=iex&extra=1",
    "symbols=SPY&symbols=QQQ&timeframe=1Day&adjustment=raw&feed=iex",
    "symbols=SPY&timeframe=1Day&adjustment=raw&feed=iex&start=2024-1-1",
    "symbols=SPY&timeframe=1Day&adjustment=raw&feed=otc",
])
def test_query_is_validated(query):
    with pytest.raises(AlpacaValidationError):
        validate_request("GET", "/v2/stocks/bars?" + query)


def test_a_good_request_passes():
    validate_request("GET", GOOD)


# ---- parsing ----

def test_prices_are_decimal_never_float():
    page, token = parsing.page(body({"SPY": [bar("2024-01-02")]}))
    candle = page["SPY"][0]
    assert candle.close == Decimal("100.5")
    assert all(isinstance(v, Decimal) for v in (candle.open, candle.high, candle.low, candle.close))
    assert token is None


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"bars": []}', b'{"bars": {"SPY": 5}}',
                                 b'{"bars": {"spy": []}}', b'{"bars": {"SPY": [1]}}',
                                 b'{"bars": {"SPY": [{"t": "x"}]}}', b'{"next_page_token": ""}',
                                 b'{"bars": {"SPY": [{"t":"2024-01-02T04:00:00Z","o":NaN,"h":1,"l":1,"c":1,"v":1}]}}'])
def test_malformed_responses_are_rejected(raw):
    with pytest.raises(AlpacaParseError):
        parsing.page(raw)


def test_inconsistent_ohlc_is_rejected():
    bad = json.dumps({"bars": {"SPY": [{"t": "2024-01-02T04:00:00Z", "o": 5, "h": 4, "l": 3, "c": 4, "v": 1}]}})
    with pytest.raises(AlpacaParseError):
        parsing.page(bad.encode())


# ---- client ----

def test_requires_adjustment_and_feed_explicitly():
    c, _ = client()
    with pytest.raises(TypeError):
        c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 3))  # type: ignore[call-arg]


def test_fetch_builds_series_with_provenance():
    c, t = client(ok(body({"SPY": [bar("2024-01-02"), bar("2024-01-03")]})))
    out = c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 3), adjustment="all", feed="iex")
    series = out["SPY"]
    assert (series.adjustment, series.feed, len(series.candles)) == ("all", "iex", 2)
    assert series.is_real_market_data
    query = t.requests[0].path
    assert "adjustment=all" in query and "feed=iex" in query and "timeframe=1Day" in query
    assert series.telemetry()["dataset_fingerprint"] == series.fingerprint


def test_pagination_follows_tokens_and_merges():
    c, t = client(
        ok(body({"SPY": [bar("2024-01-02")]}, token="abc123")),
        ok(body({"SPY": [bar("2024-01-03")]})),
    )
    out = c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 3), adjustment="raw", feed="iex")
    assert len(out["SPY"].candles) == 2
    assert "page_token=abc123" in t.requests[1].path


def test_endless_pagination_is_bounded():
    pages = [ok(body({"SPY": [bar("2024-01-02")]}, token="t")) for _ in range(300)]
    c, _ = client(*pages)
    with pytest.raises((AlpacaTransportError, AlpacaError)):
        c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 3), adjustment="raw", feed="iex")


def test_empty_result_is_an_error_not_an_empty_series():
    c, _ = client(ok(body({})))
    with pytest.raises(AlpacaValidationError, match="SPY"):
        c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 3), adjustment="raw", feed="iex")


def test_duplicate_and_out_of_order_bars_are_rejected():
    for bars in ([bar("2024-01-02"), bar("2024-01-02")], [bar("2024-01-03"), bar("2024-01-02")]):
        c, _ = client(ok(body({"SPY": bars})))
        with pytest.raises(AlpacaError):
            c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 3), adjustment="raw", feed="iex")


def test_unrequested_symbol_in_response_is_rejected():
    c, _ = client(ok(body({"QQQ": [bar("2024-01-02")]})))
    with pytest.raises(AlpacaError):
        c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 3), adjustment="raw", feed="iex")


def test_gaps_need_a_peer_and_closures_are_not_gaps():
    # 2024-01-03 (Wed): QQQ traded, SPY did not -> a confirmed gap for SPY.
    # 2024-01-05 (Fri): neither traded -> unclassified, never counted as a gap.
    spy = [bar("2024-01-02"), bar("2024-01-04"), bar("2024-01-08")]
    qqq = [bar("2024-01-02"), bar("2024-01-03"), bar("2024-01-04"), bar("2024-01-08")]
    c, _ = client(ok(body({"SPY": spy, "QQQ": qqq})))
    out = c.get_daily_bars(["SPY", "QQQ"], date(2024, 1, 2), date(2024, 1, 8), adjustment="raw", feed="iex")
    assert (out["SPY"].gaps, out["SPY"].unclassified_weekdays) == (1, 1)
    assert (out["QQQ"].gaps, out["QQQ"].unclassified_weekdays) == (0, 1)


def test_single_symbol_cannot_claim_a_gap():
    c, _ = client(ok(body({"SPY": [bar("2024-01-02"), bar("2024-01-08")]})))
    out = c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 8), adjustment="raw", feed="iex")
    assert out["SPY"].gaps == 0
    assert out["SPY"].unclassified_weekdays == 3


# ---- fingerprints ----

def make_series(close=100.5, adjustment="raw", feed="iex") -> AlpacaBarSeries:
    c, _ = client(ok(body({"SPY": [bar("2024-01-02", close)]})))
    return c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 2), adjustment=adjustment, feed=feed)["SPY"]


def test_fingerprint_is_stable_and_sensitive_to_every_identity_field():
    base = make_series()
    assert base.fingerprint == make_series().fingerprint
    assert base.fingerprint != make_series(close=100.6).fingerprint
    assert base.fingerprint != make_series(adjustment="all").fingerprint
    assert base.fingerprint != make_series(feed="sip").fingerprint


def test_bridge_into_the_research_pipeline_is_real_and_labelled():
    series = make_series()
    bridged = from_alpaca(series)
    assert bridged.source == ALPACA_HISTORICAL
    assert bridged.is_real_market_data
    assert "alpaca:iex:raw" in bridged.source_detail
    assert series.fingerprint in bridged.source_detail


# ---- errors and secrets ----

def test_errors_never_contain_credentials_or_bodies():
    leak = f"{KEY} {SECRET} secret-body".encode()
    for status, exc in ((401, AlpacaAuthenticationError), (429, AlpacaRateLimitError),
                        (500, AlpacaTransportError), (422, AlpacaValidationError)):
        responses = [TransportResponse(status, leak)] * (3 if status == 500 else 1)
        c, _ = client(*responses)
        with pytest.raises(exc) as info:
            c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 3), adjustment="raw", feed="iex")
        text = str(info.value) + repr(info.value)
        assert KEY not in text and SECRET not in text and "secret-body" not in text


def test_credentials_and_requests_do_not_repr_secrets():
    request = AlpacaRequest("GET", GOOD, CREDS)
    for text in (repr(CREDS), repr(request), str(request)):
        assert KEY not in text and SECRET not in text


def test_authentication_failure_latches():
    c, t = client(TransportResponse(403, b""))
    with pytest.raises(AlpacaAuthenticationError):
        c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 3), adjustment="raw", feed="iex")
    with pytest.raises(AlpacaAuthenticationError):
        c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 3), adjustment="raw", feed="iex")
    assert len(t.requests) == 1


def test_transient_failures_are_retried_for_reads():
    c, t = client(TransportResponse(503, b""), ok(body({"SPY": [bar("2024-01-02")]})))
    out = c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 2), adjustment="raw", feed="iex")
    assert len(out["SPY"].candles) == 1 and len(t.requests) == 2


def test_credentials_come_from_the_environment_only():
    with pytest.raises(AlpacaAuthenticationError):
        AlpacaCredentials.from_env({})
    creds = AlpacaCredentials.from_env({
        "AUTOFUND_ALPACA_DATA_API_KEY": KEY, "AUTOFUND_ALPACA_DATA_API_SECRET": SECRET})
    assert creds.key_id == KEY


def test_no_trading_host_is_representable():
    import autofund.exchanges.alpaca.client as module
    source = open(module.__file__, encoding="utf-8").read()
    assert "paper-api" not in source and "api.alpaca.markets" not in source.replace(
        "data.alpaca.markets", "")


def test_a_plan_limit_is_not_an_authentication_failure_and_does_not_latch():
    from autofund.exchanges.alpaca.errors import AlpacaSubscriptionError
    plan_limit = TransportResponse(403, b'{"message":"subscription does not permit querying recent SIP data"}')
    c, t = client(plan_limit, ok(body({"SPY": [bar("2024-01-02")]})))
    with pytest.raises(AlpacaSubscriptionError) as info:
        c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 3), adjustment="raw", feed="sip")
    assert "SIP data" not in str(info.value)  # the response body is never echoed
    out = c.get_daily_bars(["SPY"], date(2024, 1, 2), date(2024, 1, 3), adjustment="raw", feed="sip")
    assert len(out["SPY"].candles) == 1 and len(t.requests) == 2  # the client was not locked out

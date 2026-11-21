import json
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from autofund.exchanges.alpaca import parsing
from autofund.exchanges.alpaca.client import (
    AlpacaCredentials,
    AlpacaDataClient,
    TransportResponse,
    validate_request,
)
from autofund.exchanges.alpaca.env import load_credentials, parse_env_file
from autofund.exchanges.alpaca.errors import (
    AlpacaAuthenticationError,
    AlpacaParseError,
    AlpacaValidationError,
)
from autofund.exchanges.alpaca.models import Quote
from autofund.mvp.alpaca_economics import (
    Headroom,
    half_spread_bps,
    in_regular_session,
    measure_spread,
    round_trip_friction,
)

TUE_OPEN = datetime(2024, 1, 9, 16, 0, tzinfo=UTC)
SAT = datetime(2024, 1, 6, 16, 0, tzinfo=UTC)
NIGHT = datetime(2024, 1, 9, 3, 0, tzinfo=UTC)


def quote(bid="100.00", ask="100.10", at=TUE_OPEN, symbol="SPY") -> Quote:
    return Quote(symbol, at, D(bid), D(ask))


# ---- .env loading ----

def test_env_parser_reads_only_the_two_names():
    text = ("# comment\nOTHER=1\nAUTOFUND_ALPACA_DATA_API_KEY=PKABC\n"
            "AUTOFUND_ALPACA_DATA_API_SECRET='s3cret'\n")
    assert parse_env_file(text) == {"AUTOFUND_ALPACA_DATA_API_KEY": "PKABC",
                                    "AUTOFUND_ALPACA_DATA_API_SECRET": "s3cret"}


def test_env_parser_rejects_duplicates():
    with pytest.raises(AlpacaAuthenticationError):
        parse_env_file("AUTOFUND_ALPACA_DATA_API_KEY=a\nAUTOFUND_ALPACA_DATA_API_KEY=b\n")


def test_load_credentials_from_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("AUTOFUND_ALPACA_DATA_API_KEY=PKFILEKEY\nAUTOFUND_ALPACA_DATA_API_SECRET=zzz\n")
    assert load_credentials(path).key_id == "PKFILEKEY"


def test_missing_file_error_does_not_leak_path_or_content(tmp_path):
    with pytest.raises(AlpacaAuthenticationError) as info:
        load_credentials(tmp_path / "nope.env")
    assert "nope" not in str(info.value)


def test_live_keys_are_refused():
    with pytest.raises(AlpacaAuthenticationError, match="paper"):
        AlpacaCredentials("AKLIVEKEY", "secret")


# ---- quotes ----

def test_quote_validation():
    for bid, ask in (("0", "1"), ("1", "1"), ("2", "1"), ("-1", "1")):
        with pytest.raises(AlpacaValidationError):
            quote(bid, ask)
    with pytest.raises(AlpacaValidationError):
        Quote("SPY", datetime(2024, 1, 9), D("1"), D("2"))


def test_quote_parsing_is_decimal():
    body = json.dumps({"quotes": {"SPY": {"t": "2024-01-09T16:00:00Z", "bp": 100.0, "ap": 100.1}}})
    out = parsing.quotes(body.encode())
    assert out["SPY"].ask == D("100.1") and isinstance(out["SPY"].bid, D)


@pytest.mark.parametrize("raw", [b'{"quotes": []}', b'{"quotes": {"SPY": 1}}',
                                 b'{"quotes": {"SPY": {"t":"2024-01-09T16:00:00Z","bp":2,"ap":1}}}'])
def test_bad_quotes_are_rejected(raw):
    with pytest.raises(AlpacaParseError):
        parsing.quotes(raw)


def test_quote_allowlist():
    validate_request("GET", "/v2/stocks/quotes/latest?symbols=SPY,QQQ&feed=iex")
    for bad in ("/v2/stocks/quotes/latest?symbols=SPY",
                "/v2/stocks/quotes/latest?symbols=SPY&feed=iex&limit=5",
                "/v2/stocks/quotes/latest?symbols=spy&feed=iex",
                "/v2/stocks/trades/latest?symbols=SPY&feed=iex"):
        with pytest.raises(AlpacaValidationError):
            validate_request("GET", bad)


def test_client_latest_quotes():
    body = json.dumps({"quotes": {"SPY": {"t": "2024-01-09T16:00:00Z", "bp": 100, "ap": 100.1}}})

    class T:
        def send(self, request):
            assert "quotes/latest" in request.path
            return TransportResponse(200, body.encode())

    client = AlpacaDataClient(AlpacaCredentials("PKX", "y"), transport=T(),
                              sleep=lambda s: None, monotonic=lambda: 0.0)
    assert client.get_latest_quotes(["SPY"], feed="iex")["SPY"].bid == D("100")


# ---- economics ----

def test_half_spread_bps():
    # spread 0.10 on mid 100.05 -> 9.995 bps wide, half = ~4.9975
    assert half_spread_bps(quote()).quantize(D("0.0001")) == D("4.9975")


def test_session_filter_excludes_weekend_and_night():
    assert in_regular_session(TUE_OPEN)
    assert not in_regular_session(SAT) and not in_regular_session(NIGHT)


def test_out_of_session_quotes_are_counted_not_used():
    spread = measure_spread("SPY", [quote(at=SAT), quote(at=NIGHT)])
    assert not spread.usable and spread.excluded_out_of_session == 2
    with pytest.raises(ValueError, match="unmeasured"):
        round_trip_friction(spread, slippage_bps_per_side=D("1"))


def test_measurement_uses_the_median():
    spread = measure_spread("SPY", [quote(ask="100.02"), quote(ask="100.04"), quote(ask="100.50")])
    assert spread.samples == 3 and spread.median_half_spread_bps == half_spread_bps(quote(ask="100.04"))
    assert spread.max_half_spread_bps == half_spread_bps(quote(ask="100.50"))


def test_unknown_components_make_friction_a_lower_bound_never_zero():
    spread = measure_spread("SPY", [quote()])
    friction = round_trip_friction(spread, slippage_bps_per_side=D("1"))
    assert friction.is_lower_bound and "regulatory_sell_fees" in friction.unknown
    assert friction.total_bps == spread.median_half_spread_bps * 2 + D("2")
    unknown_commission = round_trip_friction(spread, slippage_bps_per_side=D("1"),
                                             commission_bps_per_side=None)
    assert "commission" in unknown_commission.unknown


def test_headroom_keeps_movement_and_friction_separate():
    assert not Headroom(D("2.5"), D("173"), True).pays_for_itself
    assert Headroom(D("2.5"), D("173"), True).conclusive  # negative is conclusive even as a bound
    assert not Headroom(D("50"), D("10"), True).conclusive  # positive vs a lower bound is not
    assert Headroom(D("50"), D("10"), False).pays_for_itself

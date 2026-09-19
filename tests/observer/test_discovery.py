"""Read-only discovery parsing tests.

Locks in the fix that discovery is quote-agnostic: the scanner must be able to see
every exchange book before filtering to MXN. No network access; payloads are
injected.
"""

from decimal import Decimal as D

import pytest

from autofund.observer.models import BookConstraints, MarketDataInvalid
from autofund.observer.parsing import books

ROW = {"book": "btc_mxn", "minimum_amount": "0.000001", "maximum_amount": "600",
       "minimum_price": "100", "maximum_price": "40000000", "minimum_value": "10",
       "maximum_value": "200000000", "tick_size": "10"}


def test_discovery_parses_non_mxn_books():
    payload = [ROW, {**ROW, "book": "btc_usd"}, {**ROW, "book": "eth_btc"},
               {**ROW, "book": "usdt_mxn", "minimum_value": "5"}]
    result = books(payload)
    assert {item.book for item in result} == {"btc_mxn", "btc_usd", "eth_btc", "usdt_mxn"}
    assert {item.quote_currency for item in result} == {"mxn", "usd", "btc"}


def test_discovery_rejects_duplicates_and_malformed_books():
    with pytest.raises(MarketDataInvalid):
        books([ROW, ROW])
    with pytest.raises(MarketDataInvalid):
        books([{**ROW, "book": "not a book"}])
    with pytest.raises(MarketDataInvalid):
        books([{**ROW, "minimum_value": "0"}])


def test_book_constraints_expose_exchange_limits():
    item = books([ROW])[0]
    assert isinstance(item, BookConstraints)
    assert item.minimum_value == D("10")
    assert item.tick_size == D("10")
    assert item.quote_currency == "mxn"


def test_mxn_filter_selects_only_mxn_quoted_books():
    payload = [ROW, {**ROW, "book": "btc_usd"}, {**ROW, "book": "sol_mxn"}]
    mxn = [item.book for item in books(payload) if item.quote_currency == "mxn"]
    assert mxn == ["btc_mxn", "sol_mxn"]


def test_f4_trading_models_still_require_mxn():
    """The quote-agnostic discovery type must not loosen F4's trading contract."""
    from autofund.observer.models import MarketLimits
    with pytest.raises(MarketDataInvalid):
        MarketLimits("btc_usd", *([D("1")] * 7))

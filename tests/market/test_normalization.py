import json
from dataclasses import FrozenInstanceError
from datetime import UTC
from decimal import Decimal as D

import pytest

from autofund.market.binance_normalizer import normalize_kline, parse_market_info
from autofund.market.errors import ConfigurationError, InvalidMessage, WrongStream


def test_metadata_decimal_filters(info):
    assert info.market == "BTC/MXN"
    assert info.status == "TRADING"
    assert info.price_tick_size == D("1")
    assert info.quantity_step_size == info.minimum_quantity == D("0.000001")
    assert info.maximum_quantity == D("922")
    assert info.minimum_notional == D("150")
    assert info.maximum_notional == D("9000000")
    with pytest.raises(FrozenInstanceError):
        info.price_tick_size = D("2")


def test_missing_filters_distinct_from_disabled_zero(metadata):
    payload = json.loads(metadata)
    payload["symbols"][0]["filters"] = []
    absent = parse_market_info(json.dumps(payload), "BTCMXN")
    assert absent.price_tick_size is absent.minimum_notional is None
    payload["symbols"][0]["filters"] = [
        {"filterType": "PRICE_FILTER", "tickSize": "0"},
        {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
    ]
    present = parse_market_info(json.dumps(payload), "BTCMXN")
    assert present.price_tick_size == D("0")
    assert present.minimum_notional == D("5")
    assert present.maximum_notional is None


@pytest.mark.parametrize("value", [1.0, 1, "NaN", "Infinity", "-1", "1_00", " 1"])
def test_bad_metadata_numbers(metadata, value):
    payload = json.loads(metadata)
    payload["symbols"][0]["filters"][0]["tickSize"] = value
    with pytest.raises(ConfigurationError):
        parse_market_info(json.dumps(payload), "BTCMXN")


@pytest.mark.parametrize(
    "mutation", ["symbol", "spot", "empty", "filters", "duplicate"]
)
def test_required_metadata_fail_fast(metadata, mutation):
    payload = json.loads(metadata)
    if mutation == "symbol":
        payload["symbols"][0]["symbol"] = "ETHMXN"
    elif mutation == "spot":
        payload["symbols"][0]["isSpotTradingAllowed"] = False
    elif mutation == "empty":
        payload["symbols"] = []
    elif mutation == "filters":
        del payload["symbols"][0]["filters"]
    else:
        payload["symbols"][0]["filters"].append(payload["symbols"][0]["filters"][0])
    with pytest.raises(ConfigurationError):
        parse_market_info(json.dumps(payload), "BTCMXN")


def test_partial_closed_utc_and_candle_compatibility(events):
    partial, closed = events[:2]
    assert not partial.is_closed and closed.is_closed
    assert closed.open_time.tzinfo is UTC
    assert closed.close_time.tzinfo is UTC
    assert closed.source_event_time.tzinfo is UTC
    assert closed.volume == D("10")
    assert isinstance(closed.close, D)
    candle = closed.to_candle()
    assert candle.timestamp == closed.open_time
    assert candle.close == closed.close
    with pytest.raises(FrozenInstanceError):
        closed.close = D("0")


@pytest.mark.parametrize("field,value", [("s", "ETHMXN"), ("i", "1s")])
def test_wrong_stream(info, messages, field, value):
    payload = json.loads(messages[1])
    payload["k"][field] = value
    with pytest.raises(WrongStream):
        normalize_kline(json.dumps(payload), info, "1m")


@pytest.mark.parametrize(
    "field,value",
    [
        ("c", 1.5),
        ("c", "NaN"),
        ("o", "0"),
        ("h", "1"),
        ("l", "2000000"),
        ("v", "-1"),
        ("x", "true"),
        ("t", 1767225600000.0),
        ("T", 1767225660000),
    ],
)
def test_bad_kline_fields(info, messages, field, value):
    payload = json.loads(messages[1])
    payload["k"][field] = value
    with pytest.raises(InvalidMessage):
        normalize_kline(json.dumps(payload), info, "1m")


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"e":"kline","e":"trade"}',
        '{"e":"kline"}',
        '{"e":"trade"}',
        '{"e":NaN}',
        "[" * 2000 + "]" * 2000,
    ],
)
def test_malformed_payload_rejected(info, payload):
    with pytest.raises(InvalidMessage):
        normalize_kline(payload, info, "1m")

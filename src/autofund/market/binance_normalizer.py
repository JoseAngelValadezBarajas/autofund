"""The only module that interprets Binance Spot JSON fields."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast

from .errors import ConfigurationError, InvalidMessage, WrongStream
from .models import LiveCandleUpdate, MarketInfo


def object_json(text: str) -> dict[str, object]:
    def reject_constant(value: str) -> object:
        raise InvalidMessage(f"non-JSON number {value}")

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise InvalidMessage("duplicate JSON key")
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            parse_float=Decimal,
            parse_constant=reject_constant,
            object_pairs_hook=unique_pairs,
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidMessage("invalid JSON") from exc
    if not isinstance(value, dict):
        raise InvalidMessage("JSON message must be an object")
    return cast(dict[str, object], value)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise InvalidMessage("expected JSON object")
    return cast(dict[str, object], value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidMessage("expected nonempty string")
    return value


def _number(value: object) -> Decimal:
    # Binance documents financial fields as strings. Numeric JSON is rejected.
    text = _text(value)
    if text.strip() != text or "_" in text:
        raise InvalidMessage("invalid decimal string")
    try:
        value_decimal = Decimal(text)
    except InvalidOperation as exc:
        raise InvalidMessage("invalid decimal string") from exc
    if not value_decimal.is_finite():
        raise InvalidMessage("nonfinite decimal")
    return value_decimal


def _timestamp(value: object) -> datetime:
    if type(value) is not int or value < 0:
        raise InvalidMessage("expected nonnegative millisecond timestamp")
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)
    except OverflowError as exc:
        raise InvalidMessage("timestamp out of range") from exc


def parse_market_info(text: str, symbol: str) -> MarketInfo:
    try:
        root = object_json(text)
        symbols = root.get("symbols")
        if not isinstance(symbols, list) or len(symbols) != 1:
            raise InvalidMessage("expected exactly one symbol")
        row = _object(symbols[0])
        if row.get("symbol") != symbol or row.get("isSpotTradingAllowed") is not True:
            raise InvalidMessage("wrong or non-Spot symbol")
        filters = row.get("filters")
        if not isinstance(filters, list):
            raise InvalidMessage("missing filters array")
        indexed: dict[str, dict[str, object]] = {}
        for raw in filters:
            item = _object(raw)
            kind = _text(item.get("filterType"))
            if kind in indexed:
                raise InvalidMessage("duplicate filter")
            indexed[kind] = item

        def optional(kind: str, field: str) -> Decimal | None:
            item = indexed.get(kind, {})
            return _number(item[field]) if field in item else None

        minima = [
            value
            for value in (
                optional("MIN_NOTIONAL", "minNotional"),
                optional("NOTIONAL", "minNotional"),
            )
            if value is not None
        ]
        return MarketInfo(
            symbol,
            _text(row.get("baseAsset")),
            _text(row.get("quoteAsset")),
            _text(row.get("status")),
            optional("PRICE_FILTER", "tickSize"),
            optional("LOT_SIZE", "stepSize"),
            optional("LOT_SIZE", "minQty"),
            optional("LOT_SIZE", "maxQty"),
            max(minima) if minima else None,
            optional("NOTIONAL", "maxNotional"),
        )
    except InvalidMessage as exc:
        raise ConfigurationError(f"invalid exchange metadata: {exc}") from exc


def normalize_kline(text: str, info: MarketInfo, interval: str) -> LiveCandleUpdate:
    row = object_json(text)
    if row.get("e") != "kline":
        raise InvalidMessage("unexpected event type")
    candle = _object(row.get("k"))
    if row.get("s") != info.symbol or candle.get("s") != info.symbol:
        raise WrongStream("wrong symbol")
    if candle.get("i") != interval:
        raise WrongStream("wrong interval")
    closed = candle.get("x")
    if type(closed) is not bool:
        raise InvalidMessage("kline closed flag must be boolean")
    return LiveCandleUpdate(
        info.market,
        interval,
        _timestamp(candle.get("t")),
        _timestamp(candle.get("T")),
        _number(candle.get("o")),
        _number(candle.get("h")),
        _number(candle.get("l")),
        _number(candle.get("c")),
        _number(candle.get("v")),
        closed,
        _timestamp(row.get("E")),
    )


def is_server_shutdown(text: str) -> bool:
    return object_json(text).get("e") == "serverShutdown"

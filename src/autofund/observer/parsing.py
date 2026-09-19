import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from autofund.decimal_utils import decimal
from autofund.errors import InvalidFinancialInput
from autofund.replay.serialization import utc_timestamp

from .errors import MarketDataInvalid
from .models import (
    BookConstraints,
    FeeSource,
    Level,
    MarketLimits,
    ObservedBalance,
    OrderBookSnapshot,
    PublicTrade,
    ShadowFee,
    Ticker,
)


def obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise MarketDataInvalid("expected object")
    return cast(dict[str, object], value)


def array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise MarketDataInvalid("expected array")
    return cast(list[object], value)


def text(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise MarketDataInvalid("expected nonempty string")
    return value


def number(value: object) -> Decimal:
    if not isinstance(value, str):
        raise MarketDataInvalid("financial wire value must be string")
    try:
        return decimal(Decimal(value), "observer amount")
    except (InvalidFinancialInput, InvalidOperation, ValueError):
        raise MarketDataInvalid("invalid financial decimal") from None


def timestamp(value: object) -> datetime:
    try:
        return utc_timestamp(datetime.fromisoformat(text(value)))
    except (ValueError, InvalidFinancialInput):
        raise MarketDataInvalid("invalid UTC timestamp") from None


def integer(value: object) -> int:
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    if type(value) is int and value >= 0:
        return value
    raise MarketDataInvalid("invalid integer")


def decode(body: bytes) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for k, v in values:
            if k in result:
                raise ValueError
            result[k] = v
        return result

    try:
        return obj(json.loads(body, object_pairs_hook=pairs))
    except (ValueError, RecursionError, UnicodeError):
        raise MarketDataInvalid("invalid response JSON") from None


def market(payload: object, book: str) -> tuple[MarketLimits, ShadowFee]:
    matches = [obj(row) for row in array(payload) if obj(row).get("book") == book]
    if len(matches) != 1:
        raise MarketDataInvalid("book unavailable or duplicated")
    row = matches[0]
    limits = MarketLimits(
        book,
        *(
            number(row.get(k))
            for k in (
                "minimum_amount",
                "maximum_amount",
                "minimum_price",
                "maximum_price",
                "minimum_value",
                "maximum_value",
                "tick_size",
            )
        ),
    )
    # Public tier rates are documented as decimals. Conservatively select the
    # largest taker rate; do not infer the account's volume tier or use flat_rate.
    tiers = array(obj(row.get("fees")).get("structure"))
    if not tiers:
        raise MarketDataInvalid("public fee schedule unavailable")
    fee = ShadowFee(
        max(number(obj(t).get("taker")) for t in tiers), FeeSource.PUBLIC_SCHEDULE_FEE
    )
    return limits, fee


def books(payload: object) -> tuple[BookConstraints, ...]:
    """All available books, in exchange order. Discovery is dynamic, never hardcoded."""
    result = []
    for row in (obj(item) for item in array(payload)):
        book = row.get("book")
        if not isinstance(book, str) or not book:
            raise MarketDataInvalid("invalid book name")
        result.append(BookConstraints(
            book,
            *(number(row.get(k)) for k in (
                "minimum_amount", "maximum_amount", "minimum_price", "maximum_price",
                "minimum_value", "maximum_value", "tick_size"))))
    if len({item.book for item in result}) != len(result):
        raise MarketDataInvalid("duplicate available book")
    return tuple(result)


def ticker(payload: object, book: str) -> Ticker:
    row = obj(payload)
    if row.get("book") != book:
        raise MarketDataInvalid("wrong ticker book")
    return Ticker(book, number(row.get("last")), number(row.get("bid")), number(row.get("ask")),
                  number(row.get("high")), number(row.get("low")), number(row.get("volume")),
                  number(row.get("vwap")), number(row.get("change_24") if row.get("change_24") is not None
                                                  else row.get("change24")))


def balances(payload: object) -> tuple[ObservedBalance, ...]:
    result = tuple(
        ObservedBalance(
            text(r.get("currency")),
            number(r.get("total")),
            number(r.get("locked")),
            number(r.get("available")),
        )
        for r in (obj(row) for row in array(obj(payload).get("balances")))
    )
    if len({b.currency for b in result}) != len(result):
        raise MarketDataInvalid("duplicate observed currency")
    return result


def fees(payload: object, book: str) -> ShadowFee:
    matches = [
        obj(row)
        for row in array(obj(payload).get("fees"))
        if obj(row).get("book") == book
    ]
    if len(matches) != 1:
        raise MarketDataInvalid("account fee unavailable")
    return ShadowFee(
        number(matches[0].get("taker_fee_decimal")), FeeSource.CONFIRMED_ACCOUNT_FEE
    )


def trades(payload: object, book: str) -> tuple[PublicTrade, ...]:
    result = []
    for item in array(payload):
        row = obj(item)
        if row.get("book") != book:
            raise MarketDataInvalid("wrong public trade book")
        result.append(
            PublicTrade(
                book,
                integer(row.get("tid")),
                timestamp(row.get("created_at")),
                number(row.get("price")),
                number(row.get("amount")),
                text(row.get("maker_side")),
            )
        )
    return tuple(result)


def depth(payload: object, book: str) -> OrderBookSnapshot:
    row = obj(payload)
    sides = []
    for side in ("bids", "asks"):
        levels = []
        for value in array(row.get(side)):
            level = obj(value)
            if level.get("book") != book:
                raise MarketDataInvalid("wrong depth book")
            levels.append(
                Level(number(level.get("price")), number(level.get("amount")))
            )
        sides.append(
            tuple(sorted(levels, key=lambda level: level.price, reverse=side == "bids"))
        )
    return OrderBookSnapshot(
        book,
        timestamp(row.get("updated_at")),
        integer(row.get("sequence")),
        sides[0],
        sides[1],
    )

"""Untrusted Bitso wire data enters the domain only here."""

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from autofund.decimal_utils import decimal
from autofund.errors import InvalidFinancialInput
from autofund.models import Side

from .errors import ExchangeInvariantError
from .models import (
    Book,
    ExchangeBalance,
    ExchangeTradeFill,
    FeeSchedule,
    OrderState,
    RemoteOrder,
    book_name,
    identifier,
)


def obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ExchangeInvariantError("expected object")
    return cast(dict[str, object], value)


def array(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ExchangeInvariantError("expected array")
    return cast(list[object], value)


def text(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ExchangeInvariantError("invalid text field")
    return value


def number(value: object) -> Decimal:
    if not isinstance(value, str):
        raise ExchangeInvariantError("financial field must be decimal string")
    try:
        return decimal(Decimal(value), "exchange amount")
    except (InvalidOperation, ValueError, InvalidFinancialInput):
        raise ExchangeInvariantError("invalid financial decimal") from None


def decode(body: bytes) -> dict[str, object]:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        return obj(json.loads(body, object_pairs_hook=pairs))
    except (ValueError, UnicodeError, RecursionError):
        raise ExchangeInvariantError("malformed exchange JSON") from None


def books(payload: object) -> tuple[Book, ...]:
    result = []
    for item in array(payload):
        row = obj(item)
        result.append(
            Book(
                text(row.get("book")),
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
        )
    if len({b.book for b in result}) != len(result):
        raise ExchangeInvariantError("duplicate book metadata")
    return tuple(result)


def balances(payload: object) -> tuple[ExchangeBalance, ...]:
    result = tuple(
        ExchangeBalance(
            text(r.get("currency")),
            number(r.get("total")),
            number(r.get("locked")),
            number(r.get("available")),
        )
        for r in (obj(i) for i in array(obj(payload).get("balances")))
    )
    if len({b.currency for b in result}) != len(result):
        raise ExchangeInvariantError("duplicate balance currency")
    return result


def fees(payload: object) -> tuple[FeeSchedule, ...]:
    return tuple(
        FeeSchedule(
            text(r.get("book")),
            number(r.get("maker_fee_decimal")),
            number(r.get("taker_fee_decimal")),
            number(r.get("current_volume")),
        )
        for r in (obj(i) for i in array(obj(payload).get("fees")))
    )


def side(value: object) -> Side:
    if value not in ("buy", "sell"):
        raise ExchangeInvariantError("unknown order side")
    return Side.BUY if value == "buy" else Side.SELL


def spot(row: dict[str, object]) -> None:
    if (
        row.get("margin_order_type")
        or row.get("settle_major")
        or row.get("settle_minor")
    ):
        raise ExchangeInvariantError("unsupported settlement or order mode")


def orders(payload: object) -> tuple[RemoteOrder, ...]:
    states = {
        "queued": OrderState.ACKNOWLEDGED,
        "open": OrderState.OPEN,
        "partially filled": OrderState.PARTIALLY_FILLED,
        "completed": OrderState.COMPLETED,
        "cancelled": OrderState.CANCELLED,
    }
    result = []
    for item in array(payload):
        row = obj(item)
        spot(row)
        state = states.get(text(row.get("status")))
        if state is None:
            raise ExchangeInvariantError("unknown remote order state")
        original, unfilled = (
            number(row.get("original_amount")),
            number(row.get("unfilled_amount")),
        )
        # Bitso omits/nulls the limit price for completed market orders. The
        # execution fills remain the authoritative price evidence.
        price = number(row["price"]) if row.get("price") is not None else Decimal("0")
        if original < 0 or not 0 <= unfilled <= original or price < 0:
            raise ExchangeInvariantError("invalid remaining order amounts")
        result.append(
            RemoteOrder(
                identifier(text(row.get("oid"))),
                text(row["origin_id"]) if row.get("origin_id") else None,
                book_name(text(row.get("book"))),
                side(row.get("side")),
                state,
                original,
                unfilled,
                price,
            )
        )
    return tuple(result)


def fills(payload: object) -> tuple[ExchangeTradeFill, ...]:
    result = []
    for item in array(payload):
        row = obj(item)
        spot(row)
        book = book_name(text(row.get("book")))
        major_currency, minor_currency = book.split("_")
        if (
            row.get("major_currency") != major_currency
            or row.get("minor_currency") != minor_currency
        ):
            raise ExchangeInvariantError("fill currency mismatch")
        direction = side(row.get("side"))
        major, minor, price = (number(row.get(k)) for k in ("major", "minor", "price"))
        if (
            price <= 0
            or (direction is Side.BUY and not (major > 0 and minor < 0))
            or (direction is Side.SELL and not (major < 0 and minor > 0))
        ):
            raise ExchangeInvariantError("fill settlement signs inconsistent")
        try:
            stamp = datetime.fromisoformat(text(row.get("created_at")))
            if stamp.tzinfo is None:
                raise ValueError
        except ValueError:
            raise ExchangeInvariantError("invalid fill timestamp") from None
        fee = number(row["fees_amount"]) if row.get("fees_amount") is not None else None
        maker = row.get("maker_side")
        if maker is not None and maker not in ("buy", "sell"):
            raise ExchangeInvariantError("invalid maker side")
        result.append(
            ExchangeTradeFill(
                identifier(text(row.get("tid"))),
                identifier(text(row.get("oid"))),
                text(row["origin_id"]) if row.get("origin_id") else None,
                book,
                direction,
                abs(major),
                abs(minor),
                price,
                stamp.astimezone(UTC),
                maker == row.get("side") if maker is not None else None,
                # Production and Stage have emitted opposite signs for this
                # debit field. Accounting consumes the confirmed magnitude;
                # fee currency determines whether it reduces base or quote.
                abs(fee) if fee is not None else None,
                text(row["fees_currency"]) if row.get("fees_currency") else None,
            )
        )
    return tuple(result)

"""Strict decoding of Alpaca bar responses. Prices are Decimal from the first byte."""

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import NoReturn

from autofund.errors import InvalidFinancialInput
from autofund.replay.data import Candle
from autofund.replay.errors import DatasetValidationError, ReplayValidationError

from .errors import AlpacaParseError, AlpacaValidationError
from .models import Quote, validate_symbol


def _reject_constant(_name: str) -> NoReturn:
    raise AlpacaParseError("non-finite number in response")


def decode(body: bytes) -> dict[str, object]:
    try:
        # parse_float keeps every fractional price as an exact Decimal; a float never exists.
        value = json.loads(body, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise AlpacaParseError("response was not valid JSON") from None
    if not isinstance(value, dict):
        raise AlpacaParseError("response was not an object")
    return value


def _number(value: object) -> Decimal:
    if isinstance(value, bool):
        raise AlpacaParseError("boolean where a number was expected")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    raise AlpacaParseError("number expected")


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise AlpacaParseError("bar timestamp must be a UTC string")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise AlpacaParseError("bar timestamp was malformed") from None


def bar(row: object) -> Candle:
    if not isinstance(row, dict):
        raise AlpacaParseError("bar was not an object")
    try:
        return Candle(
            timestamp=_timestamp(row.get("t")),
            open=_number(row.get("o")),
            high=_number(row.get("h")),
            low=_number(row.get("l")),
            close=_number(row.get("c")),
            volume=_number(row.get("v")),
        )
    except (DatasetValidationError, ReplayValidationError, InvalidFinancialInput, InvalidOperation):
        raise AlpacaParseError("bar failed OHLCV validation") from None


def page(body: bytes) -> tuple[dict[str, list[Candle]], str | None]:
    """One response page: bars by symbol, and the continuation token if there is one."""
    payload = decode(body)
    raw = payload.get("bars")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AlpacaParseError("bars was not an object")
    out: dict[str, list[Candle]] = {}
    for symbol, rows in raw.items():
        try:
            validate_symbol(symbol)
        except AlpacaValidationError:
            raise AlpacaParseError("response contained an invalid symbol") from None
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise AlpacaParseError("bars for a symbol were not a list")
        out[symbol] = [bar(row) for row in rows]
    token = payload.get("next_page_token")
    if token is not None and (not isinstance(token, str) or not token):
        raise AlpacaParseError("page token was malformed")
    return out, token


def quotes(body: bytes) -> dict[str, Quote]:
    raw = decode(body).get("quotes")
    if not isinstance(raw, dict):
        raise AlpacaParseError("quotes was not an object")
    out: dict[str, Quote] = {}
    for symbol, row in raw.items():
        if not isinstance(row, dict):
            raise AlpacaParseError("quote was not an object")
        try:
            out[symbol] = Quote(
                symbol=symbol,
                at=_timestamp(row.get("t")),
                bid=_number(row.get("bp")),
                ask=_number(row.get("ap")),
            )
        except AlpacaValidationError:
            raise AlpacaParseError("quote failed validation") from None
    return out

"""Daily-bar series with explicit provenance.

Adjustment mode and feed are part of a series' identity. They are required wherever a series is
requested and are folded into its fingerprint, so a split-adjusted series can never be mistaken for
a raw one, and a result obtained on the partial IEX feed is always labelled as such.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from autofund.replay.data import Candle
from autofund.replay.serialization import decimal_string, fingerprint

from .errors import AlpacaValidationError

SERIES_VERSION = "autofund.alpaca-daily-bars.v1"

ADJUSTMENTS: tuple[str, ...] = ("raw", "split", "dividend", "all")
FEEDS: tuple[str, ...] = ("iex", "sip")

_SYMBOL = re.compile(r"[A-Z]{1,5}(?:\.[A-Z])?\Z")


def validate_symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
        raise AlpacaValidationError("symbol must be 1-5 uppercase letters")
    return symbol


def validate_adjustment(value: str) -> str:
    if value not in ADJUSTMENTS:
        raise AlpacaValidationError("adjustment must be one of " + ",".join(ADJUSTMENTS))
    return value


def validate_feed(value: str) -> str:
    if value not in FEEDS:
        raise AlpacaValidationError("feed must be one of " + ",".join(FEEDS))
    return value


@dataclass(frozen=True, slots=True)
class AlpacaBarSeries:
    """Real market data for one symbol, with the facts needed to scope any claim made from it.

    ``gaps`` counts weekdays on which at least one *peer* symbol traded but this one has no bar: a
    confirmed missing bar. ``unclassified_weekdays`` counts weekdays missing from every symbol in
    the request, which is a market closure in almost every case but cannot be told apart from a
    provider outage without an external calendar. The two are never merged.
    """

    symbol: str
    adjustment: str
    feed: str
    candles: tuple[Candle, ...]
    gaps: int = 0
    unclassified_weekdays: int = 0

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        validate_adjustment(self.adjustment)
        validate_feed(self.feed)
        if not isinstance(self.candles, tuple) or not self.candles:
            raise AlpacaValidationError("a series needs at least one bar")
        previous = None
        for candle in self.candles:
            if previous is not None and candle.timestamp <= previous:
                raise AlpacaValidationError("bars must be strictly increasing")
            previous = candle.timestamp

    @property
    def is_real_market_data(self) -> bool:
        return True

    @property
    def fingerprint(self) -> str:
        return fingerprint(
            {
                "schema": SERIES_VERSION,
                "symbol": self.symbol,
                "adjustment": self.adjustment,
                "feed": self.feed,
                "bars": [
                    [c.timestamp.isoformat()]
                    + [decimal_string(v) for v in (c.open, c.high, c.low, c.close, c.volume)]
                    for c in self.candles
                ],
            }
        )

    def telemetry(self) -> dict[str, Any]:
        return {
            "version": SERIES_VERSION,
            "symbol": self.symbol,
            "adjustment": self.adjustment,
            "feed": self.feed,
            "bars": len(self.candles),
            "gaps": self.gaps,
            "unclassified_weekdays": self.unclassified_weekdays,
            "is_real_market_data": self.is_real_market_data,
            "dataset_fingerprint": self.fingerprint,
            "first_at": self.candles[0].timestamp.isoformat(),
            "last_at": self.candles[-1].timestamp.isoformat(),
        }


def _weekdays(first: date, last: date) -> set[date]:
    days: set[date] = set()
    current = first
    while current <= last:
        if current.weekday() < 5:
            days.add(current)
        current += timedelta(days=1)
    return days


def classify_gaps(
    bars: dict[str, tuple[Candle, ...]],
) -> dict[str, tuple[int, int]]:
    """Return ``{symbol: (gaps, unclassified_weekdays)}`` without any external calendar.

    A weekday that some symbol traded is a trading day, so a symbol missing it has a confirmed gap.
    A weekday that no requested symbol traded is reported separately and never counted as a gap,
    because a single-symbol request has no peer to prove the market was open.
    """
    sessions = {s: {c.timestamp.date() for c in candles} for s, candles in bars.items()}
    traded = set().union(*sessions.values()) if sessions else set()
    result: dict[str, tuple[int, int]] = {}
    for symbol, days in sessions.items():
        window = _weekdays(min(days), max(days))
        missing = window - days
        result[symbol] = (len(missing & traded), len(missing - traded))
    return result


@dataclass(frozen=True, slots=True)
class Quote:
    """A top-of-book quote. Crossed, locked or non-positive quotes are not constructible."""

    symbol: str
    at: datetime
    bid: Decimal
    ask: Decimal

    def __post_init__(self) -> None:
        validate_symbol(self.symbol)
        if self.at.tzinfo is None:
            raise AlpacaValidationError("quote time must be timezone-aware")
        if self.bid <= 0 or self.ask <= 0 or self.ask <= self.bid:
            raise AlpacaValidationError("quote is non-positive, locked or crossed")

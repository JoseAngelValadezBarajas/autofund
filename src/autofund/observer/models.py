import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from autofund.decimal_utils import decimal, financial
from autofund.replay.serialization import fingerprint, utc_timestamp

from .errors import MarketDataInvalid


def book_name(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]+_mxn", value):
        raise MarketDataInvalid("F4 requires a BASE/MXN book")
    return value


def any_book_name(value: str) -> str:
    """Generic BASE_QUOTE validation for read-only discovery.

    Discovery must be able to see every exchange book (including non-MXN ones)
    before the scanner filters to the MXN universe. F4's own trading models
    continue to require BASE/MXN via `book_name`.
    """
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9]+_[a-z0-9]+", value):
        raise MarketDataInvalid("invalid exchange book name")
    return value


class FeeSource(StrEnum):
    CONFIRMED_ACCOUNT_FEE = "CONFIRMED_ACCOUNT_FEE"
    PUBLIC_SCHEDULE_FEE = "PUBLIC_SCHEDULE_FEE"
    CONFIGURED_ESTIMATE = "CONFIGURED_ESTIMATE"


@dataclass(frozen=True)
class ShadowFee:
    rate: Decimal
    source: FeeSource

    def __post_init__(self) -> None:
        if not 0 <= decimal(self.rate, "fee") < 1 or not isinstance(
            self.source, FeeSource
        ):
            raise MarketDataInvalid("invalid fee provenance/rate")


@dataclass(frozen=True)
class MarketLimits:
    book: str
    minimum_amount: Decimal
    maximum_amount: Decimal
    minimum_price: Decimal
    maximum_price: Decimal
    minimum_value: Decimal
    maximum_value: Decimal
    tick_size: Decimal

    def __post_init__(self) -> None:
        book_name(self.book)
        for name in self.__dataclass_fields__:
            if name != "book" and decimal(getattr(self, name), name) <= 0:
                raise MarketDataInvalid("nonpositive market constraint")
        for field_name in ("amount", "price", "value"):
            if getattr(self, "minimum_" + field_name) > getattr(
                self, "maximum_" + field_name
            ):
                raise MarketDataInvalid("inverted market constraints")


@dataclass(frozen=True, slots=True)
class BookConstraints:
    """Exchange constraints for any book, used by read-only market discovery.

    Deliberately quote-agnostic so the MXN filter can be applied by the scanner
    rather than by the parser. No trading path accepts this type.
    """

    book: str
    minimum_amount: Decimal
    maximum_amount: Decimal
    minimum_price: Decimal
    maximum_price: Decimal
    minimum_value: Decimal
    maximum_value: Decimal
    tick_size: Decimal

    def __post_init__(self) -> None:
        any_book_name(self.book)
        for name in self.__dataclass_fields__:
            if name != "book" and decimal(getattr(self, name), name) <= 0:
                raise MarketDataInvalid("nonpositive market constraint")
        for field_name in ("amount", "price", "value"):
            if getattr(self, "minimum_" + field_name) > getattr(self, "maximum_" + field_name):
                raise MarketDataInvalid("inverted market constraints")

    @property
    def quote_currency(self) -> str:
        return self.book.split("_", 1)[1]


@dataclass(frozen=True, repr=False)
class ObservedBalance:
    currency: str
    total: Decimal = field(repr=False)
    locked: Decimal = field(repr=False)
    available: Decimal = field(repr=False)

    @financial
    def __post_init__(self) -> None:
        for name in ("total", "locked", "available"):
            if decimal(getattr(self, name), name) < 0:
                raise MarketDataInvalid("negative observed balance")
        if self.total - self.locked != self.available:
            raise MarketDataInvalid("observed balance invariant failed")

    def __repr__(self) -> str:
        return "ObservedBalance(amounts=REDACTED)"


@dataclass(frozen=True)
class PublicTrade:
    book: str
    trade_id: int
    timestamp: datetime
    price: Decimal
    amount: Decimal
    maker_side: str

    def __post_init__(self) -> None:
        book_name(self.book)
        utc_timestamp(self.timestamp)
        if type(self.trade_id) is not int or self.trade_id < 0:
            raise MarketDataInvalid("invalid public trade ID")
        if decimal(self.price, "price") <= 0 or decimal(self.amount, "amount") <= 0:
            raise MarketDataInvalid("nonpositive public trade")
        if self.maker_side not in ("buy", "sell"):
            raise MarketDataInvalid("invalid maker side")


@dataclass(frozen=True, slots=True)
class Ticker:
    """Public ticker summary. Read-only research input; never a trading signal."""

    book: str
    last: Decimal
    bid: Decimal
    ask: Decimal
    high: Decimal
    low: Decimal
    volume: Decimal
    vwap: Decimal
    change_24h: Decimal

    def __post_init__(self) -> None:
        book_name(self.book)
        for name in ("last", "bid", "ask", "high", "low", "vwap"):
            if decimal(getattr(self, name), name) <= 0:
                raise MarketDataInvalid("nonpositive ticker price")
        if decimal(self.volume, "volume") < 0:
            raise MarketDataInvalid("negative ticker volume")
        if decimal(self.change_24h, "change_24h") is None:
            raise MarketDataInvalid("invalid 24h change")


@dataclass(frozen=True)
class Level:
    price: Decimal
    amount: Decimal

    def __post_init__(self) -> None:
        if decimal(self.price, "price") <= 0 or decimal(self.amount, "amount") <= 0:
            raise MarketDataInvalid("invalid depth level")


@dataclass(frozen=True)
class OrderBookSnapshot:
    book: str
    timestamp: datetime
    sequence: int
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]

    def __post_init__(self) -> None:
        book_name(self.book)
        utc_timestamp(self.timestamp)
        if type(self.sequence) is not int or self.sequence < 0:
            raise MarketDataInvalid("invalid depth sequence")
        if (
            not isinstance(self.bids, tuple)
            or not isinstance(self.asks, tuple)
            or not self.bids
            or not self.asks
        ):
            raise MarketDataInvalid("two-sided depth required")
        if (
            tuple(sorted(self.bids, key=lambda level: level.price, reverse=True))
            != self.bids
            or tuple(sorted(self.asks, key=lambda level: level.price)) != self.asks
        ):
            raise MarketDataInvalid("depth must be price ordered")
        if any(
            len({level.price for level in levels}) != len(levels)
            for levels in (self.bids, self.asks)
        ):
            raise MarketDataInvalid("duplicate aggregated depth level")
        if self.best_bid >= self.best_ask:
            raise MarketDataInvalid("crossed or locked book")

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price

    @property
    @financial
    def midpoint(self) -> Decimal:
        return (self.best_bid + self.best_ask) / Decimal("2")

    @property
    @financial
    def spread_bps(self) -> Decimal:
        return (self.best_ask - self.best_bid) / self.midpoint * Decimal("10000")

    @property
    def fingerprint(self) -> str:
        return fingerprint(self)


@dataclass(frozen=True, slots=True)
class OhlcCandle:
    """One public OHLC bucket, as the exchange published it.

    `bucket_ms` is the exchange's own bucket identifier and is the authoritative
    time key: it is preserved verbatim so a candle cannot be silently re-labelled by
    a local clock. `opened_at` is derived from it purely for readability, and the
    replay layer uses `bucket_ms` for ordering, gap detection and deduplication.

    There is no future data in a bucket: `bucket_start_time` is the start of the
    interval, so a candle labelled T is fully known at T + the bucket duration and
    never before.
    """

    book: str
    bucket_ms: int
    opened_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @property
    def fingerprint(self) -> str:
        return fingerprint(self)

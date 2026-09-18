from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from autofund.decimal_utils import ZERO, decimal
from autofund.errors import InvalidFinancialInput
from autofund.replay.data import Candle
from autofund.replay.errors import DatasetValidationError, ReplayValidationError
from autofund.replay.serialization import utc_timestamp

from .errors import ConfigurationError, InvalidMessage

# Deliberately small subset of the official UTC Spot intervals.
INTERVAL_SECONDS = {"1s": 1, "1m": 60}


def validate_symbol(symbol: str) -> str:
    if (
        not isinstance(symbol, str)
        or not symbol
        or not symbol.isascii()
        or not symbol.isalnum()
        or symbol != symbol.upper()
    ):
        raise ConfigurationError("symbol must be uppercase ASCII alphanumeric")
    return symbol


def interval_seconds(interval: str) -> int:
    if interval not in INTERVAL_SECONDS:
        raise ConfigurationError("F2 supports only UTC 1s and 1m candles")
    return INTERVAL_SECONDS[interval]


@dataclass(frozen=True, slots=True)
class MarketInfo:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str
    price_tick_size: Decimal | None = None
    quantity_step_size: Decimal | None = None
    minimum_quantity: Decimal | None = None
    maximum_quantity: Decimal | None = None
    minimum_notional: Decimal | None = None
    maximum_notional: Decimal | None = None

    def __post_init__(self) -> None:
        for value in (self.symbol, self.base_asset, self.quote_asset):
            validate_symbol(value)
        if self.symbol != self.base_asset + self.quote_asset or not self.status:
            raise ConfigurationError("inconsistent market metadata")
        for name in (
            "price_tick_size",
            "quantity_step_size",
            "minimum_quantity",
            "maximum_quantity",
            "minimum_notional",
            "maximum_notional",
        ):
            value = getattr(self, name)
            if value is not None:
                try:
                    decimal(value, name)
                except InvalidFinancialInput as exc:
                    raise ConfigurationError(str(exc)) from exc
                if value < ZERO:
                    raise ConfigurationError(f"negative {name}")

    @property
    def market(self) -> str:
        return f"{self.base_asset}/{self.quote_asset}"


@dataclass(frozen=True, slots=True)
class LiveCandleUpdate:
    market: str
    interval: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_closed: bool
    source_event_time: datetime

    def __post_init__(self) -> None:
        try:
            parts = self.market.split("/")
            if len(parts) != 2:
                raise ConfigurationError("market must be BASE/QUOTE")
            for part in parts:
                validate_symbol(part)
            seconds = interval_seconds(self.interval)
            for name in ("open_time", "close_time", "source_event_time"):
                object.__setattr__(self, name, utc_timestamp(getattr(self, name)))
            if self.open_time.microsecond != 0 or (
                self.interval == "1m" and self.open_time.second != 0
            ):
                raise InvalidMessage("candle opening is not aligned to interval")
            if self.close_time != self.open_time + timedelta(
                seconds=seconds, milliseconds=-1
            ):
                raise InvalidMessage("incorrect candle close boundary")
            if type(self.is_closed) is not bool:
                raise InvalidMessage("is_closed must be boolean")
            if self.source_event_time < self.open_time or (
                self.is_closed and self.source_event_time < self.close_time
            ):
                raise InvalidMessage("event time precedes observable candle data")
            self.to_candle()
        except (
            ConfigurationError,
            DatasetValidationError,
            ReplayValidationError,
        ) as exc:
            raise InvalidMessage(str(exc)) from exc

    @property
    def identity(self) -> tuple[str, str, datetime]:
        return self.market, self.interval, self.open_time

    def to_candle(self) -> Candle:
        return Candle(
            self.open_time, self.open, self.high, self.low, self.close, self.volume
        )

    def closed_content(self) -> tuple[object, ...]:
        # Exchange emission time may differ when identical content is resent.
        return (
            self.identity,
            self.close_time,
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
        )


class Severity(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    FATAL = "fatal"


class QualityStatus(StrEnum):
    VALID = "VALID"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"


@dataclass(frozen=True, slots=True)
class SourceNotice:
    kind: str
    severity: Severity
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SourceItem:
    received_at: datetime
    event: LiveCandleUpdate | None = None
    raw_message: str | None = None
    notice: SourceNotice | None = None


@dataclass(frozen=True, slots=True)
class MarketDataGap:
    expected_open: datetime
    actual_open: datetime
    missing_candles: int


@dataclass(frozen=True, slots=True)
class SessionQualityReport:
    status: QualityStatus
    closed_candles: int
    duplicates: int
    gaps: int
    out_of_order: int
    invalid_messages: int
    reconnects: int
    stale_events: int
    issues: tuple[SourceNotice, ...]
    gap_events: tuple[MarketDataGap, ...]

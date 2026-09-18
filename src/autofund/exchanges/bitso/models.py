import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from autofund.decimal_utils import decimal, financial
from autofund.models import Side
from autofund.replay.strategy import OrderIntent

from .errors import BitsoValidationError, ExchangeInvariantError


class BitsoEnvironment(StrEnum):
    STAGE = "stage"


class OrderType(StrEnum):
    LIMIT = "limit"
    MARKET = "market"


class OrderState(StrEnum):
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN_SUBMISSION_OUTCOME"


class ExecutionHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"


class HaltReason(StrEnum):
    MANUAL_HALT = "MANUAL_HALT"
    RECONCILIATION_HALT = "RECONCILIATION_HALT"
    AUTH_HALT = "AUTH_HALT"
    ACCOUNTING_HALT = "ACCOUNTING_HALT"


def book_name(book: str) -> str:
    if not isinstance(book, str) or not re.fullmatch(r"[a-z0-9]+_[a-z0-9]+", book):
        raise BitsoValidationError("invalid book")
    return book


def origin_id(value: str | None = None) -> str:
    value = value or "af-stage-" + uuid.uuid4().hex[:31]
    if not re.fullmatch(r"af-stage-[a-zA-Z0-9_-]{1,31}", value):
        raise BitsoValidationError("invalid AutoFund origin_id")
    return value


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", value):
        raise BitsoValidationError("invalid remote identifier")
    if value == "all":
        raise BitsoValidationError("bulk operations prohibited")
    return value


@dataclass(frozen=True)
class Book:
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
                raise ExchangeInvariantError("nonpositive market constraint")
        for name in ("amount", "price", "value"):
            if getattr(self, "minimum_" + name) > getattr(self, "maximum_" + name):
                raise ExchangeInvariantError("inverted market limits")

    @property
    def major_currency(self) -> str:
        return self.book.split("_")[0]

    @property
    def minor_currency(self) -> str:
        return self.book.split("_")[1]


@dataclass(frozen=True)
class ExchangeBalance:
    currency: str
    total: Decimal
    locked: Decimal
    available: Decimal

    @financial
    def __post_init__(self) -> None:
        for name in ("total", "locked", "available"):
            if decimal(getattr(self, name), name) < 0:
                raise ExchangeInvariantError("negative balance")
        if self.available != self.total - self.locked:
            raise ExchangeInvariantError("balance identity mismatch")


@dataclass(frozen=True)
class FeeSchedule:
    book: str
    maker_fee_decimal: Decimal
    taker_fee_decimal: Decimal
    current_volume: Decimal

    def __post_init__(self) -> None:
        book_name(self.book)
        for name in ("maker_fee_decimal", "taker_fee_decimal", "current_volume"):
            if decimal(getattr(self, name), name) < 0:
                raise ExchangeInvariantError("negative fee or volume")
        if max(self.maker_fee_decimal, self.taker_fee_decimal) >= 1:
            raise ExchangeInvariantError("invalid fee rate")


@dataclass(frozen=True)
class ExecutionInstruction:
    """F1 intent + execution parameters, not a second financial intention."""

    intent: OrderIntent
    order_type: OrderType
    limit_price: Decimal | None = None
    post_only: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.order_type, OrderType):
            raise BitsoValidationError("invalid order type")
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None or decimal(self.limit_price, "price") <= 0:
                raise BitsoValidationError("limit requires positive price")
        elif self.limit_price is not None or self.post_only:
            raise BitsoValidationError("market cannot carry limit options")


@dataclass(frozen=True)
class BitsoOrderRequest:
    book: str
    side: Side
    order_type: OrderType
    origin_id: str
    major: Decimal | None = None
    minor: Decimal | None = None
    price: Decimal | None = None
    post_only: bool = False
    slippage_tolerance: Decimal | None = None

    def __post_init__(self) -> None:
        book_name(self.book)
        origin_id(self.origin_id)
        if not isinstance(self.side, Side) or not isinstance(
            self.order_type, OrderType
        ):
            raise BitsoValidationError("invalid side/type")
        if (self.major is None) == (self.minor is None):
            raise BitsoValidationError("exactly one major/minor required")
        for name in ("major", "minor", "price"):
            value = getattr(self, name)
            if value is not None and decimal(value, name) <= 0:
                raise BitsoValidationError("nonpositive order value")
        if self.order_type is OrderType.LIMIT:
            if (
                self.major is None
                or self.price is None
                or self.slippage_tolerance is not None
            ):
                raise BitsoValidationError("invalid limit fields")
        elif self.price is not None or self.post_only:
            raise BitsoValidationError("invalid market fields")
        if self.slippage_tolerance is not None:
            if not 0 <= decimal(self.slippage_tolerance, "slippage") <= 100:
                raise BitsoValidationError("invalid slippage")

    def payload(self) -> dict[str, str]:
        result = {
            "book": self.book,
            "side": self.side.value.lower(),
            "type": self.order_type.value,
            "origin_id": self.origin_id,
        }
        for name in ("major", "minor", "price", "slippage_tolerance"):
            value = getattr(self, name)
            if value is not None:
                result[name] = format(value, "f")
        if self.post_only:
            result["time_in_force"] = "postonly"
        return result


@dataclass(frozen=True)
class RemoteOrder:
    oid: str
    origin_id: str | None
    book: str
    side: Side
    state: OrderState
    original_amount: Decimal
    unfilled_amount: Decimal
    price: Decimal

    def __post_init__(self) -> None:
        identifier(self.oid)
        book_name(self.book)
        for name in ("original_amount", "unfilled_amount", "price"):
            if decimal(getattr(self, name), name) < 0:
                raise ExchangeInvariantError("negative remote order amount")
        if self.unfilled_amount > self.original_amount:
            raise ExchangeInvariantError("unfilled amount exceeds original")


@dataclass(frozen=True)
class ExchangeTradeFill:
    trade_id: str
    exchange_order_id: str
    origin_id: str | None
    book: str
    side: Side
    major_quantity: Decimal
    minor_value: Decimal
    price: Decimal
    timestamp: datetime
    is_maker: bool | None
    confirmed_fee: Decimal | None
    fee_currency: str | None

    def __post_init__(self) -> None:
        identifier(self.trade_id)
        identifier(self.exchange_order_id)
        book_name(self.book)
        if not isinstance(self.side, Side):
            raise ExchangeInvariantError("invalid fill side")
        for name in ("major_quantity", "minor_value", "price"):
            if decimal(getattr(self, name), name) <= 0:
                raise ExchangeInvariantError("nonpositive fill amount")
        if self.confirmed_fee is not None and decimal(self.confirmed_fee, "fee") < 0:
            raise ExchangeInvariantError("negative normalized fee")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ExchangeInvariantError("fill timestamp must be timezone aware")


@dataclass(frozen=True)
class ExecutionPolicy:
    allocated_mxn: Decimal = Decimal("50")
    deployment_fraction: Decimal = Decimal("0.50")
    single_order_cap_mxn: Decimal = Decimal("10")
    slippage_tolerance: Decimal | None = None
    reconciliation_attempts: int = 3
    polling_seconds: float = 2.0

    def __post_init__(self) -> None:
        for name in ("allocated_mxn", "deployment_fraction", "single_order_cap_mxn"):
            if decimal(getattr(self, name), name) <= 0:
                raise BitsoValidationError("invalid capital policy")
        if self.deployment_fraction > Decimal("0.5") or self.allocated_mxn > Decimal(
            "50"
        ):
            raise BitsoValidationError("F3 capital envelope exceeded")
        if self.slippage_tolerance is not None:
            if not 0 <= decimal(self.slippage_tolerance, "slippage") <= 100:
                raise BitsoValidationError("invalid slippage")
        if (
            not 1 <= self.reconciliation_attempts <= 10
            or not 1 <= self.polling_seconds <= 60
        ):
            raise BitsoValidationError("invalid bounded polling policy")

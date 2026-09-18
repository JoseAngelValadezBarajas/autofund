from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from .decimal_utils import ZERO, decimal, financial, market_name
from .errors import InvalidFinancialInput


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class EntryType(StrEnum):
    DEPOSIT = "DEPOSIT"
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class Position:
    market: str
    quantity: Decimal = ZERO
    cost_basis_mxn: Decimal = ZERO
    realized_pnl_mxn: Decimal = ZERO

    def __post_init__(self) -> None:
        market_name(self.market)
        for name in ("quantity", "cost_basis_mxn", "realized_pnl_mxn"):
            decimal(getattr(self, name), name)
        if self.quantity < ZERO or self.cost_basis_mxn < ZERO:
            raise InvalidFinancialInput("negative quantity or cost basis")
        if self.quantity == ZERO and self.cost_basis_mxn != ZERO:
            raise InvalidFinancialInput("closed position must have zero cost basis")

    @property
    @financial
    def average_unit_cost(self) -> Decimal:
        return self.cost_basis_mxn / self.quantity if self.quantity else ZERO


@dataclass(frozen=True, slots=True)
class Fill:
    market: str
    side: Side
    quantity: Decimal
    execution_price_mxn: Decimal
    gross_notional_mxn: Decimal
    fee_mxn: Decimal
    cash_delta_mxn: Decimal
    realized_pnl_mxn: Decimal
    rounding_adjustment_mxn: Decimal = ZERO

    def __post_init__(self) -> None:
        market_name(self.market)
        if not isinstance(self.side, Side):
            raise InvalidFinancialInput("invalid fill side")
        for name in (
            "quantity",
            "execution_price_mxn",
            "gross_notional_mxn",
            "fee_mxn",
            "cash_delta_mxn",
            "realized_pnl_mxn",
            "rounding_adjustment_mxn",
        ):
            decimal(getattr(self, name), name)
        if (
            self.quantity <= ZERO
            or self.execution_price_mxn <= ZERO
            or self.gross_notional_mxn <= ZERO
            or self.fee_mxn < ZERO
        ):
            raise InvalidFinancialInput("invalid fill amounts")


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    entry_id: int
    type: EntryType
    cash_delta_mxn: Decimal
    market: str | None = None
    asset_delta: Decimal = ZERO
    fee_mxn: Decimal = ZERO
    realized_pnl_mxn: Decimal = ZERO
    note: str = ""
    cost_basis_delta_mxn: Decimal = ZERO
    rounding_adjustment_mxn: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.market is not None:
            market_name(self.market)
        for name in (
            "cash_delta_mxn",
            "asset_delta",
            "fee_mxn",
            "realized_pnl_mxn",
            "cost_basis_delta_mxn",
            "rounding_adjustment_mxn",
        ):
            decimal(getattr(self, name), name)

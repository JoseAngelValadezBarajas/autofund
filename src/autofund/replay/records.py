from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from autofund.models import Fill, LedgerEntry, Position

from .strategy import OrderIntent


class EquityPhase(StrEnum):
    OPEN_AFTER_EXECUTION = "open_after_execution"
    CLOSE = "close"


class IntentStatus(StrEnum):
    FILLED = "filled"
    RISK_REJECTED = "risk_rejected"
    CANCELLED_END_OF_DATA = "cancelled_end_of_data"


@dataclass(frozen=True, slots=True)
class SignalRecord:
    signal_id: int
    timestamp: datetime
    intent: OrderIntent


@dataclass(frozen=True, slots=True)
class IntentOutcome:
    signal_id: int
    status: IntentStatus
    execution_timestamp: datetime | None
    reference_price_mxn: Decimal | None
    ledger_entry_id: int | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """Analytical projection linked to the authoritative F0 ledger entry."""

    signal_id: int
    ledger_entry_id: int
    signal_timestamp: datetime
    execution_timestamp: datetime
    reference_price_mxn: Decimal
    fill: Fill
    estimated_slippage_cost_mxn: Decimal


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One flat-to-flat episode, including additions and partial sales."""

    opening_timestamp: datetime
    closing_timestamp: datetime
    ledger_entry_ids: tuple[int, ...]
    realized_pnl_mxn: Decimal


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    phase: EquityPhase
    cash_mxn: Decimal
    deployed_value_mxn: Decimal
    equity_mxn: Decimal
    realized_pnl_mxn: Decimal
    unrealized_pnl_mxn: Decimal


@dataclass(frozen=True, slots=True)
class ReplayTrace:
    signals: tuple[SignalRecord, ...]
    outcomes: tuple[IntentOutcome, ...]
    trades: tuple[TradeRecord, ...]
    closed_trades: tuple[ClosedTrade, ...]
    equity_curve: tuple[EquityPoint, ...]
    ledger: tuple[LedgerEntry, ...]
    final_positions: tuple[Position, ...]

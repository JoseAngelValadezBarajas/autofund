"""Versioned public models; financial values remain exact decimal strings."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DashboardModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class Health(DashboardModel):
    demo_mode: bool = False
    application_status: Literal["RUNNING"] = "RUNNING"
    environment: Literal["BITSO PRODUCTION"] = "BITSO PRODUCTION"
    execution_mode: Literal["SHADOW"] = "SHADOW"
    production_trading: Literal["DISABLED"] = "DISABLED"
    production_write_capability: Literal["BLOCKED"] = "BLOCKED"
    market_data_status: Literal["VALID", "DEGRADED", "INVALID"]
    accounting: Literal["PASS", "FAIL"]
    last_market_event_at: datetime | None = None
    current_session_id: str | None = None


class Overview(DashboardModel):
    demo_mode: bool = False
    mode: Literal["SHADOW"] = "SHADOW"
    market: str
    strategy_id: str
    initial_shadow_equity_mxn: str
    current_shadow_equity_mxn: str
    realized_pnl_mxn: str
    unrealized_pnl_mxn: str
    net_pnl_mxn: str
    return_pct: str
    max_deployment_mxn: str
    currently_deployed_mxn: str
    available_deployment_mxn: str
    closed_trade_count: int
    winning_trades: int
    losing_trades: int
    total_fees_mxn: str
    spread_cost_mxn: str
    slippage_cost_mxn: str
    max_drawdown_pct: str
    market_quality: Literal["VALID", "DEGRADED", "INVALID"]
    risk_status: Literal["NORMAL", "HALTED"]
    last_update: datetime | None = None


class Position(DashboardModel):
    market: str
    quantity: str
    average_cost_mxn: str
    mark_price_mxn: str | None
    market_value_mxn: str
    unrealized_pnl_mxn: str
    realized_pnl_mxn: str


class Portfolio(DashboardModel):
    cash_mxn: str
    equity_mxn: str
    deployed_mxn: str
    available_deployment_mxn: str
    positions: list[Position]


class Candle(DashboardModel):
    timestamp: datetime
    open: str
    high: str
    low: str
    close: str
    volume: str
    state: Literal["CLOSED"] = "CLOSED"


class Market(DashboardModel):
    market: str
    last_price_mxn: str | None
    best_bid_mxn: str | None
    best_ask_mxn: str | None
    spread_mxn: str | None
    spread_bps: str | None
    order_book_age_seconds: str | None
    market_data_age_seconds: str | None
    last_closed_candle: Candle | None
    market_quality: Literal["VALID", "DEGRADED", "INVALID"]


class OrderLevel(DashboardModel):
    price_mxn: str
    amount: str


class OrderBook(DashboardModel):
    market: str
    timestamp: datetime | None
    bids: list[OrderLevel]
    asks: list[OrderLevel]


class Quality(DashboardModel):
    status: Literal["VALID", "DEGRADED", "INVALID"]
    benchmark_eligible: bool
    stale_snapshots: int
    out_of_order_trades: int
    gaps: int
    duplicate_trades: int
    contradictions: int
    reconnects: int
    invalid_payloads: int


class Activity(DashboardModel):
    timestamp: datetime | None
    event_type: str
    market: str | None = None
    detail: str


class LedgerEntry(DashboardModel):
    entry_id: int
    type: str
    market: str | None = None
    cash_delta_mxn: str
    asset_delta: str
    fee_mxn: str
    realized_pnl_mxn: str


class Signal(DashboardModel):
    timestamp: datetime
    decision: str
    intent: str | None = None


class ShadowFill(DashboardModel):
    timestamp: datetime | None
    side: str
    market: str
    quantity: str
    reference_price_mxn: str | None
    vwap_execution_price_mxn: str | None
    gross_mxn: str | None
    fee_mxn: str | None
    realized_pnl_mxn: str | None
    simulated: Literal[True] = True


class Risk(DashboardModel):
    status: Literal["NORMAL", "HALTED"]
    initial_capital_mxn: str
    current_equity_mxn: str
    max_deployment_pct: str
    max_deployment_mxn: str
    currently_deployed_mxn: str
    available_deployment_mxn: str
    single_order_cap_mxn: str
    minimum_market_value_mxn: str
    fee_rate: str
    potential_executability: Literal["EXECUTABLE", "MICRO_ORDER_NOT_EXECUTABLE"]


class SessionSummary(DashboardModel):
    session_id: str
    start: datetime
    end: datetime | None
    quality: Literal["VALID", "DEGRADED", "INVALID"]
    closed_candles: int
    signals: int
    fills: int
    initial_equity_mxn: str
    final_equity_mxn: str
    net_pnl_mxn: str
    result_fingerprint: str


class SessionDetail(SessionSummary):
    data_fingerprint: str
    strategy_fingerprint: str
    config_fingerprint: str


class Report(DashboardModel):
    period_start: datetime | None = None
    period_end: datetime | None = None
    initial_equity_mxn: str
    final_equity_mxn: str
    return_pct: str
    net_pnl_mxn: str
    fees_mxn: str
    closed_trades: int
    wins: int
    losses: int
    max_drawdown_pct: str
    quality: Literal["VALID", "DEGRADED", "INVALID"]


class Page(DashboardModel):
    items: list[DashboardModel]
    limit: int = Field(ge=1, le=200)
    offset: int = Field(ge=0)
    total: int = Field(ge=0)


class ApiError(DashboardModel):
    code: str
    message: str


class EquityPoint(DashboardModel):
    timestamp: datetime
    equity_mxn: str

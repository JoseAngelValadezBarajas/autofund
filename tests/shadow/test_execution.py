from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from autofund.capital import CapitalManager
from autofund.models import Side
from autofund.observer.models import FeeSource, Level, OrderBookSnapshot, ShadowFee
from autofund.replay.strategy import OrderIntent
from autofund.shadow.config import ShadowConfig
from autofund.shadow.execution import ShadowExecutionEngine, ShadowRejected
from autofund.wallet import Wallet


def engine(limits, fee, config=None):
    wallet = Wallet()
    wallet.deposit(D("50"))
    return ShadowExecutionEngine(wallet, config or ShadowConfig(), limits, fee)


def test_initial_virtual_capital_and_no_real_inventory(session):
    assert session.wallet.cash_mxn == D("50")
    assert CapitalManager().available_for_new_buys(session.wallet, {}) == D("25")
    assert not session.wallet.positions
    assert not hasattr(session.engine, "client")


def test_BUY_consumes_asks_vwap(limits, start):
    fee = ShadowFee(D("0"), FeeSource.CONFIGURED_ESTIMATE)
    simulator = engine(limits, fee, ShadowConfig(max_spread_bps=D("300")))
    book = OrderBookSnapshot(
        "btc_mxn",
        start,
        1,
        (Level(D("99"), D("1")),),
        (Level(D("100"), D("0.04")), Level(D("120"), D("1"))),
    )
    fill = simulator.execute(
        OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("10")), book, start
    ).fill
    assert fill.quantity == D("0.09")
    assert fill.gross_notional_mxn == D("10")
    assert fill.execution_price_mxn == D(
        "111.11111111111111111111111111111111111111111111111"
    )
    assert simulator.wallet.cash_mxn == D("40")


def test_SELL_consumes_bids(limits, fee, start):
    simulator = engine(limits, fee, ShadowConfig(max_spread_bps=D("300")))
    book = OrderBookSnapshot(
        "btc_mxn", start, 1, (Level(D("99"), D("1")),), (Level(D("100"), D("1")),)
    )
    bought = simulator.execute(
        OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("10")), book, start
    )
    quantity = bought.fill.quantity
    book = replace(
        book, sequence=2, bids=(Level(D("99"), D("0.04")), Level(D("90"), D("1")))
    )
    sold = simulator.execute(
        OrderIntent("BTC/MXN", Side.SELL, quantity=quantity), book, start
    )
    assert sold.fill.gross_notional_mxn == D("0.04") * D("99") + (
        quantity - D("0.04")
    ) * D("90")
    assert simulator.wallet.positions["BTC/MXN"].quantity == 0
    simulator.wallet.assert_invariants()


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("spread", "SPREAD_GUARD"),
        ("stale", "STALE_MARKET"),
        ("depth", "INSUFFICIENT_DEPTH"),
        ("tick", "TICK_INVALID"),
        ("minimum", "NO_EXECUTABLE_MICRO_ORDER"),
    ],
)
def test_golden_D_E_F_and_market_rejections(kind, reason, limits, fee, frames):
    snapshot = frames[0].depth
    now = frames[0].observed_at
    if kind == "spread":
        snapshot = replace(snapshot, asks=(Level(D("1200"), D("1")),))
    if kind == "stale":
        now += timedelta(seconds=60)
    if kind == "depth":
        snapshot = replace(snapshot, asks=(Level(D("1001"), D("0.00000001")),))
    if kind == "tick":
        snapshot = replace(snapshot, asks=(Level(D("1001.5"), D("1")),))
    if kind == "minimum":
        limits = replace(limits, minimum_value=D("10"))
    simulator = engine(limits, fee)
    with pytest.raises(ShadowRejected, match=reason):
        simulator.execute(
            OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("10")), snapshot, now
        )
    assert simulator.wallet.cash_mxn == D("50") and len(simulator.wallet.ledger) == 1


def test_no_short_no_real_balance_inventory(limits, fee, frames):
    simulator = engine(limits, fee)
    with pytest.raises(ShadowRejected, match="RISK"):
        simulator.execute(
            OrderIntent("BTC/MXN", Side.SELL, quantity=D("0.001")),
            frames[0].depth,
            frames[0].observed_at,
        )
    assert not simulator.wallet.positions


def test_additional_slippage_is_explicit_and_not_spread_double_count(
    limits, fee, frames
):
    simulator = engine(limits, fee, ShadowConfig(extra_slippage_bps=D("10")))
    result = simulator.execute(
        OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("10")),
        frames[0].depth,
        frames[0].observed_at,
    )
    assert result.additional_slippage_mxn > 0
    assert result.spread_cost_mxn == result.fill.quantity * D("1")
    assert result.depth_slippage_mxn == 0
    assert result.fill.execution_price_mxn == D("1002.001")
    assert result.fee_source == "CONFIGURED_ESTIMATE"


def test_no_lookahead_next_snapshot_and_roundtrip(session, frames):
    for frame in frames[:3]:
        session.process(frame)
    assert len(session.executions) == 0 and len(session.pending) == 1
    assert session.signals[-1]["decision"] == "BUY"
    session.process(frames[3])
    assert len(session.executions) == 1 and session.pending[0][0].side is Side.SELL
    session.process(frames[4])
    assert len(session.executions) == 2
    assert session.wallet.cash_mxn == D("49.7823960200")
    assert session.wallet.positions["BTC/MXN"].quantity == 0
    assert len(session.wallet.ledger) == 3
    result = session.result()
    assert result["metrics"]["net_pnl"] == D("-0.2176039800")
    assert result["metrics"]["fees"] == D("0.1978218000")
    assert result["metrics"]["max_drawdown"] == D("0.0043520796")


def test_closed_candles_only(session, frames):
    frame = replace(
        frames[0], observed_at=frames[0].trades[0].timestamp + timedelta(seconds=1)
    )
    frame = replace(frame, depth=replace(frame.depth, timestamp=frame.observed_at))
    session.process(frame)
    assert not session.candles and not session.signals

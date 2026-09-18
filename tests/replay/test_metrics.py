from datetime import UTC, datetime
from decimal import Decimal as D
from decimal import localcontext

import pytest

from autofund import Side
from autofund.replay import (
    EquityPhase,
    EquityPoint,
    OrderIntent,
    ReplayConfig,
    ReplayRunner,
    SimpleMeanReversionV0,
)
from autofund.replay.metrics import drawdown, exact_sum


def test_exact_fees_and_slippage_manual_oracle(mean_dataset):
    config = ReplayConfig(fee_rate=D("0.01"), slippage_bps=D("100"))
    result = ReplayRunner(config).run(
        dataset=mean_dataset,
        strategy=SimpleMeanReversionV0(allocation_fraction=D("0.20402")),
    )
    buy, sell = result.trace.trades
    assert buy.fill.quantity == D("0.1")
    assert buy.fill.execution_price_mxn == D("101")
    assert buy.fill.gross_notional_mxn == D("10.1")
    assert buy.fill.fee_mxn == D("0.101")
    assert buy.fill.cash_delta_mxn == D("-10.201")
    assert sell.fill.execution_price_mxn == D("99")
    assert sell.fill.fee_mxn == D("0.099")
    assert sell.fill.cash_delta_mxn == D("9.801")
    assert (
        buy.estimated_slippage_cost_mxn == sell.estimated_slippage_cost_mxn == D("0.1")
    )
    metrics = result.metrics
    assert metrics.initial_equity_mxn == D("50")
    assert metrics.final_equity_mxn == D("49.6")
    assert metrics.total_fees_mxn == D("0.2")
    assert metrics.estimated_slippage_cost_mxn == D("0.2")
    assert metrics.net_pnl_mxn == metrics.realized_pnl_mxn == D("-0.4")
    assert metrics.unrealized_pnl_mxn == D("0")
    assert metrics.cost_addback_pnl_mxn == D("0")
    assert metrics.return_fraction == D("-0.008")
    assert metrics.return_pct == D("-0.8")
    assert metrics.max_drawdown_mxn == D("0.4")
    assert metrics.max_drawdown_pct == D("0.8")
    assert metrics.ledger_entry_count == 3
    assert metrics.fill_count == 2
    assert metrics.closed_trade_count == metrics.losing_trades == 1
    assert metrics.accounting_rounding_mxn == D("0")
    assert metrics.total_fees_mxn == sum(
        (e.fee_mxn for e in result.trace.ledger), D("0")
    )


@pytest.mark.parametrize(
    "sale_price,pnl,wins,losses,even",
    [(110, "1", 1, 0, 0), (90, "-1", 0, 1, 0), (100, "0", 0, 0, 1)],
)
def test_closed_round_trip_counts(
    dataset_factory, script, zero_config, sale_price, pnl, wins, losses, even
):
    orders = (
        OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("10")),
        OrderIntent("BTC/MXN", Side.SELL, quantity=D("0.1")),
    )
    result = ReplayRunner(zero_config).run(
        dataset=dataset_factory([100, 100, sale_price]), strategy=script(orders)
    )
    assert result.metrics.closed_trade_count == 1
    assert result.metrics.fill_count == 2
    assert (
        result.metrics.winning_trades,
        result.metrics.losing_trades,
        result.metrics.breakeven_trades,
    ) == (wins, losses, even)
    assert result.trace.closed_trades[0].realized_pnl_mxn == D(pnl)


def test_additions_partial_sales_form_one_episode(dataset_factory, script, zero_config):
    orders = (
        OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("10")),
        OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("5")),
        OrderIntent("BTC/MXN", Side.SELL, quantity=D("0.05")),
        OrderIntent("BTC/MXN", Side.SELL, quantity=D("0.1")),
    )
    result = ReplayRunner(zero_config).run(
        dataset=dataset_factory([100, 100, 100, 120, 90]), strategy=script(orders)
    )
    assert result.metrics.fill_count == 4
    assert result.metrics.closed_trade_count == 1
    assert result.metrics.breakeven_trades == 1
    assert result.trace.closed_trades[0].ledger_entry_ids == (2, 3, 4, 5)
    assert result.trace.closed_trades[0].realized_pnl_mxn == D("0")
    assert result.metrics.final_equity_mxn == D("50")


def test_unclosed_partial_sale_not_counted_as_closed_trade(
    dataset_factory, script, zero_config
):
    orders = (
        OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("10")),
        OrderIntent("BTC/MXN", Side.SELL, quantity=D("0.05")),
    )
    result = ReplayRunner(zero_config).run(
        dataset=dataset_factory([100, 100, 120]), strategy=script(orders)
    )
    assert result.metrics.realized_pnl_mxn == D("1")
    assert result.metrics.unrealized_pnl_mxn == D("1")
    assert result.metrics.closed_trade_count == result.metrics.winning_trades == 0


def test_drawdown_peak_to_trough_on_equity_not_cash():
    curve = tuple(
        EquityPoint(
            datetime(2026, 1, 1, tzinfo=UTC),
            EquityPhase.CLOSE,
            D("50"),
            D(str(equity - 50)),
            D(str(equity)),
            D("0"),
            D("0"),
        )
        for equity in [100, 120, 90, 110]
    )
    assert drawdown(D("100"), curve) == (D("30"), D("25"))


def test_drawdown_includes_initial_equity_and_gap_open(
    dataset_factory, script, zero_config
):
    order = OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("25"))
    result = ReplayRunner(zero_config).run(
        dataset=dataset_factory([100, 100, 80], [100, 120, 100]),
        strategy=script((order,)),
    )
    assert result.metrics.max_drawdown_mxn == D(
        "10"
    )  # peak 55 -> open trough 45, then close 50
    assert result.metrics.final_equity_mxn == D("50")
    with localcontext() as context:
        context.prec = 50
        assert result.metrics.max_drawdown_pct == D("10") / D("55") * D("100")


def test_exact_aggregate_does_not_drop_small_fees():
    assert exact_sum((D("1"), D("1e-80"))) == D("1." + "0" * 79 + "1")


def test_reopened_episodes_count_their_own_pnl(dataset_factory, script, zero_config):
    buy = OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("10"))
    sell = OrderIntent("BTC/MXN", Side.SELL, quantity=D("0.1"))
    result = ReplayRunner(zero_config).run(
        dataset=dataset_factory([100, 100, 110, 100, 90]),
        strategy=script((buy, sell, buy, sell)),
    )
    assert tuple(t.realized_pnl_mxn for t in result.trace.closed_trades) == (
        D("1"),
        D("-1"),
    )
    assert result.metrics.closed_trade_count == 2
    assert result.metrics.winning_trades == result.metrics.losing_trades == 1
    assert result.metrics.net_pnl_mxn == D("0")


@pytest.mark.parametrize("value", [1.0, D("NaN"), D("Infinity")])
def test_exact_sum_rejects_nonfinite_and_float(value):
    from autofund.replay import ReplayValidationError

    with pytest.raises(ReplayValidationError):
        exact_sum((D("1"), value))

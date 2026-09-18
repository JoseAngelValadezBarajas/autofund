from dataclasses import FrozenInstanceError, fields, replace
from decimal import Decimal as D
from decimal import getcontext

import pytest

from autofund import Side
from autofund.replay import (
    EquityPhase,
    IntentStatus,
    OrderIntent,
    ReplayRunner,
    SimpleMeanReversionV0,
    StrategyContractError,
    StrategyIdentity,
)


def buy(amount="10"):
    return OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D(amount))


def sell(quantity="0.1"):
    return OrderIntent("BTC/MXN", Side.SELL, quantity=D(quantity))


def test_next_open_execution_exactly_once(dataset_factory, script, zero_config):
    data = dataset_factory([100, 200, 300, 400], [80, 250, 350, 450])
    result = ReplayRunner(zero_config).run(dataset=data, strategy=script((buy(),)))
    assert len(result.trace.signals) == len(result.trace.trades) == 1
    trade = result.trace.trades[0]
    assert trade.signal_timestamp == data.candles[0].timestamp
    assert trade.execution_timestamp == data.candles[1].timestamp
    assert trade.reference_price_mxn == D("200")
    assert trade.fill.execution_price_mxn == D("200")
    assert trade.fill.quantity == D("0.05")
    assert trade.ledger_entry_id == 2
    assert len(result.trace.ledger) == 2
    assert result.trace.equity_curve[2].phase == EquityPhase.OPEN_AFTER_EXECUTION
    assert result.trace.equity_curve[2].equity_mxn == D("50")
    assert result.trace.equity_curve[3].equity_mxn == D("52.5")
    altered = replace(data.candles[1], close=D("240"))
    changed = replace(data, candles=(data.candles[0], altered, *data.candles[2:]))
    assert (
        ReplayRunner(zero_config)
        .run(dataset=changed, strategy=script((buy(),)))
        .trace.trades
        == result.trace.trades
    )


def test_public_strategy_api_exposes_only_past_and_immutable_values(
    mean_dataset, zero_config
):
    class Spy:
        identity = StrategyIdentity("test_spy", "1")

        def __init__(self):
            self.contexts = []

        def on_candle(self, context):
            assert {item.name for item in fields(context)} == {
                "timestamp",
                "market",
                "history",
                "portfolio",
            }
            assert not any(
                hasattr(context, name)
                for name in (
                    "dataset",
                    "wallet",
                    "ledger",
                    "engine",
                    "clock",
                    "runner",
                    "future",
                )
            )
            assert isinstance(context.history, tuple)
            assert all(c.timestamp <= context.timestamp for c in context.history)
            assert context.history[-1].timestamp == context.timestamp
            with pytest.raises(IndexError):
                _ = context.history[len(context.history)]
            with pytest.raises(FrozenInstanceError):
                context.portfolio.cash_mxn = D("999")
            with pytest.raises(FrozenInstanceError):
                context.candle.close = D("999")
            assert not hasattr(context.portfolio, "deposit")
            self.contexts.append(context)
            return None

    spy = Spy()
    ReplayRunner(zero_config).run(dataset=mean_dataset, strategy=spy)
    for index, context in enumerate(spy.contexts):
        assert context.history == mean_dataset.candles[: index + 1]
    assert (
        len(spy.contexts[0].history) == 1
    )  # Retained snapshots never acquire future candles.


def test_changing_future_does_not_change_prefix_signals(mean_dataset, zero_config):
    strategy = SimpleMeanReversionV0()
    original = ReplayRunner(zero_config).run(dataset=mean_dataset, strategy=strategy)
    changed_candle = replace(mean_dataset.candles[-1], close=D("80"), low=D("79"))
    changed = replace(
        mean_dataset, candles=(*mean_dataset.candles[:-1], changed_candle)
    )
    other = ReplayRunner(zero_config).run(dataset=changed, strategy=strategy)
    boundary = mean_dataset.candles[-1].timestamp
    assert (
        tuple(s for s in other.trace.signals if s.timestamp < boundary)
        == original.trace.signals
    )
    assert other.trace.trades == original.trace.trades
    assert other.trace.equity_curve[:-2] == original.trace.equity_curve[:-2]


@pytest.mark.parametrize(
    "order,reason",
    [
        (buy("26"), "RiskRejected"),
        (buy("51"), "InsufficientFunds"),
        (buy("0.5"), "RiskRejected"),
        (buy("0"), "RiskRejected"),
        (sell(), "RiskRejected"),
    ],
)
def test_risk_rejections_are_recorded_not_executed(
    dataset_factory, script, zero_config, order, reason
):
    result = ReplayRunner(zero_config).run(
        dataset=dataset_factory([100] * 4), strategy=script((order,))
    )
    assert result.metrics.final_equity_mxn == D("50")
    assert result.metrics.fill_count == 0
    assert result.metrics.ledger_entry_count == 1
    assert len(result.trace.outcomes) == 1
    outcome = result.trace.outcomes[0]
    assert outcome.status == IntentStatus.RISK_REJECTED
    assert outcome.reason.startswith(reason)
    assert outcome.execution_timestamp is not None


def test_capital_revalidated_at_next_open(dataset_factory, script, zero_config):
    # 15 fits at signal time (50*.5 - 10); at next open only 10 fits (60*.5 - 20).
    result = ReplayRunner(zero_config).run(
        dataset=dataset_factory([100, 100, 200]),
        strategy=script((buy("10"), buy("15"))),
    )
    assert result.metrics.fill_count == 1
    assert result.trace.outcomes[1].status == IntentStatus.RISK_REJECTED


def test_deployment_limit_for_admitted_orders_on_flat_market(
    dataset_factory, script, zero_config
):
    result = ReplayRunner(zero_config).run(
        dataset=dataset_factory([100] * 6),
        strategy=script((buy("10"), buy("15"), buy("1"))),
    )
    assert result.metrics.max_deployed_fraction == D("0.5")
    assert result.metrics.max_deployed_mxn == D("25")
    assert result.trace.outcomes[-1].status == IntentStatus.RISK_REJECTED
    assert all(
        p.deployed_value_mxn / p.equity_mxn <= zero_config.max_deployment_fraction
        for p in result.trace.equity_curve
    )


def test_market_drift_can_exceed_limit_without_new_buy(
    dataset_factory, script, zero_config
):
    result = ReplayRunner(zero_config).run(
        dataset=dataset_factory([100, 100, 200, 200]),
        strategy=script((buy("25"), buy("1"))),
    )
    assert result.metrics.max_deployed_fraction > zero_config.max_deployment_fraction
    assert result.metrics.fill_count == 1
    assert result.trace.outcomes[1].status == IntentStatus.RISK_REJECTED


def test_end_of_data_marks_position_and_cancels_pending(
    dataset_factory, script, zero_config
):
    data = dataset_factory([100, 100], [100, 120])
    result = ReplayRunner(zero_config).run(
        dataset=data, strategy=script((buy(), sell()))
    )
    assert result.metrics.final_equity_mxn == D("52")
    assert result.metrics.realized_pnl_mxn == D("0")
    assert result.metrics.unrealized_pnl_mxn == D("2")
    assert result.trace.final_positions[0].quantity == D("0.1")
    assert result.trace.outcomes[-1].status == IntentStatus.CANCELLED_END_OF_DATA
    assert result.trace.outcomes[-1].execution_timestamp is None
    assert result.metrics.closed_trade_count == 0
    assert result.metrics.fill_count == 1


def test_single_candle_never_executes(dataset_factory, script, zero_config):
    result = ReplayRunner(zero_config).run(
        dataset=dataset_factory([100]), strategy=script((buy(),))
    )
    assert result.metrics.final_equity_mxn == D("50")
    assert result.trace.outcomes[0].status == IntentStatus.CANCELLED_END_OF_DATA
    assert result.metrics.fill_count == 0


def test_invalid_strategy_output_aborts(dataset_factory, zero_config):
    class Broken:
        identity = StrategyIdentity("bad", "1")

        def on_candle(self, context):
            return True

    with pytest.raises(StrategyContractError):
        ReplayRunner(zero_config).run(dataset=dataset_factory([100]), strategy=Broken())


def test_other_market_is_contract_error(dataset_factory, zero_config, script):
    with pytest.raises(StrategyContractError):
        ReplayRunner(zero_config).run(
            dataset=dataset_factory([100]),
            strategy=script((OrderIntent("ETH/MXN", Side.BUY, budget_mxn=D("1")),)),
        )


def test_callback_context_changes_cannot_affect_engine(
    dataset_factory, zero_config, script
):
    orders = (buy(), sell())
    normal = script(orders)

    class ChangesDecimalContext:
        identity = normal.identity

        def on_candle(self, context):
            getcontext().prec = 2
            return normal.on_candle(context)

    dataset = dataset_factory([100, 100, 110])
    expected = ReplayRunner(zero_config).run(dataset=dataset, strategy=normal)
    assert (
        ReplayRunner(zero_config).run(dataset=dataset, strategy=ChangesDecimalContext())
        == expected
    )


def test_changing_identity_aborts(dataset_factory, zero_config):
    class MutatesIdentity:
        identity = StrategyIdentity("mutable", "1")

        def on_candle(self, context):
            self.identity = StrategyIdentity("mutable", "2")
            return None

    with pytest.raises(StrategyContractError):
        ReplayRunner(zero_config).run(
            dataset=dataset_factory([100]), strategy=MutatesIdentity()
        )


def test_dummy_returns_same_intent_for_same_snapshot(mean_dataset):
    from autofund.replay import PortfolioSnapshot, StrategyContext

    candle = mean_dataset.candles[2]
    context = StrategyContext(
        candle.timestamp,
        mean_dataset.market,
        mean_dataset.candles[:3],
        PortfolioSnapshot(D("50"), D("0"), D("0"), D("50"), D("0")),
    )
    strategy = SimpleMeanReversionV0()
    first = strategy.on_candle(context)
    assert first == strategy.on_candle(context)
    assert first == OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("10"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"side": Side.BUY},
        {"side": Side.SELL},
        {"side": "BUY", "budget_mxn": D("1")},
        {"side": Side.BUY, "budget_mxn": D("1"), "quantity": D("1")},
        {"side": Side.SELL, "quantity": 1.0},
    ],
)
def test_order_intent_invalid_shape(kwargs):
    with pytest.raises(StrategyContractError):
        OrderIntent("BTC/MXN", **kwargs)

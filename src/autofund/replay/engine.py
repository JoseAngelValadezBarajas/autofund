from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from autofund.capital import CapitalManager
from autofund.decimal_utils import ZERO, financial
from autofund.errors import RiskRejected
from autofund.execution import PaperExecutionEngine
from autofund.models import Position, Side
from autofund.risk import RiskEngine
from autofund.wallet import Wallet

from .clock import ReplayClock
from .config import ReplayConfig
from .data import Candle, HistoricalDataset
from .errors import StrategyContractError
from .metrics import exact_sum
from .records import (
    ClosedTrade,
    EquityPhase,
    EquityPoint,
    IntentOutcome,
    IntentStatus,
    ReplayTrace,
    SignalRecord,
    TradeRecord,
)
from .strategy import OrderIntent, PortfolioSnapshot, Strategy, StrategyContext


@financial
def _call_strategy(strategy: Strategy, context: StrategyContext) -> OrderIntent | None:
    # A callback changing its Decimal context cannot leak it into execution/metrics.
    result = strategy.on_candle(context)
    if result is not None and not isinstance(result, OrderIntent):
        raise StrategyContractError("strategy must return one OrderIntent or None")
    if result is not None and result.market != context.market:
        raise StrategyContractError("F1 only supports the dataset market")
    return result


@financial
def _equity_point(
    wallet: Wallet,
    dataset: HistoricalDataset,
    clock: ReplayClock,
    price: Decimal,
    phase: EquityPhase,
) -> EquityPoint:
    deployed = wallet.deployed_value({dataset.market: price})
    position = wallet.positions.get(dataset.market, Position(dataset.market))
    return EquityPoint(
        clock.current,
        phase,
        wallet.cash_mxn,
        deployed,
        wallet.equity({dataset.market: price}),
        wallet.realized_pnl(),
        deployed - position.cost_basis_mxn,
    )


@dataclass(frozen=True, slots=True)
class ReplayEngine:
    config: ReplayConfig

    @financial
    def run(self, dataset: HistoricalDataset, strategy: Strategy) -> ReplayTrace:
        wallet = Wallet()
        wallet.deposit(self.config.initial_equity)
        execution = PaperExecutionEngine(
            wallet,
            CapitalManager(self.config.max_deployment_fraction),
            RiskEngine(self.config.minimum_order),
            fee_rate=self.config.fee_rate,
            slippage_bps=self.config.slippage_bps,
        )
        clock = ReplayClock()
        signals: list[SignalRecord] = []
        outcomes: list[IntentOutcome] = []
        trades: list[TradeRecord] = []
        closed: list[ClosedTrade] = []
        curve: list[EquityPoint] = []
        pending: SignalRecord | None = None
        episode_start: datetime | None = None
        episode_entries: list[int] = []
        episode_pnl: list[Decimal] = []
        history: tuple[Candle, ...] = ()
        for candle in dataset.candles:
            clock.advance(candle.timestamp)
            if pending is not None:
                signal, pending = pending, None
                intent = signal.intent
                previous = wallet.positions.get(
                    dataset.market, Position(dataset.market)
                )
                try:
                    if intent.side is Side.BUY:
                        assert (
                            intent.budget_mxn is not None
                        )  # Validated OrderIntent shape.
                        fill = execution.buy(
                            intent.market, intent.budget_mxn, candle.open
                        )
                    else:
                        assert intent.quantity is not None
                        fill = execution.sell(
                            intent.market, intent.quantity, candle.open
                        )
                except RiskRejected as exc:
                    outcomes.append(
                        IntentOutcome(
                            signal.signal_id,
                            IntentStatus.RISK_REJECTED,
                            clock.current,
                            candle.open,
                            reason=f"{type(exc).__name__}: {exc}",
                        )
                    )
                else:
                    entry = wallet.ledger[-1]
                    difference = (
                        fill.execution_price_mxn - candle.open
                        if fill.side is Side.BUY
                        else candle.open - fill.execution_price_mxn
                    )
                    trades.append(
                        TradeRecord(
                            signal.signal_id,
                            entry.entry_id,
                            signal.timestamp,
                            clock.current,
                            candle.open,
                            fill,
                            difference * fill.quantity,
                        )
                    )
                    outcomes.append(
                        IntentOutcome(
                            signal.signal_id,
                            IntentStatus.FILLED,
                            clock.current,
                            candle.open,
                            entry.entry_id,
                        )
                    )
                    if previous.quantity == ZERO:
                        episode_start = clock.current
                    episode_entries.append(entry.entry_id)
                    episode_pnl.append(entry.realized_pnl_mxn)
                    if wallet.positions[dataset.market].quantity == ZERO:
                        assert episode_start is not None
                        closed.append(
                            ClosedTrade(
                                episode_start,
                                clock.current,
                                tuple(episode_entries),
                                exact_sum(tuple(episode_pnl)),
                            )
                        )
                        episode_start = None
                        episode_entries = []
                        episode_pnl = []
            curve.append(
                _equity_point(
                    wallet,
                    dataset,
                    clock,
                    candle.open,
                    EquityPhase.OPEN_AFTER_EXECUTION,
                )
            )
            point = _equity_point(
                wallet, dataset, clock, candle.close, EquityPhase.CLOSE
            )
            curve.append(point)
            # The tuple contains ONLY fully observed candles. Never a sliceable dataset handle.
            history = (*history, candle)
            position = wallet.positions.get(dataset.market, Position(dataset.market))
            context = StrategyContext(
                clock.current,
                dataset.market,
                history,
                PortfolioSnapshot(
                    wallet.cash_mxn,
                    position.quantity,
                    position.cost_basis_mxn,
                    point.equity_mxn,
                    point.realized_pnl_mxn,
                ),
            )
            new_intent = _call_strategy(strategy, context)
            if new_intent is not None:
                pending = SignalRecord(len(signals) + 1, clock.current, new_intent)
                signals.append(pending)
            wallet.assert_invariants({dataset.market: candle.close})
        if pending is not None:
            outcomes.append(
                IntentOutcome(
                    pending.signal_id,
                    IntentStatus.CANCELLED_END_OF_DATA,
                    None,
                    None,
                    reason="end_of_data",
                )
            )
        return ReplayTrace(
            tuple(signals),
            tuple(outcomes),
            tuple(trades),
            tuple(closed),
            tuple(curve),
            wallet.ledger,
            tuple(wallet.positions.values()),
        )

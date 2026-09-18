from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from autofund.decimal_utils import financial
from autofund.models import Side
from autofund.observer.errors import MarketDataInvalid
from autofund.observer.models import MarketLimits, ShadowFee
from autofund.observer.source import MarketFrame, MarketNotice
from autofund.replay.data import Candle
from autofund.replay.serialization import fingerprint
from autofund.replay.strategy import (
    OrderIntent,
    PortfolioSnapshot,
    SimpleMeanReversionV0,
    StrategyContext,
)
from autofund.wallet import Wallet

from .aggregation import TradeCandleAggregator
from .config import ShadowConfig
from .execution import ShadowExecution, ShadowExecutionEngine, ShadowRejected


class ShadowHealth(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"


class ShadowSession:
    def __init__(
        self,
        config: ShadowConfig,
        limits: MarketLimits,
        fee: ShadowFee,
        start: datetime,
    ) -> None:
        self.config, self.limits, self.fee, self.start = config, limits, fee, start
        self.market = limits.book.replace("_", "/").upper()
        self.wallet = Wallet()
        self.wallet.deposit(
            config.initial_equity, "F4 virtual allocation; unrelated to real account"
        )
        self.engine = ShadowExecutionEngine(self.wallet, config, limits, fee)
        self.strategy = SimpleMeanReversionV0()  # Reference defaults, never optimized.
        self.aggregator = TradeCandleAggregator(limits.book, start)
        self.candles: list[Candle] = []
        self.signals: list[dict[str, object]] = []
        self.executions: list[ShadowExecution] = []
        self.rejections: list[dict[str, object]] = []
        self.equity_curve: list[dict[str, object]] = []
        self.pending: list[tuple[OrderIntent, datetime, int]] = []
        self.frames = 0
        self.quality = "VALID"
        self.halt_reason: str | None = None
        self.issues: Counter[str] = Counter()
        self.last_frame: MarketFrame | None = None
        self.data_chain = "0" * 64

    @property
    def health(self) -> ShadowHealth:
        if self.halt_reason:
            return ShadowHealth.HALTED
        return (
            ShadowHealth.DEGRADED
            if self.quality == "DEGRADED"
            else ShadowHealth.HEALTHY
        )

    def _reject(self, intent: OrderIntent, label: datetime, reason: str) -> None:
        self.rejections.append({"candle": label, "intent": intent, "reason": reason})

    @financial
    def process(self, frame: MarketFrame | MarketNotice) -> None:
        if self.halt_reason is not None:
            raise MarketDataInvalid("halted shadow session cannot accept more frames")
        self.frames += 1
        if isinstance(frame, MarketNotice):
            if frame.kind not in (
                "read_unavailable",
                "invalid_payload",
                "reconnect",
            ) or frame.severity not in ("DEGRADED", "INVALID"):
                raise MarketDataInvalid("invalid operational notice")
            self.issues[frame.kind] += 1
            self.quality = frame.severity
            if frame.severity == "INVALID":
                self.halt_reason = "MARKET_DATA_HALT"
            return
        # Local receipt latency is excluded from semantic data identity. Timing
        # decisions themselves are preserved in signals/rejections/closed candles.
        self.data_chain = fingerprint(
            {"previous": self.data_chain, "trades": frame.trades, "depth": frame.depth}
        )
        try:
            if self.last_frame is not None:
                if frame.observed_at <= self.last_frame.observed_at:
                    raise MarketDataInvalid("nonmonotonic frame time")
                if frame.depth.sequence < self.last_frame.depth.sequence:
                    raise MarketDataInvalid("snapshot sequence regressed")
                if (
                    frame.depth.sequence == self.last_frame.depth.sequence
                    and frame.depth != self.last_frame.depth
                ):
                    raise MarketDataInvalid("contradictory complete snapshot sequence")
            if frame.depth.book != self.limits.book:
                raise MarketDataInvalid("wrong snapshot book")
            if any(t.timestamp > frame.observed_at for t in frame.trades):
                raise MarketDataInvalid("future public trade")
            self.aggregator.ingest(frame.trades)
            if frame.retry_count:
                self.issues["reconnects"] += frame.retry_count
            age = (frame.observed_at - frame.depth.timestamp).total_seconds()
            if age < 0 or age > self.config.max_orderbook_age_seconds:
                self.issues["stale_orderbook"] += 1
            for intent, label, sequence in self.pending:
                if (
                    frame.depth.sequence <= sequence
                    or frame.depth.timestamp < label + timedelta(minutes=1)
                ):
                    self._reject(intent, label, "NOT_NEXT_ELIGIBLE_MARKET_STATE")
                    continue
                try:
                    execution = self.engine.execute(
                        intent, frame.depth, frame.observed_at
                    )
                    self.executions.append(execution)
                except ShadowRejected as exc:
                    self._reject(intent, label, str(exc))
            self.pending = []
            watermark = frame.observed_at - timedelta(
                seconds=self.config.closing_delay_seconds
            )
            closed = self.aggregator.close_until(watermark)
            for candle in closed:
                self.candles.append(candle)
                position = self.wallet.positions.get(self.market)
                marked_equity = self.wallet.equity({self.market: candle.close})
                portfolio = PortfolioSnapshot(
                    self.wallet.cash_mxn,
                    position.quantity if position else Decimal("0"),
                    position.cost_basis_mxn if position else Decimal("0"),
                    marked_equity,
                    self.wallet.realized_pnl(),
                )
                candidate = self.strategy.on_candle(
                    StrategyContext(
                        candle.timestamp, self.market, tuple(self.candles), portfolio
                    )
                )
                self.signals.append(
                    {
                        "candle": candle.timestamp,
                        "decision": candidate.side.value if candidate else "NO_ACTION",
                        "intent": candidate,
                    }
                )
                if candidate is not None:
                    # Backlog candles are still genuine closed observations, but
                    # never execute stale decisions at invented historical books.
                    if candle is not closed[-1]:
                        self._reject(
                            candidate, candle.timestamp, "BACKLOG_SIGNAL_NOT_EXECUTABLE"
                        )
                    else:
                        self.pending.append(
                            (candidate, candle.timestamp, frame.depth.sequence)
                        )
            self.wallet.assert_invariants()
            equity = self.wallet.equity({self.market: frame.depth.midpoint})
            deployed = self.wallet.deployed_value({self.market: frame.depth.midpoint})
            self.equity_curve.append(
                {
                    "timestamp": frame.depth.timestamp,
                    "equity": equity,
                    "deployed": deployed,
                    "deployment_fraction": deployed / equity
                    if equity
                    else Decimal("0"),
                }
            )
            if self.issues or self.aggregator.gaps or self.aggregator.out_of_order:
                self.quality = "DEGRADED"
            self.last_frame = frame
        except MarketDataInvalid:
            self.quality = "INVALID"
            self.halt_reason = "MARKET_DATA_HALT"
            self.issues["invalid_market_event"] += 1
            self.last_frame = frame
        except Exception:
            self.quality = "INVALID"
            self.halt_reason = "ACCOUNTING_HALT"
            raise

    @financial
    def result(self) -> dict[str, object]:
        equity = (
            self.equity_curve[-1]["equity"]
            if self.equity_curve
            else self.config.initial_equity
        )
        assert isinstance(equity, Decimal)
        peak, drawdown = self.config.initial_equity, Decimal("0")
        max_deployment = Decimal("0")
        max_deployed = Decimal("0")
        for point in self.equity_curve:
            value, deployment, deployed = (
                point["equity"],
                point["deployment_fraction"],
                point["deployed"],
            )
            assert (
                isinstance(value, Decimal)
                and isinstance(deployment, Decimal)
                and isinstance(deployed, Decimal)
            )
            peak = max(peak, value)
            drawdown = max(drawdown, (peak - value) / peak)
            max_deployment, max_deployed = (
                max(max_deployment, deployment),
                max(max_deployed, deployed),
            )
        fees = sum((e.fill.fee_mxn for e in self.executions), Decimal("0"))
        spread = sum((e.spread_cost_mxn for e in self.executions), Decimal("0"))
        depth = sum((e.depth_slippage_mxn for e in self.executions), Decimal("0"))
        extra = sum((e.additional_slippage_mxn for e in self.executions), Decimal("0"))
        closed_pnl = [
            e.fill.realized_pnl_mxn for e in self.executions if e.fill.side is Side.SELL
        ]
        metrics = {
            "initial_equity": self.config.initial_equity,
            "final_equity": equity,
            "net_pnl": equity - self.config.initial_equity,
            "fees": fees,
            "gross_trading_pnl": equity - self.config.initial_equity + fees,
            "spread_cost": spread,
            "depth_slippage": depth,
            "additional_slippage": extra,
            "return": equity / self.config.initial_equity - Decimal("1"),
            "max_drawdown": drawdown,
            "max_deployment": max_deployment,
            "max_deployed_mxn": max_deployed,
            "closed_trades": len(closed_pnl),
            "wins": sum(p > 0 for p in closed_pnl),
            "losses": sum(p < 0 for p in closed_pnl),
        }
        semantic = {
            "schema_version": "autofund.shadow.result.v1",
            "book": self.limits.book,
            "strategy_fingerprint": self.strategy.identity.fingerprint,
            "config_fingerprint": fingerprint(self.config),
            "data_fingerprint": self.data_chain,
            "limits": self.limits,
            "fee": self.fee,
            "candles": self.candles,
            "signals": self.signals,
            "executions": self.executions,
            "rejections": self.rejections,
            "ledger": self.wallet.ledger,
            "positions": dict(self.wallet.positions),
            "equity_curve": self.equity_curve,
            "metrics": metrics,
            "pending": [i for i, _, _ in self.pending],
            "quality": self.quality,
            "gaps": self.aggregator.gaps,
            "duplicate_trades": self.aggregator.duplicates,
            "out_of_order": self.aggregator.out_of_order,
            "halt_reason": self.halt_reason,
            "benchmark_candidate": self.quality == "VALID",
        }
        return semantic | {
            "result_fingerprint": fingerprint(semantic),
            "operational_issues": dict(self.issues),
        }

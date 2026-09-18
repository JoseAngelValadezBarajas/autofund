from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from autofund.decimal_utils import ONE, ZERO, decimal, financial, market_name
from autofund.errors import InvalidFinancialInput
from autofund.models import Side

from .data import Candle
from .errors import StrategyContractError
from .serialization import fingerprint

type Parameter = Decimal | str | int | bool


@dataclass(frozen=True, slots=True)
class StrategyIdentity:
    identifier: str
    version: str
    parameters: tuple[tuple[str, Parameter], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.identifier, str)
            or not self.identifier
            or not isinstance(self.version, str)
            or not self.version
        ):
            raise StrategyContractError(
                "strategy needs a stable identifier and version"
            )
        if not isinstance(self.parameters, tuple):
            raise StrategyContractError(
                "strategy parameters must be an immutable tuple"
            )
        keys = set()
        for item in self.parameters:
            if not isinstance(item, tuple) or len(item) != 2:
                raise StrategyContractError("invalid strategy parameter pair")
            key, value = item
            if not isinstance(key, str) or not key or key in keys:
                raise StrategyContractError(
                    "invalid or duplicate strategy parameter name"
                )
            if not isinstance(value, (Decimal, str, int, bool)):
                raise StrategyContractError(
                    "parameters must be immutable scalars; floats forbidden"
                )
            if isinstance(value, Decimal) and not value.is_finite():
                raise StrategyContractError(
                    "strategy Decimal parameters must be finite"
                )
            keys.add(key)
        object.__setattr__(self, "parameters", tuple(sorted(self.parameters)))

    @property
    def fingerprint(self) -> str:
        return fingerprint(
            {
                "schema": "autofund.strategy.v1",
                "id": self.identifier,
                "version": self.version,
                "parameters": dict(self.parameters),
            }
        )


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    cash_mxn: Decimal
    quantity: Decimal
    cost_basis_mxn: Decimal
    equity_mxn: Decimal
    realized_pnl_mxn: Decimal


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Value-only, past-only snapshot. No runner, wallet, clock, or dataset handle."""

    timestamp: datetime
    market: str
    history: tuple[Candle, ...]
    portfolio: PortfolioSnapshot

    @property
    def candle(self) -> Candle:
        return self.history[-1]


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """Unapproved candidate; BUY budget includes fees, SELL requests quantity."""

    market: str
    side: Side
    budget_mxn: Decimal | None = None
    quantity: Decimal | None = None

    def __post_init__(self) -> None:
        try:
            market_name(self.market)
            if (
                self.side is Side.BUY
                and self.quantity is None
                and self.budget_mxn is not None
            ):
                decimal(self.budget_mxn, "budget_mxn")
            elif (
                self.side is Side.SELL
                and self.budget_mxn is None
                and self.quantity is not None
            ):
                decimal(self.quantity, "quantity")
            else:
                raise StrategyContractError(
                    "intent must have exactly the amount for its side"
                )
        except InvalidFinancialInput as exc:
            raise StrategyContractError(str(exc)) from exc
        # Positivity/capital/ownership remain decisions of F0 RiskEngine.


class Strategy(Protocol):
    @property
    def identity(self) -> StrategyIdentity: ...

    def on_candle(self, context: StrategyContext) -> OrderIntent | None:
        """Pure deterministic callback: same context -> same unapproved candidate."""
        ...


@dataclass(frozen=True, slots=True)
class SimpleMeanReversionV0:
    window: int = 3
    entry_threshold: Decimal = Decimal("0.05")
    allocation_fraction: Decimal = Decimal("0.20")
    identity: StrategyIdentity = field(init=False)

    def __post_init__(self) -> None:
        if type(self.window) is not int or self.window < 2:
            raise StrategyContractError("window must be an integer >= 2")
        try:
            decimal(self.entry_threshold, "entry_threshold")
            decimal(self.allocation_fraction, "allocation_fraction")
        except InvalidFinancialInput as exc:
            raise StrategyContractError(str(exc)) from exc
        if (
            not ZERO <= self.entry_threshold < ONE
            or not ZERO < self.allocation_fraction <= ONE
        ):
            raise StrategyContractError("invalid threshold or allocation fraction")
        object.__setattr__(
            self,
            "identity",
            StrategyIdentity(
                "simple_mean_reversion",
                "0",
                (
                    ("window", self.window),
                    ("entry_threshold", self.entry_threshold),
                    ("allocation_fraction", self.allocation_fraction),
                ),
            ),
        )

    @financial
    def on_candle(self, context: StrategyContext) -> OrderIntent | None:
        if len(context.history) < self.window:
            return None
        mean = sum(
            (candle.close for candle in context.history[-self.window :]), ZERO
        ) / Decimal(self.window)
        if context.portfolio.quantity > ZERO:
            if context.candle.close >= mean:
                return OrderIntent(
                    context.market, Side.SELL, quantity=context.portfolio.quantity
                )
        elif context.candle.close < mean * (ONE - self.entry_threshold):
            return OrderIntent(
                context.market,
                Side.BUY,
                budget_mxn=context.portfolio.equity_mxn * self.allocation_fraction,
            )
        return None

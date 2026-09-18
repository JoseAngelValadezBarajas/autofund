from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal

from autofund.capital import CapitalManager
from autofund.decimal_utils import financial
from autofund.errors import RiskRejected
from autofund.models import Fill, Side
from autofund.observer.models import Level, MarketLimits, OrderBookSnapshot, ShadowFee
from autofund.replay.strategy import OrderIntent
from autofund.risk import RiskEngine
from autofund.wallet import Wallet

from .config import ShadowConfig


class ShadowRejected(Exception):
    pass


@dataclass(frozen=True)
class ShadowExecution:
    fill: Fill
    snapshot_id: str
    spread_cost_mxn: Decimal
    depth_slippage_mxn: Decimal
    additional_slippage_mxn: Decimal
    fee_source: str


class ShadowExecutionEngine:
    """No network handle, no exchange execution port, only a virtual Wallet."""

    def __init__(
        self, wallet: Wallet, config: ShadowConfig, limits: MarketLimits, fee: ShadowFee
    ) -> None:
        self.wallet, self.config, self.limits, self.fee = wallet, config, limits, fee
        self.capital = CapitalManager(config.max_deployment)
        self.risk = RiskEngine(limits.minimum_value)

    @financial
    def execute(
        self, intent: OrderIntent, snapshot: OrderBookSnapshot, at: datetime
    ) -> ShadowExecution:
        if (
            intent.market != snapshot.book.replace("_", "/").upper()
            or snapshot.book != self.limits.book
        ):
            raise ShadowRejected("MARKET_MISMATCH")
        age = (at - snapshot.timestamp).total_seconds()
        if age < 0 or age > self.config.max_orderbook_age_seconds:
            raise ShadowRejected("SHADOW_EXECUTION_REJECTED_STALE_MARKET")
        if snapshot.spread_bps > self.config.max_spread_bps:
            raise ShadowRejected("SPREAD_GUARD")
        levels = snapshot.asks if intent.side is Side.BUY else snapshot.bids
        if any(
            level.price % self.limits.tick_size
            for level in (*snapshot.bids, *snapshot.asks)
        ):
            raise ShadowRejected("MARKET_TICK_INVALID")
        rate, extra = self.fee.rate, self.config.extra_slippage_bps / Decimal("10000")
        quantity = Decimal("0")
        gross_book = Decimal("0")
        try:
            if intent.side is Side.BUY:
                assert intent.budget_mxn is not None
                budget = intent.budget_mxn
                self.risk.check_buy(
                    self.wallet,
                    self.capital,
                    intent.market,
                    budget,
                    snapshot.midpoint,
                    {intent.market: snapshot.midpoint},
                )
                if (
                    budget > self.config.single_order_cap
                    or budget
                    + self.wallet.deployed_value({intent.market: snapshot.midpoint})
                    > self.config.initial_equity * self.config.max_deployment
                ):
                    raise ShadowRejected("RISK_ORDER_CAP")
                remainder = budget / (Decimal("1") + rate) / (Decimal("1") + extra)
                for level in levels:
                    notional = min(remainder, level.price * level.amount)
                    quantity += notional / level.price
                    gross_book += notional
                    remainder -= notional
                    if remainder == 0:
                        break
                if remainder > 0:
                    raise ShadowRejected("INSUFFICIENT_DEPTH")
                # A conservative derived-amount precision, not an invented lot step.
                quantity = quantity.quantize(
                    Decimal("0.00000001"), rounding=ROUND_FLOOR
                )
                gross_book = self._consume_quantity(quantity, levels)
            else:
                assert intent.quantity is not None
                quantity = intent.quantity
                self.risk.check_sell(
                    self.wallet, intent.market, quantity, snapshot.midpoint
                )
                if quantity * snapshot.midpoint > self.config.single_order_cap:
                    raise ShadowRejected("RISK_ORDER_CAP")
                gross_book = self._consume_quantity(quantity, levels)
        except RiskRejected:
            raise ShadowRejected("RISK_REJECTED") from None
        if not self.limits.minimum_amount <= quantity <= self.limits.maximum_amount:
            raise ShadowRejected("MARKET_AMOUNT_LIMIT")
        gross = gross_book * (
            Decimal("1") + extra if intent.side is Side.BUY else Decimal("1") - extra
        )
        if not self.limits.minimum_value <= gross <= self.limits.maximum_value:
            raise ShadowRejected("NO_EXECUTABLE_MICRO_ORDER")
        price = gross / quantity
        if not self.limits.minimum_price <= price <= self.limits.maximum_price:
            raise ShadowRejected("MARKET_PRICE_LIMIT")
        fee = gross * rate
        buying = intent.side is Side.BUY
        cash = -gross - fee if buying else gross - fee
        removed = Decimal("0")
        if not buying:
            pos = self.wallet.positions[intent.market]
            removed = (
                pos.cost_basis_mxn
                if quantity == pos.quantity
                else pos.cost_basis_mxn * (quantity / pos.quantity)
            )
        fill = Fill(
            intent.market,
            intent.side,
            quantity,
            price,
            gross,
            fee,
            cash,
            Decimal("0") if buying else cash - removed,
        )
        self.wallet._apply_fill(fill, removed)
        spread_cost = quantity * (
            (snapshot.best_ask - snapshot.midpoint)
            if buying
            else (snapshot.midpoint - snapshot.best_bid)
        )
        depth_cost = (
            (gross_book - quantity * snapshot.best_ask)
            if buying
            else (quantity * snapshot.best_bid - gross_book)
        )
        return ShadowExecution(
            fill,
            snapshot.fingerprint,
            spread_cost,
            depth_cost,
            abs(gross - gross_book),
            self.fee.source.value,
        )

    @staticmethod
    @financial
    def _consume_quantity(quantity: Decimal, levels: tuple[Level, ...]) -> Decimal:
        remainder, value = quantity, Decimal("0")
        for level in levels:
            take = min(remainder, level.amount)
            value += take * level.price
            remainder -= take
            if remainder == 0:
                return value
        raise ShadowRejected("INSUFFICIENT_DEPTH")

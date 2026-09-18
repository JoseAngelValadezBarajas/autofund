from collections.abc import Mapping
from decimal import ROUND_DOWN, Decimal, localcontext

from .capital import CapitalManager
from .decimal_utils import ONE, ZERO, decimal, financial
from .errors import InvalidFinancialInput
from .models import Fill, Side
from .risk import RiskEngine
from .wallet import Wallet


class PaperExecutionEngine:
    def __init__(
        self,
        wallet: Wallet,
        capital: CapitalManager | None = None,
        risk: RiskEngine | None = None,
        *,
        fee_rate: Decimal = Decimal("0"),
        slippage_bps: Decimal = Decimal("0"),
    ) -> None:
        decimal(fee_rate, "fee_rate")
        decimal(slippage_bps, "slippage_bps")
        if not ZERO <= fee_rate < ONE or not ZERO <= slippage_bps < Decimal("10000"):
            raise InvalidFinancialInput("fee must be in [0,1), slippage in [0,10000)")
        self._wallet = wallet
        self._capital = capital if capital is not None else CapitalManager()
        self._risk = risk if risk is not None else RiskEngine()
        self._fee_rate = fee_rate
        self._slippage_bps = slippage_bps

    @financial
    def buy(
        self,
        market: str,
        budget_mxn: Decimal,
        reference_price: Decimal,
        *,
        marks: Mapping[str, Decimal] | None = None,
    ) -> Fill:
        snapshot = dict(marks or {})
        # The order reference is the mark for this market; other holdings need marks.
        snapshot[market] = reference_price
        self._risk.check_buy(
            self._wallet, self._capital, market, budget_mxn, reference_price, snapshot
        )
        price = reference_price * (ONE + self._slippage_bps / Decimal("10000"))
        with localcontext() as ctx:
            ctx.rounding = ROUND_DOWN
            quantity = budget_mxn / (price * (ONE + self._fee_rate))
            gross = quantity * price
            nominal_fee = gross * self._fee_rate
        # Explicit precision residual: fee includes it, so total cash equals budget.
        fee = budget_mxn - gross
        adjustment = fee - nominal_fee
        fill = Fill(
            market, Side.BUY, quantity, price, gross, fee, -budget_mxn, ZERO, adjustment
        )
        self._wallet._apply_fill(fill)
        return fill

    @financial
    def sell(self, market: str, quantity: Decimal, reference_price: Decimal) -> Fill:
        self._risk.check_sell(self._wallet, market, quantity, reference_price)
        price = reference_price * (ONE - self._slippage_bps / Decimal("10000"))
        gross = quantity * price
        fee = gross * self._fee_rate
        net = gross - fee
        position = self._wallet.positions[market]
        removed = (
            position.cost_basis_mxn
            if quantity == position.quantity
            else position.cost_basis_mxn * (quantity / position.quantity)
        )
        fill = Fill(market, Side.SELL, quantity, price, gross, fee, net, net - removed)
        self._wallet._apply_fill(fill, removed)
        return fill

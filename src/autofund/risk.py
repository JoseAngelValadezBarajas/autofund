from collections.abc import Mapping
from decimal import Decimal

from .capital import CapitalManager
from .decimal_utils import ZERO, decimal, financial, market_name
from .errors import InsufficientFunds, InvalidFinancialInput, RiskRejected
from .wallet import Wallet


class RiskEngine:
    def __init__(self, minimum_order: Decimal = Decimal("1")) -> None:
        decimal(minimum_order, "minimum_order")
        if minimum_order <= ZERO:
            raise InvalidFinancialInput("minimum_order must be positive")
        self._minimum_order = minimum_order

    @property
    def minimum_order(self) -> Decimal:
        return self._minimum_order

    @staticmethod
    def _positive(value: Decimal, name: str) -> None:
        decimal(value, name)
        if value <= ZERO:
            raise RiskRejected(f"{name} must be positive")

    @financial
    def check_buy(
        self,
        wallet: Wallet,
        capital: CapitalManager,
        market: str,
        budget_mxn: Decimal,
        reference_price: Decimal,
        marks: Mapping[str, Decimal],
    ) -> None:
        wallet.assert_invariants()
        market_name(market)
        self._positive(budget_mxn, "budget")
        self._positive(reference_price, "reference_price")
        if budget_mxn < self._minimum_order:
            raise RiskRejected("below minimum order")
        if budget_mxn > wallet.cash_mxn:
            raise InsufficientFunds("budget exceeds cash")
        if budget_mxn > capital.available_for_new_buys(wallet, marks):
            raise RiskRejected("budget exceeds deployable capital")

    @financial
    def check_sell(
        self, wallet: Wallet, market: str, quantity: Decimal, reference_price: Decimal
    ) -> None:
        wallet.assert_invariants()
        market_name(market)
        self._positive(quantity, "quantity")
        self._positive(reference_price, "reference_price")
        position = wallet.positions.get(market)
        if position is None or quantity > position.quantity:
            raise RiskRejected("quantity exceeds owned position")

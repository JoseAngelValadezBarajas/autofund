from collections.abc import Mapping
from decimal import Decimal

from .decimal_utils import ONE, ZERO, decimal, financial
from .errors import InvalidFinancialInput
from .wallet import Wallet


class CapitalManager:
    def __init__(self, max_deployment_fraction: Decimal = Decimal("0.50")) -> None:
        fraction = decimal(max_deployment_fraction, "max_deployment_fraction")
        if not ZERO <= fraction <= ONE:
            raise InvalidFinancialInput("deployment fraction must be in [0, 1]")
        self._fraction = fraction

    @property
    def max_deployment_fraction(self) -> Decimal:
        return self._fraction

    @financial
    def deployment_limit(self, wallet: Wallet, marks: Mapping[str, Decimal]) -> Decimal:
        return wallet.equity(marks) * self._fraction

    @financial
    def available_for_new_buys(
        self, wallet: Wallet, marks: Mapping[str, Decimal]
    ) -> Decimal:
        return min(
            wallet.cash_mxn,
            max(
                ZERO,
                self.deployment_limit(wallet, marks) - wallet.deployed_value(marks),
            ),
        )

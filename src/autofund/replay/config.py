from dataclasses import dataclass
from decimal import Decimal

from autofund.capital import CapitalManager
from autofund.decimal_utils import ONE, ZERO, decimal
from autofund.errors import InvalidFinancialInput
from autofund.risk import RiskEngine

from .errors import ReplayValidationError
from .serialization import fingerprint


@dataclass(frozen=True, slots=True)
class ReplayConfig:
    initial_equity: Decimal = Decimal("50")
    max_deployment_fraction: Decimal = Decimal("0.50")
    minimum_order: Decimal = Decimal("1")
    fee_rate: Decimal = Decimal("0.001")
    slippage_bps: Decimal = Decimal("5")
    schema_version: str = "autofund.replay.v1"
    base_currency: str = "MXN"
    execution_policy: str = "next_candle_open"
    end_of_data_policy: str = "cancel_pending_mark_final_close"

    def __post_init__(self) -> None:
        if (
            self.schema_version,
            self.base_currency,
            self.execution_policy,
            self.end_of_data_policy,
        ) != (
            "autofund.replay.v1",
            "MXN",
            "next_candle_open",
            "cancel_pending_mark_final_close",
        ):
            raise ReplayValidationError(
                "unsupported replay schema, currency, or lifecycle policy"
            )
        try:
            for name in (
                "initial_equity",
                "max_deployment_fraction",
                "minimum_order",
                "fee_rate",
                "slippage_bps",
            ):
                decimal(getattr(self, name), name)
            if (
                self.initial_equity <= ZERO
                or not ZERO <= self.fee_rate < ONE
                or not ZERO <= self.slippage_bps < Decimal("10000")
            ):
                raise ReplayValidationError("invalid equity, fee or slippage")
            CapitalManager(self.max_deployment_fraction)
            RiskEngine(self.minimum_order)
        except InvalidFinancialInput as exc:
            raise ReplayValidationError(str(exc)) from exc

    @property
    def fingerprint(self) -> str:
        return fingerprint(self)

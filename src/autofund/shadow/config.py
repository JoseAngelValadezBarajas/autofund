from dataclasses import dataclass
from decimal import Decimal

from autofund.decimal_utils import decimal
from autofund.observer.errors import MarketDataInvalid


@dataclass(frozen=True)
class ShadowConfig:
    initial_equity: Decimal = Decimal("50")
    max_deployment: Decimal = Decimal("0.50")
    single_order_cap: Decimal = Decimal("10")
    extra_slippage_bps: Decimal = Decimal("0")
    max_spread_bps: Decimal = Decimal("100")
    max_orderbook_age_seconds: int = 15
    closing_delay_seconds: int = 3
    poll_seconds: int = 5
    interval: str = "1m"

    def __post_init__(self) -> None:
        for name in (
            "initial_equity",
            "max_deployment",
            "single_order_cap",
            "extra_slippage_bps",
            "max_spread_bps",
        ):
            decimal(getattr(self, name), name)
        if not 0 < self.initial_equity <= Decimal(
            "50"
        ) or not 0 < self.max_deployment <= Decimal("0.5"):
            raise MarketDataInvalid("shadow capital envelope exceeded")
        if not 0 < self.single_order_cap <= self.initial_equity * self.max_deployment:
            raise MarketDataInvalid("invalid shadow order cap")
        if (
            not 0 <= self.extra_slippage_bps < Decimal("10000")
            or self.max_spread_bps < 0
        ):
            raise MarketDataInvalid("invalid spread/slippage policy")
        if self.interval != "1m":
            raise MarketDataInvalid("only UTC 1m candles supported")
        for name in (
            "max_orderbook_age_seconds",
            "closing_delay_seconds",
            "poll_seconds",
        ):
            if (
                type(getattr(self, name)) is not int
                or not 1 <= getattr(self, name) <= 60
            ):
                raise MarketDataInvalid("invalid bounded timing policy")

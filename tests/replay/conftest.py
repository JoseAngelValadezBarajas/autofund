from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from autofund.replay import (
    Candle,
    HistoricalDataset,
    ReplayConfig,
    StrategyIdentity,
    canonical_json,
    load_csv,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


@dataclass(frozen=True)
class ScriptedStrategy:
    orders: tuple

    @property
    def identity(self):
        return StrategyIdentity(
            "test_script", "1", (("orders", canonical_json(self.orders)),)
        )

    def on_candle(self, context):
        index = len(context.history) - 1
        return self.orders[index] if index < len(self.orders) else None


@pytest.fixture
def script():
    return ScriptedStrategy


@pytest.fixture
def dataset_factory():
    def make(prices, closes=None):
        start = datetime(2026, 1, 1, tzinfo=UTC)
        closes = closes if closes is not None else prices
        return HistoricalDataset(
            "BTC/MXN",
            tuple(
                Candle(
                    start + timedelta(hours=i),
                    D(str(opening)),
                    max(D(str(opening)), D(str(close))) + D("1"),
                    min(D(str(opening)), D(str(close))) - D("1"),
                    D(str(close)),
                    D("10"),
                )
                for i, (opening, close) in enumerate(zip(prices, closes, strict=True))
            ),
        )

    return make


@pytest.fixture
def zero_config():
    return ReplayConfig(fee_rate=D("0"), slippage_bps=D("0"))


@pytest.fixture
def mean_dataset():
    return load_csv(FIXTURES / "mean_reversion_market.csv", market="BTC/MXN")

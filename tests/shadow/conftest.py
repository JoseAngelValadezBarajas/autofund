import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from autofund.observer.models import (
    FeeSource,
    Level,
    MarketLimits,
    OrderBookSnapshot,
    PublicTrade,
    ShadowFee,
)
from autofund.observer.source import MarketFrame
from autofund.shadow.config import ShadowConfig
from autofund.shadow.session import ShadowSession


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline F4 test attempted network")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    for key in (
        "AUTOFUND_BITSO_PROD_API_KEY",
        "AUTOFUND_BITSO_PROD_API_SECRET",
        "AUTOFUND_BITSO_PROD_READONLY_CONFIRMED",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def start():
    return datetime(2026, 9, 18, 0, 0, tzinfo=UTC)


@pytest.fixture
def limits():
    return MarketLimits(
        "btc_mxn",
        D("0.00000001"),
        D("600"),
        D("1"),
        D("40000000"),
        D("1"),
        D("100000"),
        D("1"),
    )


@pytest.fixture
def fee():
    return ShadowFee(D("0.01"), FeeSource.CONFIGURED_ESTIMATE)


@pytest.fixture
def session(start, limits, fee):
    return ShadowSession(ShadowConfig(), limits, fee, start)


@pytest.fixture
def frames(start):
    result = []
    for index, price in enumerate(("1000", "1000", "800", "1000", "1000", "1000")):
        observed = start + timedelta(minutes=index + 1, seconds=4)
        trade = PublicTrade(
            "btc_mxn",
            index + 1,
            start + timedelta(minutes=index, seconds=10),
            D(price),
            D("0.01"),
            "buy",
        )
        depth = OrderBookSnapshot(
            "btc_mxn",
            observed - timedelta(seconds=1),
            index + 1,
            (Level(D("999"), D("1")),),
            (Level(D("1001"), D("1")),),
        )
        result.append(MarketFrame(observed, (trade,), depth))
    return result

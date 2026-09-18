import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autofund.market.binance_normalizer import normalize_kline, parse_market_info
from autofund.market.transport import PublicReply

FIXTURES = Path(__file__).parents[1] / "fixtures" / "binance"


class FakeClock:
    def __init__(self):
        self.elapsed = 0.0
        self.sleeps = []

    def now(self):
        return datetime(2026, 1, 2, tzinfo=UTC) + timedelta(seconds=self.elapsed)

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.elapsed += seconds

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.advance(seconds)


class FakeTransport:
    def __init__(self, metadata, connections, clock, replies=None):
        self.metadata = metadata
        self.connections = list(connections)
        self.clock = clock
        self.replies = list(replies or [])
        self.metadata_calls = []
        self.connect_calls = []
        self.closed_connections = 0

    async def exchange_info(self, symbol):
        self.metadata_calls.append(symbol)
        result = (
            self.replies.pop(0) if self.replies else PublicReply(200, self.metadata)
        )
        if isinstance(result, Exception):
            raise result
        return result

    @asynccontextmanager
    async def connect(self, symbol, interval):
        self.connect_calls.append((symbol, interval))
        sequence = (
            self.connections.pop(0)
            if self.connections
            else OSError("fixture exhausted")
        )
        if isinstance(sequence, Exception):
            raise sequence
        parent = self
        items = iter(sequence)

        class Receiver:
            async def receive(self, timeout):
                parent.clock.advance(0.01)
                value = next(items, OSError("fixture disconnected"))
                if isinstance(value, BaseException):
                    if isinstance(value, TimeoutError):
                        parent.clock.advance(timeout)
                    raise value
                return value

        try:
            yield Receiver()
        finally:
            self.closed_connections += 1


@pytest.fixture
def metadata():
    return (FIXTURES / "exchange_info_btc_mxn.json").read_text()


@pytest.fixture
def info(metadata):
    return parse_market_info(metadata, "BTCMXN")


@pytest.fixture
def messages():
    return [
        json.dumps(row)
        for row in json.loads((FIXTURES / "golden_messages.json").read_text())
    ]


@pytest.fixture
def events(messages, info):
    return tuple(normalize_kline(raw, info, "1m") for raw in messages)


@pytest.fixture
def fake_clock():
    return FakeClock()


@pytest.fixture
def transport_factory(metadata, fake_clock):
    return lambda connections, replies=None: FakeTransport(
        metadata, connections, fake_clock, replies
    )


@pytest.fixture(autouse=True)
def offline_network_guard(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        return
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("offline F2 test attempted real networking")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)

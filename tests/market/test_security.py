import ast
import asyncio
import inspect
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from autofund.market import BinancePublicMarketDataSource, LiveMarketRunner
from autofund.market.errors import ConfigurationError
from autofund.market.transport import (
    REST_ENDPOINT,
    STREAM_ENDPOINT,
    BinancePublicTransport,
)


def test_no_order_or_auth_surface_in_f2_source():
    root = Path(__file__).parents[2] / "src" / "autofund" / "market"
    forbidden_modules = {
        "autofund.wallet",
        "autofund.execution",
        "autofund.capital",
        "autofund.risk",
        "autofund.replay.runner",
        "ccxt",
        "binance",
        "dotenv",
    }
    forbidden_names = {"Wallet", "PaperExecutionEngine", "ExchangeExecutionClient"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                assert node.module not in forbidden_modules
            if isinstance(node, ast.Import):
                assert not any(alias.name in forbidden_modules for alias in node.names)
            if isinstance(node, ast.Name):
                assert node.id not in forbidden_names
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {
                    "post",
                    "delete",
                    "put",
                    "patch",
                    "send",
                    "send_json",
                }
    assert REST_ENDPOINT == "https://data-api.binance.vision/api/v3/exchangeInfo"
    assert STREAM_ENDPOINT == "wss://data-stream.binance.vision:443/ws/"
    parameters = inspect.signature(BinancePublicMarketDataSource).parameters
    assert not any(
        "key" in name or "secret" in name or "endpoint" in name for name in parameters
    )


def test_f2_never_touches_wallet_or_paper_execution(
    tmp_path, transport_factory, fake_clock, messages, monkeypatch
):
    from autofund.execution import PaperExecutionEngine
    from autofund.wallet import Wallet

    def forbidden(*args, **kwargs):
        raise AssertionError("financial execution reached by F2")

    monkeypatch.setattr(Wallet, "__init__", forbidden)
    monkeypatch.setattr(PaperExecutionEngine, "__init__", forbidden)
    monkeypatch.setenv("BINANCE_API_KEY", "SHOULD_NEVER_APPEAR")
    source = BinancePublicMarketDataSource(
        "BTCMXN", transport=transport_factory([messages]), clock=fake_clock
    )
    result = asyncio.run(
        LiveMarketRunner(clock=fake_clock).run(
            source, output=tmp_path / "no-auth.jsonl", closed_candles=6
        )
    )
    assert result.strategy_signal_count == 2
    assert all(
        "SHOULD_NEVER_APPEAR" not in path.read_text() for path in tmp_path.iterdir()
    )


def test_production_http_transport_only_calls_public_get(metadata, monkeypatch):
    original = httpx.AsyncClient
    requests = []

    def handle(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == REST_ENDPOINT + "?symbol=BTCMXN"
        assert "authorization" not in request.headers
        assert "x-mbx-apikey" not in request.headers
        return httpx.Response(200, text=metadata)

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    reply = asyncio.run(BinancePublicTransport().exchange_info("BTCMXN"))
    assert reply.status == 200 and len(requests) == 1


def test_production_websocket_wrapper_url_subscription_and_close(monkeypatch, messages):
    import autofund.market.transport as module

    seen = []

    class Socket:
        async def recv(self):
            return messages[0]

    @asynccontextmanager
    async def fake_connect(uri, **kwargs):
        assert uri == STREAM_ENDPOINT + "btcmxn@kline_1m"
        assert (
            kwargs["ping_interval"] is None
        )  # Library still sends automatic pong responses.
        assert kwargs["proxy"] is None
        seen.append("open")
        try:
            yield Socket()
        finally:
            seen.append("closed")

    monkeypatch.setattr(module, "connect", fake_connect)

    async def receive():
        async with BinancePublicTransport().connect("BTCMXN", "1m") as socket:
            assert not hasattr(socket, "send")
            assert await socket.receive(1) == messages[0]

    asyncio.run(receive())
    assert seen == ["open", "closed"]


@pytest.mark.parametrize(
    "symbol", ["btcmxn", "BTCMXN?symbol=ETHUSDT", "../order", "BTC/MXN", ""]
)
def test_symbol_cannot_inject_path_or_query(symbol):
    with pytest.raises(ConfigurationError):
        BinancePublicMarketDataSource(symbol)

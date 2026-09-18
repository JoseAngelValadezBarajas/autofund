"""Public-data-only network surface: one GET endpoint and receive-only streams."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .errors import ConfigurationError
from .models import interval_seconds, validate_symbol

REST_ENDPOINT = "https://data-api.binance.vision/api/v3/exchangeInfo"
STREAM_ENDPOINT = "wss://data-stream.binance.vision:443/ws/"


@dataclass(frozen=True, slots=True)
class PublicReply:
    status: int
    body: str
    retry_after: int | None = None


class ReceiveOnlySocket(Protocol):
    async def receive(self, timeout: float) -> str: ...


class PublicTransport(Protocol):
    async def exchange_info(self, symbol: str) -> PublicReply: ...
    def connect(
        self, symbol: str, interval: str
    ) -> AbstractAsyncContextManager[ReceiveOnlySocket]: ...


class BinancePublicTransport:
    def __init__(self, connect_timeout: int = 10) -> None:
        self._timeout = connect_timeout

    async def exchange_info(self, symbol: str) -> PublicReply:
        validate_symbol(symbol)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, trust_env=False, follow_redirects=False
            ) as client:
                response = await client.get(REST_ENDPOINT, params={"symbol": symbol})
        except httpx.TransportError as exc:
            raise OSError("public metadata transport unavailable") from exc
        retry_text = response.headers.get("Retry-After", "")
        return PublicReply(
            response.status_code,
            response.text,
            int(retry_text) if retry_text.isascii() and retry_text.isdigit() else None,
        )

    @asynccontextmanager
    async def connect(
        self, symbol: str, interval: str
    ) -> AsyncIterator[ReceiveOnlySocket]:
        validate_symbol(symbol)
        interval_seconds(interval)
        uri = STREAM_ENDPOINT + symbol.lower() + "@kline_" + interval
        try:
            async with connect(
                uri,
                open_timeout=self._timeout,
                close_timeout=5,
                ping_interval=None,
                proxy=None,
                max_size=65536,
                max_queue=16,
            ) as socket:

                class Receiver:
                    async def receive(self, timeout: float) -> str:
                        try:
                            message = await asyncio.wait_for(socket.recv(), timeout)
                        except ConnectionClosed as exc:
                            raise OSError("public stream disconnected") from exc
                        if isinstance(message, bytes):
                            return message.decode("utf-8", errors="replace")
                        return message

                # The library answers server Ping frames with Pong automatically.
                yield Receiver()
        except InvalidStatus as exc:
            if exc.response.status_code in (400, 401, 403, 404, 418, 451):
                raise ConfigurationError(
                    f"public stream HTTP {exc.response.status_code}"
                ) from exc
            raise OSError(
                f"public stream handshake HTTP {exc.response.status_code}"
            ) from exc

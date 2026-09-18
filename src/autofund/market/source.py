from collections.abc import AsyncGenerator
from typing import Protocol

from .models import MarketInfo, SourceItem


class PublicMarketDataSource(Protocol):
    source_name: str
    interval: str
    metadata_raw: str | None

    async def get_market_info(self) -> MarketInfo: ...
    def stream_candles(self) -> AsyncGenerator[SourceItem, None]: ...

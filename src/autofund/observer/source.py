"""Complete REST snapshots, bounded trade pagination; no WebSocket state."""

from dataclasses import dataclass
from datetime import UTC, datetime

from . import parsing
from .client import BitsoProductionReadOnlyClient
from .errors import MarketDataInvalid
from .models import OrderBookSnapshot, PublicTrade, book_name


@dataclass(frozen=True)
class MarketFrame:
    observed_at: datetime
    trades: tuple[PublicTrade, ...]
    depth: OrderBookSnapshot
    retry_count: int = 0


@dataclass(frozen=True)
class MarketNotice:
    observed_at: datetime
    kind: str
    severity: str = "DEGRADED"


class BitsoPublicMarketDataSource:
    def __init__(
        self,
        book: str = "btc_mxn",
        *,
        client: BitsoProductionReadOnlyClient | None = None,
        marker: int | None = None,
    ) -> None:
        self.book = book_name(book)
        self.client = client or BitsoProductionReadOnlyClient()
        self.marker = marker

    def poll(self) -> MarketFrame:
        collected: list[PublicTrade] = []
        cursor = self.marker
        retries = self.client.retries
        for _ in range(10):
            query = {
                "book": self.book,
                "limit": "100",
                "sort": "asc" if cursor is not None else "desc",
            }
            if cursor is not None:
                query["marker"] = str(cursor)
            page = parsing.trades(self.client.read("trades", query), self.book)
            collected.extend(page)
            next_cursor = max((t.trade_id for t in page), default=cursor)
            if cursor is None or len(page) < 100:
                cursor = next_cursor
                break
            if next_cursor is None or next_cursor <= cursor:
                raise MarketDataInvalid("trade pagination did not advance")
            cursor = next_cursor
        else:
            raise MarketDataInvalid("bounded trade scan incomplete")
        depth = parsing.depth(
            self.client.read("order_book", {"book": self.book}), self.book
        )
        # Advance only after the complete frame succeeds, so a failed depth read
        # cannot lose trades on retry.
        self.marker = cursor
        return MarketFrame(
            datetime.now(UTC), tuple(collected), depth, self.client.retries - retries
        )

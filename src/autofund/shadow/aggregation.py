from datetime import datetime, timedelta
from decimal import Decimal

from autofund.decimal_utils import financial
from autofund.observer.errors import MarketDataInvalid
from autofund.observer.models import PublicTrade
from autofund.replay.data import Candle
from autofund.replay.serialization import utc_timestamp


def minute(timestamp: datetime) -> datetime:
    return utc_timestamp(timestamp).replace(second=0, microsecond=0)


class TradeCandleAggregator:
    def __init__(self, book: str, start: datetime) -> None:
        if minute(start) != start:
            raise MarketDataInvalid("aggregation starts at a UTC minute boundary")
        self.book, self.start, self.next_minute = book, start, start
        self.seen: dict[int, PublicTrade] = {}
        self.pending: dict[datetime, list[PublicTrade]] = {}
        self.duplicates = 0
        self.out_of_order = 0
        self.latest: tuple[datetime, int] | None = None
        self.gaps: list[datetime] = []

    def ingest(self, trades: tuple[PublicTrade, ...]) -> None:
        for trade in trades:
            if trade.book != self.book:
                raise MarketDataInvalid("trade book differs from session")
            prior = self.seen.get(trade.trade_id)
            if prior is not None:
                if prior != trade:
                    raise MarketDataInvalid("contradictory public trade ID")
                self.duplicates += 1
                continue
            self.seen[trade.trade_id] = trade
            if trade.timestamp < self.start:
                continue  # Deliberately exclude the incomplete initial minute.
            bucket = minute(trade.timestamp)
            if bucket < self.next_minute:
                raise MarketDataInvalid("late trade changes an already closed candle")
            key = (trade.timestamp, trade.trade_id)
            if self.latest is not None and key < self.latest:
                self.out_of_order += 1
            self.latest = max(key, self.latest) if self.latest else key
            self.pending.setdefault(bucket, []).append(trade)

    @financial
    def close_until(self, watermark: datetime) -> tuple[Candle, ...]:
        result = []
        while self.next_minute + timedelta(minutes=1) <= watermark:
            timestamp = self.next_minute
            values = sorted(
                self.pending.pop(timestamp, []), key=lambda t: (t.timestamp, t.trade_id)
            )
            if values:
                prices = [t.price for t in values]
                result.append(
                    Candle(
                        timestamp,
                        prices[0],
                        max(prices),
                        min(prices),
                        prices[-1],
                        sum((t.amount for t in values), Decimal("0")),
                    )
                )
            else:
                self.gaps.append(timestamp)
            self.next_minute += timedelta(minutes=1)
        return tuple(result)

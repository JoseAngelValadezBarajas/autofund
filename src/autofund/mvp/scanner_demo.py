"""Deterministic, network-free scanner source for demo mode.

Demo mode must never contact an exchange. This source is a fixed in-memory
fixture that exercises every eligibility outcome, so browser certification can
show a realistic scanner while guaranteeing zero network traffic.
"""

from datetime import UTC, datetime
from decimal import Decimal

DEMO_BOOKS: tuple[tuple[str, str], ...] = (
    ("btc_mxn", "10"),   # eligible
    ("usd_mxn", "10"),   # wide spread -> ineligible
    ("eth_mxn", "10"),   # eligible
    ("sol_mxn", "60"),   # minimum above the 11 MXN cap
)

# book -> (bid, ask, high, low, top-of-book amount)
DEMO_MARKETS: dict[str, tuple[str, str, str, str, str]] = {
    "btc_mxn": ("1000000", "1000010", "1010000", "990000", "0.0002"),
    "usd_mxn": ("18", "18.9", "18.9", "17.95", "100"),
    "eth_mxn": ("50000", "50300", "54000", "46000", "0.001"),
    "sol_mxn": ("3000", "3001", "3300", "2700", "0.01"),
}


class _Limits:
    def __init__(self, book: str, minimum_value: str) -> None:
        self.book, self.minimum_value = book, Decimal(minimum_value)
        self.minimum_amount = Decimal("0.000001")


class _Level:
    def __init__(self, price: str, amount: str) -> None:
        self.price, self.amount = Decimal(price), Decimal(amount)


class _Ticker:
    def __init__(self, book: str, bid: str, ask: str, high: str, low: str) -> None:
        self.book, self.bid, self.ask = book, Decimal(bid), Decimal(ask)
        self.high, self.low = Decimal(high), Decimal(low)
        self.volume, self.vwap = Decimal("100"), Decimal("100")


class _Depth:
    def __init__(self, book: str, bid: str, ask: str, amount: str, at: datetime) -> None:
        self.book, self.timestamp, self.sequence = book, at, 1
        self.bids, self.asks = (_Level(bid, amount),), (_Level(ask, amount),)

    @property
    def best_bid(self) -> Decimal:
        return self.bids[0].price

    @property
    def best_ask(self) -> Decimal:
        return self.asks[0].price

    @property
    def spread_bps(self) -> Decimal:
        mid = (self.best_ask + self.best_bid) / Decimal("2")
        return (self.best_ask - self.best_bid) / mid * Decimal("10000")


class _Fee:
    rate = Decimal("0.0078")


class DemoScannerSource:
    """Fixed in-memory market data. Performs no I/O of any kind."""

    def __init__(self, *, now: datetime | None = None) -> None:
        self._now = now

    def _stamp(self) -> datetime:
        return self._now or datetime.now(UTC)

    def available_books(self) -> tuple[_Limits, ...]:
        return tuple(_Limits(book, minimum) for book, minimum in DEMO_BOOKS)

    def ticker(self, book: str) -> _Ticker:
        return _Ticker(book, *DEMO_MARKETS[book][:4])

    def order_book(self, book: str) -> _Depth:
        bid, ask, _, _, amount = DEMO_MARKETS[book]
        return _Depth(book, bid, ask, amount, self._stamp())

    def fee_schedule(self, book: str) -> _Fee:
        return _Fee()

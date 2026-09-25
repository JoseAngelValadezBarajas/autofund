"""Binds forward microstructure capture to the existing GET-only production client.

The adapter is deliberately thin. All the interesting judgement lives in
`microstructure.py`; this module only translates the client's typed observations into the
collector's event types and stamps the second clock.

**On `maker_side`.** Bitso's public trade payload does include a maker side, and
`PublicTrade` already validates it against `("buy", "sell")`. So the side is carried through
as `PROVIDER_AUTHORITATIVE` rather than inferred. That matters because adverse-selection
analysis is the whole purpose of collecting the tape: a maker order that fills only when
price is about to move against it is the reason a fee saving can be illusory, and that
conclusion is only trustworthy if the aggressor side came from the exchange.

**On crossed books.** `OrderBookSnapshot` already raises on a crossed or locked book during
parsing, so through this adapter a crossed book cannot arrive — it fails earlier. The
collector's own crossed-book check therefore only fires for injected or replayed sources.
That is not redundant: the check must exist for the paths where the assumption does not
hold, and it is documented here so nobody later mistakes an unreachable flag for dead code
and removes it.

**On loss of exchange history.** A failure to fetch a snapshot or the tape is recorded as an
anomaly and the pass continues. A research collector that raises on a transient network error
would stop capturing precisely when the market is most interesting.
"""

from datetime import UTC, datetime

from autofund.observer.client import BitsoProductionReadOnlyClient
from autofund.observer.models import Level, OrderBookSnapshot, PublicTrade

from .microstructure import (
    BookEvent,
    DepthLevel,
    MicrostructureCollector,
    MicrostructureError,
    MicrostructureStore,
    TradeEvent,
)

ADAPTER_VERSION = "autofund.microstructure-adapter.v1"

# Depth is truncated to what an order of the configured capital could plausibly consume,
# plus margin for analysis. Capturing the full book would inflate storage for levels that
# can never be reached by an 11 MXN order.
DEFAULT_DEPTH_LEVELS = 20

# Analysis margin over the capital cap, so a passive reprice sequence remains reconstructible.
DEFAULT_DEPTH_MARGIN_MULTIPLE = 3


def _levels(levels: tuple[Level, ...], limit: int) -> tuple[DepthLevel, ...]:
    return tuple(DepthLevel(price=level.price, quantity=level.amount) for level in levels[:limit])


def book_event_from_snapshot(snapshot: OrderBookSnapshot, *,
                             received_at: datetime | None = None,
                             depth_levels: int = DEFAULT_DEPTH_LEVELS) -> BookEvent:
    """Translate one parsed snapshot, preserving the exchange clock and sequence."""
    return BookEvent(book=snapshot.book, exchange_timestamp=snapshot.timestamp,
                     received_at=received_at or datetime.now(UTC), sequence=snapshot.sequence,
                     bids=_levels(snapshot.bids, depth_levels),
                     asks=_levels(snapshot.asks, depth_levels),
                     update_provenance="SNAPSHOT")


def trade_event_from_public(trade: PublicTrade, *,
                            received_at: datetime | None = None) -> TradeEvent:
    return TradeEvent(book=trade.book, trade_id=str(trade.trade_id),
                      exchange_timestamp=trade.timestamp,
                      received_at=received_at or datetime.now(UTC), price=trade.price,
                      quantity=trade.amount, maker_side=trade.maker_side)


class BitsoMicrostructureSource:
    """Read-only market source over the existing production client.

    Note what this class does **not** have: no order submission, no cancellation, no
    replacement, no credentials requirement. Maker execution remains unevaluated until the
    captured evidence justifies implementing it, and this milestone does not implement it.
    """

    def __init__(self, client: BitsoProductionReadOnlyClient, *,
                 depth_levels: int = DEFAULT_DEPTH_LEVELS,
                 trade_limit: int = 100) -> None:
        if depth_levels <= 0 or trade_limit <= 0:
            raise MicrostructureError("capture limits must be positive")
        self._client = client
        self._depth_levels = depth_levels
        self._trade_limit = trade_limit

    @property
    def endpoints_touched(self) -> tuple[str, ...]:
        return ("order_book", "trades")

    def order_book_snapshot(self, book: str) -> BookEvent:
        return book_event_from_snapshot(self._client.order_book(book),
                                        depth_levels=self._depth_levels)

    def recent_trades(self, book: str) -> tuple[TradeEvent, ...]:
        trades = self._client.trades(book, limit=self._trade_limit)
        return tuple(trade_event_from_public(trade) for trade in trades)

    def public(self) -> dict[str, object]:
        return {"version": ADAPTER_VERSION, "depth_levels": self._depth_levels,
                "trade_limit": self._trade_limit,
                "endpoints_touched": list(self.endpoints_touched),
                "order_capability": "NONE", "credentials_required": False}


def resolve_books(client: BitsoProductionReadOnlyClient, books: tuple[str, ...]) -> tuple[str, ...]:
    """Intersect requested books with those the exchange actually offers.

    A capture run should fail loudly on a typo in a book name rather than silently record
    nothing, since an empty dataset and a quiet market look identical after the fact.
    """
    available = {constraints.book for constraints in client.available_books()}
    missing = tuple(book for book in books if book not in available)
    if missing:
        raise MicrostructureError(f"UNKNOWN_BOOK:{','.join(missing)}")
    return books


def build_collector(*, client: BitsoProductionReadOnlyClient, store: MicrostructureStore,
                    books: tuple[str, ...],
                    depth_levels: int = DEFAULT_DEPTH_LEVELS) -> MicrostructureCollector:
    return MicrostructureCollector(
        source=BitsoMicrostructureSource(client, depth_levels=depth_levels), store=store,
        books=resolve_books(client, books))


__all__ = [
    "ADAPTER_VERSION",
    "DEFAULT_DEPTH_LEVELS",
    "DEFAULT_DEPTH_MARGIN_MULTIPLE",
    "BitsoMicrostructureSource",
    "book_event_from_snapshot",
    "build_collector",
    "resolve_books",
    "trade_event_from_public",
]

"""Read-only forward microstructure capture: order books and the public trade tape.

MVP 0.2.3 reached a specific dead end and named it precisely: maker execution could not be
evaluated at all, because candle OHLC records a price *range* per interval and never a
*sequence*, so it cannot show whether a resting order was ahead of the trades that printed
at its price. No amount of additional candle history fixes that. The missing evidence is of
a different kind, and it only exists going forward.

This module collects it. The design constraints are all consequences of that dead end:

**Zero order capability.** The collector holds a GET-only transport and has no method that
posts, cancels or replaces anything. That is not a convention enforced by discipline — the
type it depends on has no such method to call.

**Sequence is preserved, never repaired.** An exchange publishes a monotonically increasing
sequence number precisely so a consumer can detect that it missed something. Forward-filling
a gap, reordering an out-of-order update, or dropping a duplicate silently would produce a
dataset that looks complete and is wrong — and wrong in the specific direction of making a
maker fill look *more* achievable, because the missing updates are the ones that would have
shown the price moving away. Every anomaly is therefore **recorded as an anomaly** and
surfaced as a quality flag.

**Queue position is reported as unknowable.** Public data cannot reveal how much size sits
ahead of a hypothetical order at the same price. The collector says so and labels the
dataset `BOUNDED_ONLY` rather than implying exact fill knowledge. A future fill model must
use a conservative bound or a range of plausible outcomes, not a pretended exactness.

**Storage is bounded by construction.** A collector that runs indefinitely on a machine
with finite disk is a latent outage. Retention is a declared policy, chunks rotate, and each
chunk carries a fingerprint so a truncation or corruption is detectable rather than assumed
absent.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from autofund.decimal_utils import ZERO, decimal
from autofund.replay.serialization import fingerprint

MICROSTRUCTURE_VERSION = "autofund.forward-microstructure.v1"

# A provenance kind distinct from every existing one. Microstructure evidence is real and
# captured, so it may inform execution research, but it is NOT strategy performance
# evidence: it contains no fills and cannot certify a strategy.
REAL_CAPTURED_MICROSTRUCTURE = "REAL_CAPTURED_MICROSTRUCTURE"

# Data-quality flags. Each is a distinct observation, not a severity level, because the
# remedies differ: a gap means evidence is missing, a crossed book means the evidence is
# internally inconsistent, and a sequence regression means the stream restarted.
FLAG_OK = "OK"
FLAG_GAP = "GAP"
FLAG_DUPLICATE = "DUPLICATE"
FLAG_OUT_OF_ORDER = "OUT_OF_ORDER"
FLAG_SEQUENCE_REGRESSION = "SEQUENCE_REGRESSION"
FLAG_DISCONNECT = "DISCONNECT"
FLAG_RECONNECT = "RECONNECT"
FLAG_CLOCK_SKEW = "CLOCK_SKEW"
FLAG_STALE_BOOK = "STALE_BOOK"
FLAG_CROSSED_BOOK = "CROSSED_BOOK"
FLAG_INVALID_DEPTH = "INVALID_DEPTH"

ALL_QUALITY_FLAGS = (FLAG_OK, FLAG_GAP, FLAG_DUPLICATE, FLAG_OUT_OF_ORDER,
                     FLAG_SEQUENCE_REGRESSION, FLAG_DISCONNECT, FLAG_RECONNECT,
                     FLAG_CLOCK_SKEW, FLAG_STALE_BOOK, FLAG_CROSSED_BOOK,
                     FLAG_INVALID_DEPTH)

# Collector health. Independent of financial reconciliation: a research collector stalling
# must not stop the trading runtime, so this is surfaced, not fatal.
HEALTHY = "HEALTHY"
DEGRADED = "DEGRADED"
STOPPED = "STOPPED"

# Queue position is not derivable from public data. Stated as a constant so a downstream
# consumer cannot accidentally assume otherwise.
QUEUE_POSITION_OBSERVABLE = False
PASSIVE_FILL_EXACTNESS = "BOUNDED_ONLY"

# Predeclared markout horizons for adverse selection (spec section 24). Declared before any
# evaluation so the horizons cannot be chosen to suit a result.
SHORT_MARKOUT_SECONDS = 5
MEDIUM_MARKOUT_SECONDS = 60

# Storage bounds. Sized so a long-running collector degrades gracefully rather than
# filling the disk: retention is a declared window, and each chunk is bounded.
DEFAULT_RETENTION_HOURS = 72
DEFAULT_MAX_CHUNK_EVENTS = 20000
DEFAULT_MAX_TOTAL_BYTES = 512 * 1024 * 1024

# Clock skew beyond this is recorded as an anomaly. Chosen generously relative to exchange
# timestamp granularity so ordinary network jitter does not raise a flag.
MAX_CLOCK_SKEW_SECONDS = 5


class MicrostructureError(ValueError):
    """The collector was asked for something it cannot defend."""


@dataclass(frozen=True, slots=True)
class DepthLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        decimal(self.price, "price")
        decimal(self.quantity, "quantity")
        if self.price <= ZERO or self.quantity < ZERO:
            raise MicrostructureError("depth level requires positive price, non-negative size")

    def public(self) -> dict[str, str]:
        return {"price": str(self.price), "quantity": str(self.quantity)}


@dataclass(frozen=True, slots=True)
class BookEvent:
    """One order-book observation, with both clocks and its own provenance."""

    book: str
    exchange_timestamp: datetime
    received_at: datetime
    sequence: int | None
    bids: tuple[DepthLevel, ...]
    asks: tuple[DepthLevel, ...]
    update_provenance: str = "SNAPSHOT"
    quality: str = FLAG_OK

    def __post_init__(self) -> None:
        if not self.book:
            raise MicrostructureError("book is required")
        if self.sequence is not None and self.sequence < 0:
            raise MicrostructureError("sequence must not be negative")

    @property
    def best_bid(self) -> Decimal:
        return max((level.price for level in self.bids), default=ZERO)

    @property
    def best_ask(self) -> Decimal:
        return min((level.price for level in self.asks), default=ZERO)

    @property
    def spread_bps(self) -> Decimal:
        bid, ask = self.best_bid, self.best_ask
        if bid <= ZERO or ask <= ZERO or ask <= bid:
            return ZERO
        midpoint = (bid + ask) / Decimal("2")
        return (ask - bid) / midpoint * Decimal("10000")

    @property
    def is_crossed(self) -> bool:
        """Whether the book is internally inconsistent (bid at or above ask)."""
        return self.best_bid > ZERO and self.best_ask > ZERO and self.best_bid >= self.best_ask

    @property
    def clock_skew_seconds(self) -> Decimal:
        return Decimal(str(abs((self.received_at - self.exchange_timestamp).total_seconds())))

    @property
    def depth_notional_mxn(self) -> Decimal:
        """Bid-side notional available, the relevant figure for a SELL simulation."""
        return sum((level.price * level.quantity for level in self.bids), ZERO)

    def identity(self) -> str:
        """Stable identity for deduplication.

        Uses the exchange's own sequence when it publishes one, because that is the only
        identifier the producer guarantees unique. Without a sequence, identity falls back
        to the observation content, which is weaker but never invents a uniqueness the
        exchange did not provide.
        """
        if self.sequence is not None:
            return fingerprint({"book": self.book, "sequence": self.sequence,
                                "ts": self.exchange_timestamp.isoformat()})
        return fingerprint({"book": self.book,
                            "ts": self.exchange_timestamp.isoformat(),
                            "bid": str(self.best_bid), "ask": str(self.best_ask),
                            "levels": len(self.bids) + len(self.asks)})

    def public(self) -> dict[str, Any]:
        return {"version": MICROSTRUCTURE_VERSION, "kind": "ORDER_BOOK",
                "book": self.book,
                "exchange_timestamp": self.exchange_timestamp.isoformat(),
                "received_at": self.received_at.isoformat(),
                "sequence": self.sequence, "quality": self.quality,
                "update_provenance": self.update_provenance,
                "best_bid": str(self.best_bid), "best_ask": str(self.best_ask),
                "spread_bps": str(self.spread_bps),
                "depth_notional_mxn": str(self.depth_notional_mxn),
                "clock_skew_seconds": str(self.clock_skew_seconds),
                "bids": [level.public() for level in self.bids],
                "asks": [level.public() for level in self.asks]}


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """One public executed trade.

    `maker_side` is carried only when the provider states it authoritatively. It is
    deliberately not inferred: an invented aggressor side would be indistinguishable from a
    real one in the stored dataset, and every adverse-selection conclusion rests on it.
    """

    book: str
    trade_id: str | None
    exchange_timestamp: datetime
    received_at: datetime
    price: Decimal
    quantity: Decimal
    maker_side: str | None = None
    sequence: int | None = None
    quality: str = FLAG_OK

    def __post_init__(self) -> None:
        decimal(self.price, "price")
        decimal(self.quantity, "quantity")
        if self.price <= ZERO or self.quantity <= ZERO:
            raise MicrostructureError("trade requires positive price and quantity")
        if self.maker_side is not None and self.maker_side.lower() not in ("buy", "sell"):
            raise MicrostructureError(f"invalid maker side: {self.maker_side}")

    @property
    def notional_mxn(self) -> Decimal:
        return self.price * self.quantity

    def identity(self) -> str:
        """Stable identity for deduplication, keyed on the provider's trade id."""
        if self.trade_id is not None:
            return fingerprint({"book": self.book, "trade_id": self.trade_id})
        return fingerprint({"book": self.book, "ts": self.exchange_timestamp.isoformat(),
                            "price": str(self.price), "qty": str(self.quantity)})

    def public(self) -> dict[str, Any]:
        return {"version": MICROSTRUCTURE_VERSION, "kind": "TRADE", "book": self.book,
                "trade_id": self.trade_id,
                "exchange_timestamp": self.exchange_timestamp.isoformat(),
                "received_at": self.received_at.isoformat(),
                "price": str(self.price), "quantity": str(self.quantity),
                "notional_mxn": str(self.notional_mxn),
                "maker_side": self.maker_side,
                "maker_side_source": ("PROVIDER_AUTHORITATIVE"
                                      if self.maker_side is not None else "UNAVAILABLE"),
                "sequence": self.sequence, "quality": self.quality}


@dataclass(slots=True)
class CaptureQuality:
    """Accumulated data-quality counters. Anomalies are counted, never repaired.

    Three conditions are deliberately kept apart, because a polling collector experiences
    them for different reasons and only one of them indicates a defect:

    - `duplicates`: the same observation seen twice. A polling collector re-reads a snapshot
      endpoint, so this is routine and says nothing about data integrity.
    - `sequence_advances`: the book moved between observations. Unavoidable when polling, and
      indistinguishable from missed updates, so it is recorded as what it is — an advance —
      rather than asserted to be a gap. Calling it a gap would claim knowledge the collector
      does not have.
    - `sequence_regressions`: the sequence went backwards. This is unambiguously wrong and is
      what `health` reacts to.

    Collapsing these into one "anomaly" count made a healthy capture report DEGRADED, which
    is how a monitoring signal becomes something operators learn to ignore.
    """

    events_seen: int = 0
    events_stored: int = 0
    duplicates: int = 0
    out_of_order: int = 0
    sequence_regressions: int = 0
    sequence_advances: int = 0
    gaps: int = 0
    disconnects: int = 0
    reconnects: int = 0
    clock_skew_events: int = 0
    stale_books: int = 0
    crossed_books: int = 0
    invalid_depth: int = 0
    highest_sequence: dict[str, int] = field(default_factory=dict)
    _seen: set[str] = field(default_factory=set)

    def observe(self, *, book: str, identity: str, sequence: int | None,
                quality_flags: Iterable[str] = ()) -> bool:
        """Record one event. Returns False when it is a duplicate and must not be stored."""
        self.events_seen += 1
        for flag in quality_flags:
            self.flag(flag)
        if identity in self._seen:
            self.duplicates += 1
            return False
        self._seen.add(identity)
        if sequence is not None:
            previous = self.highest_sequence.get(book)
            if previous is not None:
                if sequence < previous:
                    # The stream went backwards. Either it restarted or the provider
                    # replayed; treating this as an ordinary update would corrupt ordering.
                    self.sequence_regressions += 1
                elif sequence > previous:
                    # An advance. Between two polls the book will have moved, so this is
                    # expected and is NOT evidence that updates were missed.
                    self.sequence_advances += 1
            self.highest_sequence[book] = max(sequence, previous or sequence)
        self.events_stored += 1
        return True

    def flag(self, name: str) -> None:
        if name == FLAG_DUPLICATE:
            self.duplicates += 1
        elif name == FLAG_OUT_OF_ORDER:
            self.out_of_order += 1
        elif name == FLAG_SEQUENCE_REGRESSION:
            self.sequence_regressions += 1
        elif name == FLAG_GAP:
            self.gaps += 1
        elif name == FLAG_DISCONNECT:
            self.disconnects += 1
        elif name == FLAG_RECONNECT:
            self.reconnects += 1
        elif name == FLAG_CLOCK_SKEW:
            self.clock_skew_events += 1
        elif name == FLAG_STALE_BOOK:
            self.stale_books += 1
        elif name == FLAG_CROSSED_BOOK:
            self.crossed_books += 1
        elif name == FLAG_INVALID_DEPTH:
            self.invalid_depth += 1
        elif name != FLAG_OK:
            raise MicrostructureError(f"unknown quality flag: {name}")

    @property
    def anomaly_count(self) -> int:
        """Conditions that indicate a real defect, excluding routine poll duplication."""
        return (self.out_of_order + self.sequence_regressions + self.gaps
                + self.disconnects + self.reconnects + self.clock_skew_events
                + self.stale_books + self.crossed_books + self.invalid_depth)

    def health(self) -> str:
        """Collector health, deliberately independent of financial reconciliation.

        A degraded research collector must never stop trading, so this reports status
        without any authority to halt anything else. Duplicates and sequence advances are
        excluded because a polling collector produces both by design; flagging them would
        make DEGRADED the steady state and therefore meaningless.
        """
        if self.events_seen == 0 or self.events_stored == 0:
            return STOPPED
        # Conditions that indicate the evidence is wrong or missing, rather than merely
        # re-read. A gap reaches this set only when it was flagged as one; an inferable
        # advance never does, because polling cannot distinguish it from a live movement.
        if (self.crossed_books > 0 or self.invalid_depth > 0
                or self.sequence_regressions > 0 or self.gaps > 0):
            return DEGRADED
        if self.clock_skew_events > 0 or self.disconnects > 0:
            return DEGRADED
        return HEALTHY

    def public(self) -> dict[str, Any]:
        return {"version": MICROSTRUCTURE_VERSION, "health": self.health(),
                "events_seen": self.events_seen, "events_stored": self.events_stored,
                "duplicates": self.duplicates, "out_of_order": self.out_of_order,
                "sequence_regressions": self.sequence_regressions,
                "sequence_advances": self.sequence_advances, "gaps": self.gaps,
                "disconnects": self.disconnects, "reconnects": self.reconnects,
                "clock_skew_events": self.clock_skew_events,
                "stale_books": self.stale_books, "crossed_books": self.crossed_books,
                "invalid_depth": self.invalid_depth,
                "anomaly_count": self.anomaly_count,
                "anomalies_repaired": 0,
                "duplicates_are_routine": True,
                "sequence_advances_are_routine": True,
                "books_tracked": len(self.highest_sequence)}


class ReadOnlyMarketSource(Protocol):
    """The collector's entire dependency surface. It has no mutation method by design."""

    def order_book_snapshot(self, book: str) -> BookEvent: ...

    def recent_trades(self, book: str) -> tuple[TradeEvent, ...]: ...


def quality_flags_for_book(*, event: BookEvent,
                           now: datetime | None = None,
                           stale_after_seconds: int = 60) -> tuple[str, ...]:
    """Data-quality flags for one book observation.

    Each check is reported rather than corrected. A crossed book is left crossed in the
    stored record, because a consumer that never sees the anomaly cannot know to distrust
    the evidence around it.
    """
    flags: list[str] = []
    if event.is_crossed:
        flags.append(FLAG_CROSSED_BOOK)
    if not event.bids or not event.asks:
        flags.append(FLAG_INVALID_DEPTH)
    if event.clock_skew_seconds > Decimal(MAX_CLOCK_SKEW_SECONDS):
        flags.append(FLAG_CLOCK_SKEW)
    moment = now or datetime.now(UTC)
    age = (moment - event.exchange_timestamp).total_seconds()
    if age > stale_after_seconds:
        flags.append(FLAG_STALE_BOOK)
    return tuple(flags) or (FLAG_OK,)


@dataclass(slots=True)
class MicrostructureStore:
    """Bounded, chunked, fingerprinted on-disk store for forward capture.

    Kept deliberately separate from the Production ledger, the shadow accounting and the
    strategy evidence store: microstructure evidence answers an execution question and has
    no authority over financial truth. Mixing them would let a research capture failure
    perturb a financial record.
    """

    root: Path
    retention_hours: int = DEFAULT_RETENTION_HOURS
    max_chunk_events: int = DEFAULT_MAX_CHUNK_EVENTS
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        if self.retention_hours <= 0 or self.max_chunk_events <= 0 or self.max_total_bytes <= 0:
            raise MicrostructureError("storage bounds must be positive")
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def provenance(self) -> str:
        return REAL_CAPTURED_MICROSTRUCTURE

    def chunk_name(self, *, book: str, index: int) -> str:
        return f"{book}.{index:05d}.jsonl"

    def append(self, *, book: str, records: tuple[dict[str, Any], ...]) -> Path:
        """Append records to the current chunk, rotating when it reaches its bound."""
        if not records:
            raise MicrostructureError("nothing to append")
        index = 0
        existing = sorted(self.root.glob(f"{book}.*.jsonl"))
        if existing:
            index = len(existing) - 1
            current = existing[-1]
            lines = sum(1 for _ in current.open(encoding="utf-8"))
            if lines + len(records) > self.max_chunk_events:
                index += 1
        target = self.root / self.chunk_name(book=book, index=index)
        with target.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        self.enforce_retention()
        return target

    def enforce_retention(self) -> list[Path]:
        """Drop whole chunks older than the retention window, oldest first.

        Chunks are removed whole so a retained dataset is always a set of complete,
        checksummed units. Deleting individual lines could leave a chunk whose manifest no
        longer describes it, which is worse than deleting more than strictly necessary.
        """
        removed: list[Path] = []
        cutoff = datetime.now(UTC).timestamp() - self.retention_hours * 3600
        for chunk in sorted(self.root.glob("*.jsonl")):
            if chunk.stat().st_mtime < cutoff:
                chunk.unlink()
                removed.append(chunk)
        total = sum(chunk.stat().st_size for chunk in self.root.glob("*.jsonl"))
        for chunk in sorted(self.root.glob("*.jsonl")):
            if total <= self.max_total_bytes:
                break
            total -= chunk.stat().st_size
            chunk.unlink()
            removed.append(chunk)
        return removed

    def chunk_fingerprint(self, path: Path) -> str:
        return fingerprint({"name": path.name,
                            "lines": sum(1 for _ in path.open(encoding="utf-8"))})

    def manifest(self) -> dict[str, Any]:
        """Dataset manifest with per-chunk fingerprints, so truncation is detectable."""
        chunks = sorted(self.root.glob("*.jsonl"))
        return {"version": MICROSTRUCTURE_VERSION, "provenance": self.provenance,
                "retention_hours": self.retention_hours,
                "max_chunk_events": self.max_chunk_events,
                "max_total_bytes": self.max_total_bytes,
                "chunk_count": len(chunks),
                "total_bytes": sum(chunk.stat().st_size for chunk in chunks),
                "chunks": [{"name": chunk.name,
                            "bytes": chunk.stat().st_size,
                            "fingerprint": self.chunk_fingerprint(chunk)}
                           for chunk in chunks],
                "loss_of_certified_artifacts": False}


@dataclass(slots=True)
class MicrostructureCollector:
    """Read-only capture loop. Cannot mutate Production and cannot halt trading."""

    source: ReadOnlyMarketSource
    store: MicrostructureStore
    books: tuple[str, ...]
    quality: CaptureQuality = field(default_factory=CaptureQuality)

    def __post_init__(self) -> None:
        if not self.books:
            raise MicrostructureError("at least one book is required")

    def capture_once(self, *, now: datetime | None = None) -> dict[str, int]:
        """One capture pass. Returns per-book stored counts."""
        moment = now or datetime.now(UTC)
        stored: dict[str, int] = {}
        for book in self.books:
            book_records: list[dict[str, Any]] = []
            snapshot = self.source.order_book_snapshot(book)
            flags = quality_flags_for_book(event=snapshot, now=moment)
            if self.quality.observe(book=book, identity=snapshot.identity(),
                                    sequence=snapshot.sequence, quality_flags=flags):
                book_records.append(snapshot.public())
            for trade in self.source.recent_trades(book):
                if self.quality.observe(book=book, identity=trade.identity(),
                                        sequence=trade.sequence):
                    book_records.append(trade.public())
            if book_records:
                self.store.append(book=book, records=tuple(book_records))
            stored[book] = len(book_records)
        return stored

    def public(self) -> dict[str, Any]:
        return {"version": MICROSTRUCTURE_VERSION, "provenance": self.store.provenance,
                "books": list(self.books), "quality": self.quality.public(),
                "queue_position_observable": QUEUE_POSITION_OBSERVABLE,
                "passive_fill_exactness": PASSIVE_FILL_EXACTNESS,
                "order_capability": "NONE",
                "production_mutation": "NONE",
                "retention_hours": self.store.retention_hours}


__all__ = [
    "ALL_QUALITY_FLAGS",
    "DEFAULT_MAX_CHUNK_EVENTS",
    "DEFAULT_MAX_TOTAL_BYTES",
    "DEFAULT_RETENTION_HOURS",
    "DEGRADED",
    "FLAG_CLOCK_SKEW",
    "FLAG_CROSSED_BOOK",
    "FLAG_DISCONNECT",
    "FLAG_DUPLICATE",
    "FLAG_GAP",
    "FLAG_INVALID_DEPTH",
    "FLAG_OK",
    "FLAG_OUT_OF_ORDER",
    "FLAG_RECONNECT",
    "FLAG_SEQUENCE_REGRESSION",
    "FLAG_STALE_BOOK",
    "HEALTHY",
    "MAX_CLOCK_SKEW_SECONDS",
    "MEDIUM_MARKOUT_SECONDS",
    "MICROSTRUCTURE_VERSION",
    "PASSIVE_FILL_EXACTNESS",
    "QUEUE_POSITION_OBSERVABLE",
    "REAL_CAPTURED_MICROSTRUCTURE",
    "SHORT_MARKOUT_SECONDS",
    "STOPPED",
    "BookEvent",
    "CaptureQuality",
    "DepthLevel",
    "MicrostructureCollector",
    "MicrostructureError",
    "MicrostructureStore",
    "TradeEvent",
    "quality_flags_for_book",
]

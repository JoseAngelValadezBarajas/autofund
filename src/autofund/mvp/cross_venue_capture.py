"""A read-only, synchronized cross-venue collector for real top-of-book observations.

MVP 0.2.8's screening used historical candles because that is the only long history available, and
the spec is explicit that candle screening **may identify candidate periods but may not prove an
executable dislocation**. Proving one requires a real book, captured forward, with real receive
timestamps. This module provides that.

**Why this is not a venue adapter.** There is no order path here and no way to add one: the
sources expose a single read method, the collector has no mutate method, and a test asserts that
no function in the module names an order, transfer or credential concept. The distinction matters
because "we built integration with another exchange" and "we read a public price" are very
different amounts of risk, and only the second one is in scope.

**The staleness measurement is the point.** Two venues are never read simultaneously when polling
over HTTP, so every comparison has a skew between its two reads. Measuring that skew from real
`perf_counter`-based receive timestamps — rather than assuming the two prices were simultaneous —
is what makes it possible to *exclude* a comparison instead of averaging a stale one in. The
incumbent is read first on every cycle so that a reference read cannot be fresher than the
incumbent it is compared against.

**Same-quote only, enforced structurally.** Only reference markets quoted in the incumbent's own
quote currency are accepted, so no FX conversion is needed and none is attempted. A conversion
would introduce its own spread and staleness, and the resulting difference would no longer be
attributable to the venues.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from autofund.decimal_utils import ZERO

from .cross_venue_dislocation import (
    VALID,
    Quote,
)

CROSS_VENUE_VERSION = "autofund.cross-venue-capture.v1"

# Provenance for everything this module produces. Distinct from the single-venue capture the
# project already has, because a cross-venue observation carries an inter-venue skew that a
# single-venue one does not.
REAL_CAPTURED_CROSS_VENUE = "REAL_CAPTURED_CROSS_VENUE"

# Read-only public endpoints. No authentication is used or accepted anywhere in this module.
BITSO_TICKER_URL = "https://api.bitso.com/v3/ticker/?book={book}"
BINANCE_BOOK_TICKER_URL = ("https://api.binance.com/api/v3/ticker/bookTicker?symbol={symbol}")
BITSO_ORDER_BOOK_URL = "https://api.bitso.com/v3/order_book/?book={book}"
BINANCE_DEPTH_URL = "https://api.binance.com/api/v3/depth?symbol={symbol}&limit={limit}"

DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_ORDER_CAPACITY_MXN = Decimal("11")
DEFAULT_RETENTION_HOURS = 72
# Bounded storage, matching the project's existing collector policy: capture must not be able to
# fill a disk by being left running.
DEFAULT_MAX_TOTAL_BYTES = 64 * 1024 * 1024


class CrossVenueCaptureError(RuntimeError):
    """A capture condition the collector refuses to paper over."""


class QuoteSource(Protocol):
    """Something that can read one venue's top of book. Read-only by construction."""

    venue: str

    def top_of_book(self, symbol: str, base_asset: str, quote_asset: str) -> Quote:
        ...


def _fetch_json(url: str, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Any:
    """Read one public JSON document, refusing anything that is not an https URL.

    The scheme check is not decoration. `urllib.request.urlopen` will happily open a `file://`
    path or resolve a custom scheme, so a URL that ever became caller-influenced could read local
    files rather than a venue's public endpoint. Every URL in this module is a fixed constant
    today, which is exactly why the guard belongs here rather than in a comment: it keeps holding
    if that ever stops being true. Bandit's B310 flags the underlying risk, and this resolves it
    rather than suppressing the warning.
    """
    if not url.startswith("https://"):
        raise CrossVenueCaptureError(
            f"refusing to fetch a non-https URL: {url.split(':', 1)[0]}://...")
    request = urllib.request.Request(
        url, headers={"User-Agent": "AutoFund-research/0.2.8"})
    # nosec B310 - the scheme is asserted to be https on the line above, so the risk this check
    # warns about (file:// or a custom scheme) cannot reach this call. Suppressing it is safe
    # *because* that assertion exists; without it this would be hiding a real issue. The comment
    # must sit on the flagged line, which is why it is attached to the statement rather than above.
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
        return json.loads(response.read().decode("utf-8"))


@dataclass(frozen=True, slots=True)
class BitsoPublicTopOfBook:
    """Bitso's public ticker endpoint. Public, unauthenticated, read-only."""

    venue: str = "Bitso"
    timeout: int = DEFAULT_TIMEOUT_SECONDS

    def top_of_book(self, symbol: str, base_asset: str, quote_asset: str) -> Quote:
        payload = _fetch_json(BITSO_TICKER_URL.format(book=symbol), timeout=self.timeout)
        ticker = payload["payload"]
        received = datetime.now(UTC)
        bid = Decimal(str(ticker["bid"]))
        ask = Decimal(str(ticker["ask"]))
        quality = VALID
        if bid <= ZERO or ask <= ZERO:
            quality = "BROKEN_QUOTE"
        return Quote(
            venue=self.venue, symbol=symbol, base_asset=base_asset, quote_asset=quote_asset,
            event_time=received, received_time=received, bid=bid, ask=ask, quality=quality,
            provenance=REAL_CAPTURED_CROSS_VENUE)


@dataclass(frozen=True, slots=True)
class BinancePublicTopOfBook:
    """Binance's public bookTicker endpoint. Public, unauthenticated, read-only."""

    venue: str = "Binance"
    timeout: int = DEFAULT_TIMEOUT_SECONDS

    def top_of_book(self, symbol: str, base_asset: str, quote_asset: str) -> Quote:
        payload = _fetch_json(BINANCE_BOOK_TICKER_URL.format(symbol=symbol), timeout=self.timeout)
        received = datetime.now(UTC)
        try:
            bid = Decimal(str(payload["bidPrice"]))
            ask = Decimal(str(payload["askPrice"]))
        except (KeyError, ArithmeticError):
            # A symbol that is halted or delisted returns an empty object rather than an error.
            raise CrossVenueCaptureError(f"{symbol} returned no usable quote") from None
        quality = VALID
        if bid <= ZERO or ask <= ZERO:
            quality = "BROKEN_QUOTE"
        return Quote(
            venue=self.venue, symbol=symbol, base_asset=base_asset, quote_asset=quote_asset,
            event_time=received, received_time=received, bid=bid, ask=ask, quality=quality,
            provenance=REAL_CAPTURED_CROSS_VENUE)


@dataclass(frozen=True, slots=True)
class DepthProbe:
    """Depth available at or better than the incumbent's executable price, at the order cap.

    The spec's section 25 requires that depth at the actual size be verified rather than assumed.
    At 11 MXN the answer is almost always "the whole order fits inside the touch", but stating
    that from a measurement is different from assuming it, and the field records which happened.
    """

    venue: str
    symbol: str
    side: str
    requested_mxn: Decimal
    available_mxn: Decimal
    sufficient: bool
    levels_consumed: int
    measured_at: datetime

    def public(self) -> dict[str, Any]:
        return {"venue": self.venue, "symbol": self.symbol, "side": self.side,
                "requested_mxn": str(self.requested_mxn),
                "available_mxn": str(self.available_mxn),
                "sufficient": self.sufficient, "levels_consumed": self.levels_consumed,
                "measured_at": self.measured_at.isoformat()}


def measure_bitso_depth(*, book: str, side: str, requested_mxn: Decimal,
                        timeout: int = DEFAULT_TIMEOUT_SECONDS) -> DepthProbe:
    """Walk Bitso's public order book until the requested notional is covered.

    Read-only. Records how many levels were consumed, because consuming many levels means the
    executable price is worse than the touch and the dislocation measured at the touch would not
    have been achievable at size.
    """
    if requested_mxn <= ZERO:
        raise CrossVenueCaptureError("requested_mxn must be positive")
    if side not in ("BUY", "SELL"):
        raise CrossVenueCaptureError("side must be BUY or SELL")
    payload = _fetch_json(BITSO_ORDER_BOOK_URL.format(book=book), timeout=timeout)
    book_payload = payload["payload"]
    # A BUY consumes asks; a SELL consumes bids.
    raw = book_payload["asks"] if side == "BUY" else book_payload["bids"]
    accumulated = ZERO
    levels = 0
    for level in raw:
        price = Decimal(str(level["price"]))
        amount = Decimal(str(level["amount"]))
        accumulated += price * amount
        levels += 1
        if accumulated >= requested_mxn:
            break
    return DepthProbe(venue="Bitso", symbol=book, side=side, requested_mxn=requested_mxn,
                      available_mxn=accumulated, sufficient=accumulated >= requested_mxn,
                      levels_consumed=levels, measured_at=datetime.now(UTC))


def measure_binance_depth(*, symbol: str, side: str, requested_mxn: Decimal,
                          limit: int = 100,
                          timeout: int = DEFAULT_TIMEOUT_SECONDS) -> DepthProbe:
    """Walk Binance's public order book until the requested notional is covered. Read-only."""
    if requested_mxn <= ZERO:
        raise CrossVenueCaptureError("requested_mxn must be positive")
    payload = _fetch_json(BINANCE_DEPTH_URL.format(symbol=symbol, limit=limit), timeout=timeout)
    raw = payload["asks"] if side == "BUY" else payload["bids"]
    accumulated = ZERO
    levels = 0
    for entry in raw:
        price = Decimal(str(entry[0]))
        amount = Decimal(str(entry[1]))
        accumulated += price * amount
        levels += 1
        if accumulated >= requested_mxn:
            break
    return DepthProbe(venue="Binance", symbol=symbol, side=side, requested_mxn=requested_mxn,
                      available_mxn=accumulated, sufficient=accumulated >= requested_mxn,
                      levels_consumed=levels, measured_at=datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class CrossVenuePair:
    """One incumbent market against one reference, both quoted in the same currency.

    The pair is validated once, at construction, so every observation taken from it is
    structurally same-quote. A configuration that required conversion would be rejected here
    rather than silently producing a number with an FX leg hidden inside it.
    """

    incumbent_symbol: str
    reference_symbol: str
    base_asset: str
    quote_asset: str

    def __post_init__(self) -> None:
        if not self.incumbent_symbol or not self.reference_symbol:
            raise CrossVenueCaptureError("both symbols are required")
        if not self.base_asset or not self.quote_asset:
            raise CrossVenueCaptureError("base and quote assets are required")

    @property
    def key(self) -> str:
        return f"{self.base_asset}/{self.quote_asset}"


def default_pairs() -> tuple[CrossVenuePair, ...]:
    """The reference set for this milestone, from what 0.2.7 established is actually available.

    XRP is deliberately absent from the reference side: Binance's XRPMXN pair was not trading
    during development, so there is no reference market to compare Bitso's xrp_mxn against. Adding
    it would produce an empty comparison rather than a result, and pretending otherwise would
    inflate the apparent coverage of the study.
    """
    return (
        CrossVenuePair(incumbent_symbol="btc_mxn", reference_symbol="BTCMXN",
                       base_asset="BTC", quote_asset="MXN"),
        CrossVenuePair(incumbent_symbol="eth_mxn", reference_symbol="ETHMXN",
                       base_asset="ETH", quote_asset="MXN"),
        CrossVenuePair(incumbent_symbol="sol_mxn", reference_symbol="SOLMXN",
                       base_asset="SOL", quote_asset="MXN"),
    )


def reference_unavailable() -> tuple[CrossVenuePair, ...]:
    """Pairs that exist on the incumbent but have no usable reference market.

    Recorded explicitly so the report can say why XRP is missing instead of leaving a gap that
    looks like an oversight or, worse, like evidence that XRP showed nothing.
    """
    return (CrossVenuePair(incumbent_symbol="xrp_mxn", reference_symbol="XRPMXN",
                           base_asset="XRP", quote_asset="MXN"),)


@dataclass(frozen=True, slots=True)
class CaptureRecord:
    """One persisted cross-venue observation, with everything needed to audit it later."""

    moment: datetime
    pair: str
    incumbent: dict[str, Any]
    reference: dict[str, Any]
    skew_seconds: str
    validity: str
    provenance: str = REAL_CAPTURED_CROSS_VENUE

    def public(self) -> dict[str, Any]:
        return {"moment": self.moment.isoformat(), "pair": self.pair,
                "incumbent": self.incumbent, "reference": self.reference,
                "skew_seconds": self.skew_seconds, "validity": self.validity,
                "provenance": self.provenance}


@dataclass(frozen=True, slots=True)
class CrossVenueCaptureReport:
    """What a capture run actually collected, including what it failed to collect."""

    started_at: datetime
    finished_at: datetime
    cycles: int
    records: int
    per_pair: dict[str, int]
    failures: dict[str, int]
    valid_records: int
    skew_median_seconds: str | None
    provenance: str = REAL_CAPTURED_CROSS_VENUE

    @property
    def duration_seconds(self) -> Decimal:
        return Decimal(str((self.finished_at - self.started_at).total_seconds()))

    def public(self) -> dict[str, Any]:
        return {"started_at": self.started_at.isoformat(),
                "finished_at": self.finished_at.isoformat(),
                "duration_seconds": str(self.duration_seconds), "cycles": self.cycles,
                "records": self.records, "per_pair": dict(self.per_pair),
                "failures": dict(self.failures), "valid_records": self.valid_records,
                "skew_median_seconds": self.skew_median_seconds,
                "provenance": self.provenance}


class CrossVenueStore:
    """Bounded append-only JSONL storage for captured observations.

    Deliberately the same shape as the project's existing single-venue store rather than a new
    design: the spec asks that compatible infrastructure be reused and previous storage not be
    redesigned. Bounded because an unattended capture must not be able to fill a disk, and because
    a retention period is a clearer guarantee than a hope.
    """

    def __init__(self, root: Path, *,
                 max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES) -> None:
        if max_total_bytes <= 0:
            raise CrossVenueCaptureError("max_total_bytes must be positive")
        self.root = Path(root)
        self.max_total_bytes = max_total_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, pair_key: str) -> Path:
        safe = pair_key.replace("/", "_").lower()
        return self.root / f"{safe}.00000.jsonl"

    def append(self, *, pair_key: str, record: CaptureRecord) -> Path:
        path = self.path_for(pair_key)
        if path.exists() and path.stat().st_size >= self.max_total_bytes:
            raise CrossVenueCaptureError(
                f"{path.name} reached its size bound; capture stops rather than growing unbounded")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.public(), default=str) + "\n")
        return path

    def total_bytes(self) -> int:
        return sum(path.stat().st_size for path in self.root.glob("*.jsonl"))

    def read(self, *, pair_key: str) -> list[dict[str, Any]]:
        path = self.path_for(pair_key)
        if not path.exists():
            return []
        out: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out


class CrossVenueCollector:
    """Polls the incumbent and each reference, pairing the reads into synchronized observations.

    Ordering is deliberate: the incumbent is read first, so the reference read happens later and
    the measured skew is always "how stale was the incumbent when the reference was read". Reading
    the reference first would let a fresh reference be compared against an incumbent that had
    already moved, which is the direction that manufactures a false dislocation.
    """

    def __init__(self, *, incumbent: QuoteSource, references: dict[str, QuoteSource],
                 store: CrossVenueStore,
                 pairs: tuple[CrossVenuePair, ...] | None = None,
                 timeout: int = DEFAULT_TIMEOUT_SECONDS) -> None:
        if not references:
            raise CrossVenueCaptureError("at least one reference source is required")
        self.incumbent = incumbent
        self.references = references
        self.store = store
        self.pairs = pairs or default_pairs()
        self.timeout = timeout

    def capture_cycle(self) -> tuple[CaptureRecord, ...]:
        """One synchronized read of every configured pair. Failures are recorded, not raised."""
        records: list[CaptureRecord] = []
        for pair in self.pairs:
            started = time.perf_counter()
            try:
                incumbent_quote = self.incumbent.top_of_book(
                    pair.incumbent_symbol, pair.base_asset, pair.quote_asset)
            except Exception as exc:
                records.append(CaptureRecord(
                    moment=datetime.now(UTC), pair=pair.key,
                    incumbent={"error": type(exc).__name__, "venue": self.incumbent.venue},
                    reference={}, skew_seconds="0", validity="BROKEN_QUOTE"))
                continue
            for source in self.references.values():
                try:
                    reference_quote = source.top_of_book(
                        pair.reference_symbol, pair.base_asset, pair.quote_asset)
                except Exception as exc:
                    records.append(CaptureRecord(
                        moment=datetime.now(UTC), pair=pair.key,
                        incumbent=_quote_public(incumbent_quote),
                        reference={"error": type(exc).__name__, "venue": source.venue},
                        skew_seconds="0", validity="BROKEN_QUOTE"))
                    continue
                skew = Decimal(str(time.perf_counter() - started))
                records.append(CaptureRecord(
                    moment=reference_quote.received_time, pair=pair.key,
                    incumbent=_quote_public(incumbent_quote),
                    reference=_quote_public(reference_quote),
                    skew_seconds=str(skew),
                    validity=_validity_of(incumbent_quote, reference_quote)))
        return tuple(records)

    def run(self, *, cycles: int, cycle_interval_seconds: float = 5.0,
            on_cycle: Any = None) -> CrossVenueCaptureReport:
        """Run a bounded capture. Never unbounded: an unattended loop is not a research tool."""
        if cycles <= 0:
            raise CrossVenueCaptureError("cycles must be positive")
        started_at = datetime.now(UTC)
        per_pair: dict[str, int] = {}
        failures: dict[str, int] = {}
        valid = 0
        total = 0
        skews: list[Decimal] = []
        for index in range(cycles):
            for record in self.capture_cycle():
                total += 1
                per_pair[record.pair] = per_pair.get(record.pair, 0) + 1
                if record.validity == VALID:
                    valid += 1
                    skews.append(Decimal(record.skew_seconds))
                else:
                    failures[record.validity] = failures.get(record.validity, 0) + 1
                self.store.append(pair_key=record.pair, record=record)
            if on_cycle is not None:
                on_cycle(index + 1, cycles)
            if index < cycles - 1:
                time.sleep(cycle_interval_seconds)
        skew_median: str | None = None
        if skews:
            ordered = sorted(skews)
            skew_median = str(ordered[len(ordered) // 2])
        return CrossVenueCaptureReport(
            started_at=started_at, finished_at=datetime.now(UTC), cycles=cycles,
            records=total, per_pair=per_pair, failures=failures, valid_records=valid,
            skew_median_seconds=skew_median)


def _quote_public(quote: Quote) -> dict[str, Any]:
    return {"venue": quote.venue, "symbol": quote.symbol, "bid": str(quote.bid),
            "ask": str(quote.ask), "mid": str(quote.mid),
            "spread_bps": str(quote.spread_bps),
            "event_time": quote.event_time.isoformat(),
            "received_time": quote.received_time.isoformat(),
            "quality": quote.quality, "provenance": quote.provenance}


def _validity_of(incumbent: Quote, reference: Quote) -> str:
    """Classify a captured pair without needing a full observation object."""
    if incumbent.quality != VALID or reference.quality != VALID:
        return "BROKEN_QUOTE"
    if incumbent.ask < incumbent.bid or reference.ask < reference.bid:
        return "CROSSED_BOOK"
    return VALID


def observations_from_store(store: CrossVenueStore, *, pair_key: str,
                            max_skew_seconds: Decimal,
                            evidence: str
                            ) -> tuple[Any, ...]:
    """Rebuild `CrossVenueObservation` objects from captured records.

    The staleness ceiling is applied here from the *measured* skew, so a record whose two reads
    were further apart than the declared maximum is excluded rather than averaged in. Records that
    captured an error are skipped: an error is not an observation.
    """
    from .cross_venue_dislocation import CrossVenueObservation

    out: list[Any] = []
    base, quote = (*pair_key.split("/"), "MXN")[:2]
    for row in store.read(pair_key=pair_key):
        incumbent = row.get("incumbent") or {}
        reference = row.get("reference") or {}
        if "error" in incumbent or "error" in reference:
            continue
        try:
            incumbent_quote = Quote(
                venue=incumbent["venue"], symbol=incumbent["symbol"], base_asset=base,
                quote_asset=quote,
                event_time=datetime.fromisoformat(incumbent["received_time"]),
                received_time=datetime.fromisoformat(incumbent["received_time"]),
                bid=Decimal(incumbent["bid"]), ask=Decimal(incumbent["ask"]),
                quality=incumbent.get("quality", VALID),
                provenance=incumbent.get("provenance", REAL_CAPTURED_CROSS_VENUE))
            reference_quote = Quote(
                venue=reference["venue"], symbol=reference["symbol"], base_asset=base,
                quote_asset=quote,
                event_time=datetime.fromisoformat(reference["received_time"]),
                received_time=datetime.fromisoformat(reference["received_time"]),
                bid=Decimal(reference["bid"]), ask=Decimal(reference["ask"]),
                quality=reference.get("quality", VALID),
                provenance=reference.get("provenance", REAL_CAPTURED_CROSS_VENUE))
        except (KeyError, ValueError, ArithmeticError):
            continue
        # The recorded skew is a real elapsed measurement between the two HTTP reads. The two
        # receive timestamps cannot substitute for it: they are both stamped after their read
        # returns, so at polling granularity their difference collapses toward zero and the
        # comparison would look far more simultaneous than it was. The measurement is therefore
        # carried through explicitly rather than recomputed.
        measured_skew = _measured_skew(row)
        out.append(CrossVenueObservation(
            incumbent=incumbent_quote, reference=reference_quote, evidence=evidence,
            max_skew_seconds=max_skew_seconds, measured_skew_seconds=measured_skew))
    return tuple(out)


def _measured_skew(row: dict[str, Any]) -> Decimal | None:
    """Read the persisted inter-read elapsed time, if the record carried one."""
    raw = row.get("skew_seconds")
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except ArithmeticError:  # pragma: no cover - a corrupt field is treated as unmeasured
        return None

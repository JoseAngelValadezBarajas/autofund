"""Real historical market data backfill, and evidence provenance.

This module is the bridge MVP 0.2 exists to build.

MVP 0.1.4 could shadow-evaluate, but only had local capture and synthetic fixtures,
so no market/profile pair could accumulate enough *real* evidence to certify. Waiting
weeks for local capture is not a mechanism, it is an absence of one. The exchange
publishes historical OHLC candles publicly, so the same deterministic strategy that
would trade live can be evaluated over real exchange data now.

What this buys, and what it does not:

- **Real prices, real times.** Candles come from the exchange's own OHLC endpoint.
  `bucket_ms` is the exchange's bucket identifier and is preserved verbatim, so a
  candle can never be silently re-labelled by a local clock.
- **No lookahead, structurally.** A bucket labelled T covers [T, T+bucket) and is
  only known at T+bucket. The series is sorted by `bucket_ms` and a decision at index
  N sees only `[0..N]`, exactly as in replay.
- **Gaps are reported, never interpolated.** A missing bucket is a gap. Filling it
  with a fabricated candle would invent prices that never traded, so gaps are counted
  and surfaced.
- **No order-book history.** The exchange does not publish historical depth, so
  historical evidence carries *modelled* friction from observed spread and a stated
  slippage assumption, and is labelled as such. Live shadow evaluation, which does
  have a real book, remains the stronger evidence for execution assumptions. This
  limitation is reported rather than hidden.

Evidence provenance is explicit and load-bearing: only `REAL_CAPTURED` and
`REAL_HISTORICAL` may contribute to Production certification. `SYNTHETIC_FIXTURE`
tests the architecture and can never certify anything.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from autofund.decimal_utils import ZERO, financial
from autofund.observer.models import OhlcCandle
from autofund.replay.data import Candle
from autofund.replay.serialization import fingerprint

BACKFILL_VERSION = "autofund.historical-backfill.v1"

# Provenance classes. The distinction is the whole point of this module.
REAL_CAPTURED = "REAL_CAPTURED"
REAL_HISTORICAL = "REAL_HISTORICAL"
SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"

# Provenance that may contribute toward Production certification.
CERTIFYING_PROVENANCE = frozenset({REAL_CAPTURED, REAL_HISTORICAL})
NON_CERTIFYING_PROVENANCE = frozenset({SYNTHETIC_FIXTURE})

OHLC_BUCKET_SECONDS = 60


class OhlcSource(Protocol):
    """Public OHLC reader. Implemented by the strict GET-only observer client."""

    def ohlc(self, book: str, *, time_bucket: int = ..., start_ms: int | None = ...,
             end_ms: int | None = ..., limit: int | None = ...) -> tuple[OhlcCandle, ...]: ...


@dataclass(frozen=True, slots=True)
class Provenance:
    """Where a piece of evidence came from. Required on every evaluation."""

    kind: str
    source: str
    detail: str = ""

    def __post_init__(self) -> None:
        if self.kind not in CERTIFYING_PROVENANCE | NON_CERTIFYING_PROVENANCE:
            raise ValueError(f"unknown evidence provenance: {self.kind}")

    @property
    def may_certify(self) -> bool:
        """Whether this evidence may contribute toward Production certification."""
        return self.kind in CERTIFYING_PROVENANCE

    @property
    def is_real(self) -> bool:
        return self.kind in CERTIFYING_PROVENANCE

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "source": self.source, "detail": self.detail,
                "may_certify": self.may_certify, "is_real": self.is_real}


def provenance_for_source(source: str) -> Provenance:
    """Map an internal series source label onto a certification provenance.

    Kept as one function so a new data source cannot acquire certifying status by
    accident: it has to be classified here explicitly.
    """
    if source == "CAPTURED_MARKET_DATA":
        return Provenance(REAL_CAPTURED, source, "AutoFund-recorded live market observations")
    if source == "EXCHANGE_HISTORICAL_OHLC":
        return Provenance(REAL_HISTORICAL, source, "Exchange-published historical candles")
    if source == "SYNTHETIC_FIXTURE":
        return Provenance(SYNTHETIC_FIXTURE, source, "Deterministic fixture; tests architecture only")
    return Provenance(SYNTHETIC_FIXTURE, source or "UNKNOWN",
                      "Unclassified source is treated as non-certifying")


@dataclass(frozen=True, slots=True)
class BackfillWindow:
    """The requested span, resolved so a replay can be reproduced exactly."""

    book: str
    start_ms: int
    end_ms: int
    time_bucket: int

    @property
    def expected_candles(self) -> int:
        span = max(0, self.end_ms - self.start_ms)
        return span // (self.time_bucket * 1000)

    def public(self) -> dict[str, Any]:
        return {"book": self.book, "start_ms": self.start_ms, "end_ms": self.end_ms,
                "time_bucket": self.time_bucket,
                "start_at": datetime.fromtimestamp(self.start_ms / 1000, tz=UTC).isoformat(),
                "end_at": datetime.fromtimestamp(self.end_ms / 1000, tz=UTC).isoformat(),
                "expected_candles": self.expected_candles}


def window(*, book: str, lookback_hours: int, now: datetime | None = None,
           time_bucket: int = OHLC_BUCKET_SECONDS) -> BackfillWindow:
    """A closed historical window ending at the last *completed* bucket.

    The end is floored to a bucket boundary and excludes the in-progress bucket, so
    backfill can never include a candle that is still forming. That is a lookahead
    guard at the source: a partially-formed bucket would let a decision see the
    future of its own interval.
    """
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    bucket_ms = time_bucket * 1000
    end_ms = (int(moment.timestamp() * 1000) // bucket_ms) * bucket_ms
    start_ms = end_ms - lookback_hours * 3600 * 1000
    return BackfillWindow(book=book, start_ms=start_ms, end_ms=end_ms, time_bucket=time_bucket)


@dataclass(frozen=True, slots=True)
class HistoricalSeries:
    """Real historical candles with their integrity facts and provenance.

    Integrity is reported, not asserted: the caller can see the gap count, the
    duplicate count and whether the series is contiguous, instead of trusting a
    boolean.
    """

    market: str
    candles: tuple[Candle, ...]
    provenance: Provenance
    window: BackfillWindow
    gaps: tuple[int, ...] = ()
    duplicates_dropped: int = 0
    out_of_order_dropped: int = 0
    incomplete_tail: bool = False

    @property
    def bucket_seconds(self) -> int:
        return self.window.time_bucket

    @property
    def is_contiguous(self) -> bool:
        return not self.gaps

    @property
    def span_hours(self) -> Decimal:
        if not self.candles:
            return ZERO
        first = self.candles[0].timestamp
        last = self.candles[-1].timestamp
        return Decimal((last - first).total_seconds()) / Decimal("3600")

    @property
    def fingerprint(self) -> str:
        """Deterministic identity of the exact series, for reproducible evidence."""
        return fingerprint({
            "schema": BACKFILL_VERSION, "market": self.market,
            "provenance": self.provenance.kind,
            "window": self.window.public(),
            "candles": [[c.timestamp.isoformat(), str(c.open), str(c.high), str(c.low),
                         str(c.close), str(c.volume)] for c in self.candles],
        })

    def telemetry(self) -> dict[str, Any]:
        return {"version": BACKFILL_VERSION, "market": self.market,
                "provenance": self.provenance.public(), "window": self.window.public(),
                "candles": len(self.candles), "gaps": len(self.gaps),
                "gap_buckets": list(self.gaps[:20]),
                "duplicates_dropped": self.duplicates_dropped,
                "out_of_order_dropped": self.out_of_order_dropped,
                "incomplete_tail": self.incomplete_tail,
                "contiguous": self.is_contiguous, "span_hours": str(self.span_hours),
                "dataset_fingerprint": self.fingerprint}


@financial
def normalise_ohlc(candles: tuple[OhlcCandle, ...], *, time_bucket: int) -> tuple[
        tuple[Candle, ...], tuple[int, ...], int, int]:
    """Convert exchange candles into replay candles, reporting integrity issues.

    Returns `(candles, gap_bucket_indices, duplicates_dropped, out_of_order_dropped)`.

    Duplicates are dropped rather than rejected here because the exchange may repeat
    a bucket across paginated requests, which is a known artifact of windowed reads,
    not a market contradiction. The count is reported so the caller can see it
    happened.

    Gaps are *identified*, never filled. A bucket with no trade may legitimately be
    absent; inserting a candle would fabricate a price that never existed.
    """
    ordered = sorted(candles, key=lambda candle: candle.bucket_ms)
    unique: list[OhlcCandle] = []
    duplicates = 0
    out_of_order = 0
    previous: int | None = None
    for candle in ordered:
        if previous is not None and candle.bucket_ms == previous:
            duplicates += 1
            continue
        if previous is not None and candle.bucket_ms < previous:
            out_of_order += 1
        unique.append(candle)
        previous = candle.bucket_ms
    step_ms = time_bucket * 1000
    gaps: list[int] = []
    for index in range(1, len(unique)):
        delta = unique[index].bucket_ms - unique[index - 1].bucket_ms
        if delta > step_ms:
            missing = (delta // step_ms) - 1
            gaps.extend(range(1, missing + 1))
    converted = tuple(
        Candle(candle.opened_at, candle.open, candle.high, candle.low, candle.close,
               candle.volume)
        for candle in unique)
    return converted, tuple(gaps), duplicates, out_of_order


def page_budget(*, lookback_hours: int, time_bucket: int = OHLC_BUCKET_SECONDS,
                candles_per_page: int = 1440, extra: int = 2) -> int:
    """How many pages a lookback needs, so `max_requests` never silently truncates.

    A hard-coded page count is a footgun: ask for 30 days, pass a budget that covers
    three, and the function still returns successfully with a tenth of the data. The
    caller would then certify against a window it did not intend. Deriving the budget
    from the request means the only way to shorten the fetch is to shorten `hours`.
    """
    if lookback_hours <= 0:
        raise ValueError("lookback_hours must be positive")
    buckets = (lookback_hours * 3600) // time_bucket
    return max(1, buckets // candles_per_page + extra)


def fetch_historical_series(*, source: OhlcSource, book: str, lookback_hours: int,
                            now: datetime | None = None,
                            time_bucket: int = OHLC_BUCKET_SECONDS,
                            max_requests: int | None = None,
                            candles_per_page: int = 1440) -> HistoricalSeries:
    """Fetch real historical candles in bounded, chronological pages.

    Paging forward from the window start guarantees chronological ordering and means
    a truncated fetch yields a *shorter but still valid* prefix rather than a series
    with holes in the middle. `max_requests` bounds the work; when it is omitted it is
    derived from the requested span so the fetch cannot silently under-deliver.
    """
    if max_requests is None:
        max_requests = page_budget(lookback_hours=lookback_hours, time_bucket=time_bucket,
                                   candles_per_page=candles_per_page)
    if max_requests <= 0:
        raise ValueError("max_requests must be positive")
    resolved_book = book.lower()
    window_spec = window(book=resolved_book, lookback_hours=lookback_hours, now=now,
                         time_bucket=time_bucket)
    step_ms = time_bucket * 1000
    page_ms = candles_per_page * step_ms
    collected: list[OhlcCandle] = []
    cursor = window_spec.start_ms
    requests = 0
    truncated = False
    while cursor < window_spec.end_ms and requests < max_requests:
        page_end = min(cursor + page_ms, window_spec.end_ms)
        page = source.ohlc(resolved_book, time_bucket=time_bucket, start_ms=cursor,
                           end_ms=page_end)
        collected.extend(page)
        requests += 1
        if not page:
            cursor = page_end
            continue
        last = max(candle.bucket_ms for candle in page)
        cursor = max(page_end, last + step_ms)
    if cursor < window_spec.end_ms:
        truncated = True
    candles, gaps, duplicates, out_of_order = normalise_ohlc(tuple(collected),
                                                             time_bucket=time_bucket)
    return HistoricalSeries(
        market=resolved_book.upper().replace("_", "/"), candles=candles,
        provenance=Provenance(REAL_HISTORICAL, "EXCHANGE_HISTORICAL_OHLC",
                              f"{requests} bounded public OHLC pages"),
        window=window_spec, gaps=gaps, duplicates_dropped=duplicates,
        out_of_order_dropped=out_of_order, incomplete_tail=truncated)


@dataclass(frozen=True, slots=True)
class HistoricalMarketBook:
    """Execution context for historical replay.

    Historical depth is not published, so depth is a *stated assumption* rather than
    an observation. `depth_mxn` and `slippage_bps` record that so the limitation
    travels with the evidence instead of being silently assumed, and `spread_bps` is
    the observed spread the caller measured live.
    """

    spread_bps: Decimal
    depth_mxn: Decimal
    slippage_bps: Decimal = Decimal("5")
    minimum_value_mxn: Decimal = Decimal("1")
    depth_is_observed: bool = False
    spread_observed_at: str = ""

    def public(self) -> dict[str, Any]:
        return {"spread_bps": str(self.spread_bps), "depth_mxn": str(self.depth_mxn),
                "slippage_bps": str(self.slippage_bps),
                "minimum_value_mxn": str(self.minimum_value_mxn),
                "depth_is_observed": self.depth_is_observed,
                "spread_observed_at": self.spread_observed_at}


__all__ = [
    "BACKFILL_VERSION",
    "CERTIFYING_PROVENANCE",
    "NON_CERTIFYING_PROVENANCE",
    "OHLC_BUCKET_SECONDS",
    "REAL_CAPTURED",
    "REAL_HISTORICAL",
    "SYNTHETIC_FIXTURE",
    "BackfillWindow",
    "HistoricalMarketBook",
    "HistoricalSeries",
    "OhlcSource",
    "Provenance",
    "fetch_historical_series",
    "normalise_ohlc",
    "provenance_for_source",
    "window",
]

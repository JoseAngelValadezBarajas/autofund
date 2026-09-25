"""Deterministic aggregation of 1-minute candles into coarser trading horizons.

MVP 0.2.3 ended with a specific, quantified obstacle: at account-confirmed taker fees a
round trip costs ~173 bps, so a strategy must find a gross move materially larger than
that merely to break even. Every profile the project has built so far operates on 1-minute
bars, where the typical move is a fraction of that. This module exists to test the obvious
alternative honestly: **is the trading horizon itself the problem?**

It does not decide that. It provides the aggregation such a test needs, with two
properties that are easy to get wrong and expensive to get wrong silently:

**Alignment is on wall-clock boundaries, not on the first bar seen.** A 15-minute bucket
starting at 00:07 would make every subsequent bucket depend on where the fetch happened to
begin, so the same market aggregated from two different windows would disagree. Buckets are
therefore aligned to epoch multiples of the timeframe, which is deterministic and
reproducible from any starting point.

**An incomplete bucket does not exist.** The final bucket of any fetch is almost always
partial, and aggregating it would produce a candle whose close is not yet a fact. A
strategy that acted on it would be reading a provisional price as if it were settled. Such
buckets are dropped and counted rather than filled or extrapolated, and the count is
reported so an unusually gappy market cannot hide behind a clean-looking result.

The third property is a consequence of the second: because only complete buckets are
returned, and a bucket's label is its *opening* time, the earliest bar at which a decision
may be taken on a bucket is the bar **after** its close. `BarHorizon` exposes that
relationship explicitly so the replay cannot accidentally act inside a bucket it is still
forming.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial
from autofund.replay.data import Candle

HORIZON_VERSION = "autofund.time-horizon.v1"

# The predeclared horizons (spec section 3). Deliberately two, and deliberately not a
# sweep: the hypothesis is about *coarseness*, and testing a ladder of bar sizes while
# picking the winner would be data-mining bar widths rather than testing the hypothesis.
FIFTEEN_MINUTES = 15 * 60
ONE_HOUR = 60 * 60

BASE_TIMEFRAME_SECONDS = 60


class HorizonError(ValueError):
    """The aggregation was asked for something it cannot defend."""


@dataclass(frozen=True, slots=True)
class TimeframeSpec:
    """One predeclared horizon.

    `label` is the bucket's OPENING time. Stating that explicitly matters because the
    alternative convention (labelling by close) makes every decision look one bar earlier
    than it is, which is exactly the look-ahead this module exists to prevent.
    """

    name: str
    seconds: int

    def __post_init__(self) -> None:
        if self.seconds <= 0 or self.seconds % BASE_TIMEFRAME_SECONDS != 0:
            raise HorizonError("timeframe must be a positive multiple of the base bar")
        if self.seconds < BASE_TIMEFRAME_SECONDS:
            raise HorizonError("timeframe must be at least the base bar")
        if not self.name:
            raise HorizonError("timeframe requires a name")

    @property
    def base_bars(self) -> int:
        """How many base bars a complete bucket must contain."""
        return self.seconds // BASE_TIMEFRAME_SECONDS

    @property
    def label_convention(self) -> str:
        return "BUCKET_OPEN"

    def bucket_start(self, moment: datetime) -> datetime:
        """The aligned bucket containing `moment`. Deterministic and fetch-independent."""
        stamp = int(moment.timestamp())
        return datetime.fromtimestamp(stamp - (stamp % self.seconds), tz=UTC)

    def public(self) -> dict[str, Any]:
        return {"version": HORIZON_VERSION, "name": self.name, "seconds": self.seconds,
                "base_bars": self.base_bars, "label_convention": self.label_convention}


FIFTEEN_MINUTE = TimeframeSpec(name="15m", seconds=FIFTEEN_MINUTES)
ONE_HOUR_TIMEFRAME = TimeframeSpec(name="1h", seconds=ONE_HOUR)

# The frozen horizon experiment: exactly these two.
PREDECLARED_TIMEFRAMES = (FIFTEEN_MINUTE, ONE_HOUR_TIMEFRAME)


@dataclass(frozen=True, slots=True)
class HorizonSeries:
    """Aggregated candles plus the honest account of what was dropped.

    Dropped counts are reported rather than absorbed. An aggregation that silently
    discarded a third of its buckets would still produce a plausible-looking series, and
    the resulting strategy evaluation would be measured on a dataset nobody had described.
    """

    timeframe: TimeframeSpec
    market: str
    candles: tuple[Candle, ...]
    source_bars: int
    complete_buckets: int
    incomplete_buckets: int
    incomplete_bucket_starts: tuple[int, ...]
    irregular_buckets: int

    # Base bars sharing a minute already present in the same bucket. Kept separate from
    # `incomplete_buckets` because the two call for opposite responses: a duplicate means the
    # source data is wrong and should be investigated, whereas an incomplete bucket at the
    # trailing edge is the ordinary consequence of a window ending mid-bucket. Collapsing
    # them into one count would hide a data defect inside a routine artefact.
    duplicate_bars: int = 0

    @property
    def drop_rate(self) -> Decimal:
        total = self.complete_buckets + self.incomplete_buckets
        if total <= 0:
            return ZERO
        return Decimal(self.incomplete_buckets) / Decimal(total)

    @property
    def first_close_at(self) -> datetime | None:
        """When the first returned bucket *settled*, i.e. the earliest decision time."""
        if not self.candles:
            return None
        return self.candles[0].timestamp + timedelta(seconds=self.timeframe.seconds)

    @property
    def last_close_at(self) -> datetime | None:
        if not self.candles:
            return None
        return self.candles[-1].timestamp + timedelta(seconds=self.timeframe.seconds)

    def public(self) -> dict[str, Any]:
        return {"version": HORIZON_VERSION, "market": self.market,
                "timeframe": self.timeframe.public(), "source_bars": self.source_bars,
                "complete_buckets": self.complete_buckets,
                "incomplete_buckets": self.incomplete_buckets,
                "irregular_buckets": self.irregular_buckets,
                "duplicate_bars": self.duplicate_bars,
                "drop_rate": str(self.drop_rate),
                "first_close_at": (None if self.first_close_at is None
                                   else self.first_close_at.isoformat()),
                "last_close_at": (None if self.last_close_at is None
                                  else self.last_close_at.isoformat()),
                "incomplete_buckets_aggregated": False,
                "gaps_repaired": False}


@financial
def aggregate_candles(*, candles: Sequence[Candle], timeframe: TimeframeSpec,
                      market: str = "") -> HorizonSeries:
    """Aggregate base bars into complete buckets of `timeframe`.

    Aggregation rules, applied literally:

        open   = first constituent bar's open
        high   = maximum constituent high
        low    = minimum constituent low
        close  = last constituent close
        volume = sum of constituent volume

    A bucket is emitted only when it contains exactly `timeframe.base_bars` constituent
    bars. Anything else is counted as incomplete and dropped, never partially emitted and
    never extrapolated: a bucket missing its last constituent has no settled close, so
    emitting it would present a provisional price as a fact.

    A base bar whose minute is already present in its bucket is a duplicate, not an extra
    constituent. It is counted separately and the bucket is left one constituent short, so
    a duplicated minute degrades into a dropped bucket rather than silently compressing two
    hours of price action into one hour.
    """
    if not candles:
        return HorizonSeries(timeframe=timeframe, market=market, candles=(),
                             source_bars=0, complete_buckets=0, incomplete_buckets=0,
                             incomplete_bucket_starts=(), irregular_buckets=0,
                             duplicate_bars=0)

    buckets: dict[int, dict[int, Candle]] = {}
    irregular = 0
    duplicated = 0
    for candle in candles:
        offset = int(candle.timestamp.timestamp())
        if offset % BASE_TIMEFRAME_SECONDS != 0:
            # A base bar off the expected grid makes its bucket's membership ambiguous.
            irregular += 1
            continue
        start = int(timeframe.bucket_start(candle.timestamp).timestamp())
        row = buckets.setdefault(start, {})
        minute = offset // BASE_TIMEFRAME_SECONDS
        if minute in row:
            duplicated += 1
            continue
        row[minute] = candle

    expected = timeframe.base_bars
    aggregated: list[Candle] = []
    incomplete: list[int] = []
    for start in sorted(buckets):
        members = buckets[start]
        if len(members) != expected:
            incomplete.append(start)
            continue
        ordered = [members[minute] for minute in sorted(members)]
        aggregated.append(Candle(
            timestamp=datetime.fromtimestamp(start, tz=UTC),
            open=ordered[0].open,
            high=max(item.high for item in ordered),
            low=min(item.low for item in ordered),
            close=ordered[-1].close,
            volume=sum((item.volume for item in ordered), ZERO)))

    return HorizonSeries(
        timeframe=timeframe, market=market, candles=tuple(aggregated),
        source_bars=len(candles), complete_buckets=len(aggregated),
        incomplete_buckets=len(incomplete),
        incomplete_bucket_starts=tuple(incomplete), irregular_buckets=irregular,
        duplicate_bars=duplicated)


def decision_bar_index(*, series: HorizonSeries, settled_at: datetime) -> int:
    """The first aggregated bar a decision may use, given a settled time.

    Returns -1 when no bucket has settled by `settled_at`. The comparison is against the
    bucket's *close*, so a caller cannot accidentally act on a bucket that is still
    forming even if it holds a reference to that bucket's in-progress values.
    """
    if not series.candles:
        return -1
    span = timedelta(seconds=series.timeframe.seconds)
    index = -1
    for position, candle in enumerate(series.candles):
        if candle.timestamp + span <= settled_at:
            index = position
        else:
            break
    return index


__all__ = [
    "BASE_TIMEFRAME_SECONDS",
    "FIFTEEN_MINUTE",
    "FIFTEEN_MINUTES",
    "HORIZON_VERSION",
    "ONE_HOUR",
    "ONE_HOUR_TIMEFRAME",
    "PREDECLARED_TIMEFRAMES",
    "HorizonError",
    "HorizonSeries",
    "TimeframeSpec",
    "aggregate_candles",
    "decision_bar_index",
]

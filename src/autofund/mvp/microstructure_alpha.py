"""Order-book and trade-tape features, with markout measured strictly after observation.

This is the module the whole microstructure path depends on being honest, because the data it
consumes is the only evidence in the project that captures *sequence* rather than a price range
per interval. Three hazards are specific to it and each is handled explicitly.

**Markout must be measured after the observation, through the same book.** A markout is the
change in the executable price over a forward window, so it needs a *later* book snapshot than
the one the feature came from. Measuring it against the same snapshot, or against a trade at the
same instant, would produce a markout of exactly zero and a spread of exactly zero — a signal
that a leak exists is that the numbers look impossibly clean.

**Aggressor side is only used when the provider states it.** `TradeEvent.maker_side` is
authoritative on this venue, and the module records that provenance. Inferring a side from a
price move relative to the midpoint would be indistinguishable from the real field once stored,
and every adverse-selection conclusion rests on it.

**Queue position is not observable, and the output says so.** A passive fill's markout cannot be
measured exactly from public data, because whether a resting order would have been reached
depends on how much size sat ahead of it. The module therefore produces a *bound* and labels its
evidence class, rather than pretending to an exactness the data does not support.

Imbalance, depth changes and spread are all computed from the bid and ask levels the collector
stored, in Decimal, with no floating point anywhere in the path.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

from autofund.decimal_utils import ZERO, financial

from .microstructure import BookEvent, TradeEvent

MICROSTRUCTURE_ALPHA_VERSION = "autofund.microstructure-alpha.v1"

# ---- predeclared markout horizons (section 15) ----
# Frozen before evaluation. The short horizon is limited by the capture interval: a markout
# shorter than the sampling period cannot be measured, because no later book exists inside it.
# That constraint is stated rather than worked around, and it is why the short horizon is
# reported as a floor rather than as a choice.
PREDECLARED_MARKOUT_SECONDS: tuple[int, ...] = (5, 30, 60)

# The declared feature set (section 12). Small and interpretable: each is a statement about
# visible supply and demand, not a fitted quantity.
TOP_OF_BOOK_IMBALANCE = "TOP_OF_BOOK_IMBALANCE"
MULTI_LEVEL_DEPTH_IMBALANCE = "MULTI_LEVEL_DEPTH_IMBALANCE"
SPREAD_STATE = "SPREAD_STATE"
BID_DEPTH_CHANGE = "BID_DEPTH_CHANGE"
ASK_DEPTH_CHANGE = "ASK_DEPTH_CHANGE"
BOOK_PRESSURE = "BOOK_PRESSURE"
TRADE_FLOW_IMBALANCE = "TRADE_FLOW_IMBALANCE"
MICROPRICE_DISPLACEMENT = "MICROPRICE_DISPLACEMENT"

PREDECLARED_MICROSTRUCTURE_FEATURES: tuple[str, ...] = (
    TOP_OF_BOOK_IMBALANCE, MULTI_LEVEL_DEPTH_IMBALANCE, SPREAD_STATE,
    BID_DEPTH_CHANGE, ASK_DEPTH_CHANGE, BOOK_PRESSURE, TRADE_FLOW_IMBALANCE,
    MICROPRICE_DISPLACEMENT)

# ---- adverse-selection evidence classes (section 16) ----
TRADE_TAPE_SUPPORTED = "TRADE_TAPE_SUPPORTED"
BOOK_SUPPORTED_BOUND = "BOOK_SUPPORTED_BOUND"
INSUFFICIENT_QUEUE_EVIDENCE = "INSUFFICIENT_QUEUE_EVIDENCE"

# Minimum markout observations before a forward-markout statement is made at all.
MINIMUM_MARKOUT_OBSERVATIONS = 100

# Within this window of a book snapshot, trades are attributed to that snapshot's state. Trades
# outside it belong to a book the collector never saw, so attributing them would be assuming
# what the book looked like rather than observing it.
TRADE_ATTRIBUTION_SECONDS = 5


class MicrostructureAlphaError(ValueError):
    """Microstructure alpha was asked for something it cannot defend."""


@dataclass(frozen=True, slots=True)
class BookFeatures:
    """Features derived from one book snapshot, and nothing later than it."""

    book: str
    moment: datetime
    sequence: int | None
    best_bid: Decimal
    best_ask: Decimal
    midpoint: Decimal
    spread_bps: Decimal
    top_imbalance: Decimal
    depth_imbalance: Decimal
    bid_depth: Decimal
    ask_depth: Decimal
    microprice: Decimal | None
    microprice_displacement_bps: Decimal | None
    quality: str

    def public(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {"book": self.book, "moment": self.moment.isoformat(),
                "sequence": self.sequence, "best_bid": str(self.best_bid),
                "best_ask": str(self.best_ask), "midpoint": str(self.midpoint),
                "spread_bps": str(self.spread_bps),
                "top_imbalance": str(self.top_imbalance),
                "depth_imbalance": str(self.depth_imbalance),
                "bid_depth": str(self.bid_depth), "ask_depth": str(self.ask_depth),
                "microprice": s(self.microprice),
                "microprice_displacement_bps": s(self.microprice_displacement_bps),
                "quality": self.quality, "uses_only_this_snapshot": True}


@financial
def _imbalance(*, bid: Decimal, ask: Decimal) -> Decimal:
    """(bid - ask) / (bid + ask), defined as zero when neither side has any depth.

    Positive means more visible bid than ask supply. Zero rather than undefined for an empty
    two-sided book, because a missing side is not evidence of direction, and returning None
    would make the feature unmeasurable exactly at the moments the book is thinnest.
    """
    total = bid + ask
    if total <= ZERO:
        return ZERO
    return (bid - ask) / total


@financial
def features_from_book(*, event: BookEvent, levels: int = 5) -> BookFeatures:
    """Derive the imbalance and spread features from a single snapshot.

    `microprice` weights each side's best price by the *opposite* side's size, which is the
    standard construction: a large bid stack pulls the fair estimate up toward the ask. It is
    reported alongside its displacement from the plain midpoint because the displacement is what
    the markout test needs — a raw microprice is not comparable across assets with different
    price scales.
    """
    if not event.bids or not event.asks:
        raise MicrostructureAlphaError("a two-sided book is required")
    best_bid = event.best_bid
    best_ask = event.best_ask
    midpoint = (best_bid + best_ask) / Decimal("2")
    bid_depth = sum((level.price * level.quantity for level in event.bids[:levels]), ZERO)
    ask_depth = sum((level.price * level.quantity for level in event.asks[:levels]), ZERO)
    top_bid_size = event.bids[0].quantity
    top_ask_size = event.asks[0].quantity
    microprice: Decimal | None = None
    displacement: Decimal | None = None
    if top_bid_size + top_ask_size > ZERO:
        microprice = ((best_bid * top_ask_size + best_ask * top_bid_size)
                      / (top_bid_size + top_ask_size))
        if midpoint > ZERO:
            displacement = (microprice - midpoint) / midpoint * Decimal("10000")
    return BookFeatures(
        book=event.book, moment=event.exchange_timestamp, sequence=event.sequence,
        best_bid=best_bid, best_ask=best_ask, midpoint=midpoint,
        spread_bps=event.spread_bps,
        top_imbalance=_imbalance(bid=top_bid_size, ask=top_ask_size),
        depth_imbalance=_imbalance(bid=bid_depth, ask=ask_depth),
        bid_depth=bid_depth, ask_depth=ask_depth, microprice=microprice,
        microprice_displacement_bps=displacement, quality=event.quality)


@dataclass(frozen=True, slots=True)
class BookTransition:
    """What changed between two consecutive snapshots, and how long it took."""

    book: str
    from_moment: datetime
    to_moment: datetime
    elapsed_seconds: Decimal
    bid_depth_change: Decimal
    ask_depth_change: Decimal
    spread_change_bps: Decimal
    midpoint_change_bps: Decimal
    imbalance_persistence: Decimal

    def public(self) -> dict[str, Any]:
        return {"book": self.book, "from_moment": self.from_moment.isoformat(),
                "to_moment": self.to_moment.isoformat(),
                "elapsed_seconds": str(self.elapsed_seconds),
                "bid_depth_change": str(self.bid_depth_change),
                "ask_depth_change": str(self.ask_depth_change),
                "spread_change_bps": str(self.spread_change_bps),
                "midpoint_change_bps": str(self.midpoint_change_bps),
                "imbalance_persistence": str(self.imbalance_persistence),
                "uses_only_two_observed_snapshots": True}


@financial
def transitions(*, events: Sequence[BookEvent], levels: int = 5) -> tuple[BookTransition, ...]:
    """Consecutive-snapshot changes, measured only between snapshots that were actually seen.

    A gap in the capture is not interpolated. If two consecutive stored snapshots are far apart
    the transition is still recorded, but its `elapsed_seconds` carries the real spacing, so a
    change over a long gap is distinguishable from the same change over a short one. Smoothing
    the gap would present a slow drift as a fast shift.
    """
    ordered = sorted(events, key=lambda event: event.exchange_timestamp)
    out: list[BookTransition] = []
    for previous, current in pairwise(ordered):
        try:
            before = features_from_book(event=previous, levels=levels)
            after = features_from_book(event=current, levels=levels)
        except MicrostructureAlphaError:
            continue
        elapsed = Decimal(str(
            (current.exchange_timestamp - previous.exchange_timestamp).total_seconds()))
        if before.bid_depth <= ZERO or before.ask_depth <= ZERO:
            bid_change = ZERO
            ask_change = ZERO
        else:
            bid_change = (after.bid_depth - before.bid_depth) / before.bid_depth
            ask_change = (after.ask_depth - before.ask_depth) / before.ask_depth
        midpoint_change = ZERO
        if before.midpoint > ZERO:
            midpoint_change = ((after.midpoint - before.midpoint) / before.midpoint
                               * Decimal("10000"))
        # Persistence: the same sign of imbalance on both sides of the transition. Recorded as
        # a signed value so a persistent bid-side imbalance and a persistent ask-side one are
        # distinguishable rather than both counting as "persistent".
        persistence = (before.depth_imbalance if before.depth_imbalance
                       * after.depth_imbalance > ZERO else ZERO)
        out.append(BookTransition(
            book=current.book, from_moment=previous.exchange_timestamp,
            to_moment=current.exchange_timestamp, elapsed_seconds=elapsed,
            bid_depth_change=bid_change, ask_depth_change=ask_change,
            spread_change_bps=after.spread_bps - before.spread_bps,
            midpoint_change_bps=midpoint_change, imbalance_persistence=persistence))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class TradeFlow:
    """Signed trade flow over a window, from provider-stated aggressor sides only."""

    book: str
    window_start: datetime
    window_end: datetime
    trades: int
    buy_volume: Decimal
    sell_volume: Decimal
    imbalance: Decimal
    side_source: str

    def public(self) -> dict[str, Any]:
        return {"book": self.book, "window_start": self.window_start.isoformat(),
                "window_end": self.window_end.isoformat(), "trades": self.trades,
                "buy_volume": str(self.buy_volume), "sell_volume": str(self.sell_volume),
                "imbalance": str(self.imbalance), "side_source": self.side_source,
                "aggressor_inferred": False}


@financial
def trade_flow(*, trades: Sequence[TradeEvent], start: datetime,
               end: datetime) -> TradeFlow | None:
    """Signed volume imbalance over a window.

    `maker_side` is the side of the resting order, so a maker side of "sell" means the aggressor
    bought. The inversion is applied explicitly here rather than being left to a reader, because
    getting it backwards would invert every trade-flow conclusion while looking entirely
    plausible.

    A trade without a stated maker side is excluded rather than assigned one. Inferring the
    aggressor from a price move would produce a field indistinguishable from the real one once
    stored, and every adverse-selection conclusion rests on that field being real.
    """
    if end <= start:
        raise MicrostructureAlphaError("window end must follow its start")
    buy = ZERO
    sell = ZERO
    count = 0
    for trade in trades:
        if not (start <= trade.exchange_timestamp < end):
            continue
        if trade.maker_side is None:
            continue
        count += 1
        if trade.maker_side == "sell":
            buy += trade.quantity
        else:
            sell += trade.quantity
    if count == 0:
        return None
    return TradeFlow(book=trades[0].book, window_start=start, window_end=end, trades=count,
                     buy_volume=buy, sell_volume=sell,
                     imbalance=_imbalance(bid=buy, ask=sell),
                     side_source="PROVIDER_AUTHORITATIVE")


@dataclass(frozen=True, slots=True)
class Markout:
    """Forward price movement after an observation, measured through a later book.

    `late_midpoint` is the first observed midpoint at or after `horizon`, always strictly later
    than the feature's own snapshot. If no such snapshot exists the markout is not produced:
    a horizon the capture cannot reach is missing evidence, not a zero.
    """

    book: str
    observed_at: datetime
    horizon_seconds: int
    measured_at: datetime
    actual_elapsed_seconds: Decimal
    early_midpoint: Decimal
    late_midpoint: Decimal
    signed_markout_bps: Decimal
    favourable: bool

    def public(self) -> dict[str, Any]:
        return {"book": self.book, "observed_at": self.observed_at.isoformat(),
                "horizon_seconds": self.horizon_seconds,
                "measured_at": self.measured_at.isoformat(),
                "actual_elapsed_seconds": str(self.actual_elapsed_seconds),
                "early_midpoint": str(self.early_midpoint),
                "late_midpoint": str(self.late_midpoint),
                "signed_markout_bps": str(self.signed_markout_bps),
                "favourable": self.favourable,
                "measured_strictly_after_observation": True}


@financial
def markouts_for(*, features: BookFeatures, later: Sequence[BookFeatures],
                 horizons: Sequence[int] = PREDECLARED_MARKOUT_SECONDS,
                 direction: Decimal = ZERO) -> tuple[Markout, ...]:
    """Forward markouts for one observation, sign-adjusted by the position's direction.

    `direction` is +1 for a long-biased view (imbalance says the bid is heavy) and -1 for a
    short-biased one, so a positive markout always means "the move went the way the feature
    pointed". Sign-adjusting here rather than at the reporting layer keeps the convention in one
    place, where it can be tested.
    """
    if features.midpoint <= ZERO:
        return ()
    sign = direction if direction != ZERO else Decimal("1")
    out: list[Markout] = []
    for horizon in horizons:
        target = features.moment + timedelta(seconds=horizon)
        candidate: BookFeatures | None = None
        for other in later:
            if other.moment <= features.moment:
                continue
            if other.moment >= target:
                candidate = other
                break
        if candidate is None or candidate.midpoint <= ZERO:
            continue
        elapsed = Decimal(str((candidate.moment - features.moment).total_seconds()))
        raw = (candidate.midpoint - features.midpoint) / features.midpoint * Decimal("10000")
        signed = raw * sign
        out.append(Markout(book=features.book, observed_at=features.moment,
                           horizon_seconds=horizon, measured_at=candidate.moment,
                           actual_elapsed_seconds=elapsed, early_midpoint=features.midpoint,
                           late_midpoint=candidate.midpoint, signed_markout_bps=signed,
                           favourable=signed > ZERO))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class MarkoutSummary:
    """Distribution of forward markout for one feature and horizon."""

    feature_name: str
    book: str
    horizon_seconds: int
    observations: int
    mean_markout_bps: Decimal
    median_markout_bps: Decimal
    favourable_fraction: Decimal
    standard_error_bps: Decimal
    evidence_class: str
    queue_exact: bool

    @property
    def effect_multiple(self) -> Decimal | None:
        if self.standard_error_bps <= ZERO:
            return None
        return self.mean_markout_bps / self.standard_error_bps

    def public(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {"feature_name": self.feature_name, "book": self.book,
                "horizon_seconds": self.horizon_seconds, "observations": self.observations,
                "mean_markout_bps": str(self.mean_markout_bps),
                "median_markout_bps": str(self.median_markout_bps),
                "favourable_fraction": str(self.favourable_fraction),
                "standard_error_bps": str(self.standard_error_bps),
                "effect_multiple": s(self.effect_multiple),
                "evidence_class": self.evidence_class, "queue_exact": self.queue_exact,
                "queue_position_assumed": False}


@financial
def summarise_markouts(*, feature_name: str, book: str, horizon_seconds: int,
                       markouts: Sequence[Markout],
                       evidence_class: str = BOOK_SUPPORTED_BOUND,
                       ) -> MarkoutSummary:
    """Summarise one feature's markouts, refusing to state anything below the floor."""
    if horizon_seconds <= 0:
        raise MicrostructureAlphaError("horizon must be positive")
    if len(markouts) < MINIMUM_MARKOUT_OBSERVATIONS:
        return MarkoutSummary(
            feature_name=feature_name, book=book, horizon_seconds=horizon_seconds,
            observations=len(markouts), mean_markout_bps=ZERO, median_markout_bps=ZERO,
            favourable_fraction=ZERO, standard_error_bps=ZERO,
            evidence_class=INSUFFICIENT_QUEUE_EVIDENCE, queue_exact=False)
    values = [m.signed_markout_bps for m in markouts]
    mean = sum(values, ZERO) / Decimal(len(values))
    ordered = sorted(values)
    median = ordered[len(ordered) // 2]
    favourable = sum(1 for value in values if value > ZERO)
    variance = (sum(((value - mean) ** 2 for value in values), ZERO)
                / Decimal(len(values) - 1)) if len(values) > 1 else ZERO
    error = (variance.sqrt() / Decimal(len(values)).sqrt()) if variance > ZERO else ZERO
    return MarkoutSummary(
        feature_name=feature_name, book=book, horizon_seconds=horizon_seconds,
        observations=len(values), mean_markout_bps=mean, median_markout_bps=median,
        favourable_fraction=Decimal(favourable) / Decimal(len(values)),
        standard_error_bps=error, evidence_class=evidence_class, queue_exact=False)


__all__ = [
    "ASK_DEPTH_CHANGE",
    "BID_DEPTH_CHANGE",
    "BOOK_PRESSURE",
    "BOOK_SUPPORTED_BOUND",
    "INSUFFICIENT_QUEUE_EVIDENCE",
    "MICROPRICE_DISPLACEMENT",
    "MICROSTRUCTURE_ALPHA_VERSION",
    "MINIMUM_MARKOUT_OBSERVATIONS",
    "MULTI_LEVEL_DEPTH_IMBALANCE",
    "PREDECLARED_MARKOUT_SECONDS",
    "PREDECLARED_MICROSTRUCTURE_FEATURES",
    "SPREAD_STATE",
    "TOP_OF_BOOK_IMBALANCE",
    "TRADE_ATTRIBUTION_SECONDS",
    "TRADE_FLOW_IMBALANCE",
    "TRADE_TAPE_SUPPORTED",
    "BookFeatures",
    "BookTransition",
    "Markout",
    "MarkoutSummary",
    "MicrostructureAlphaError",
    "TradeFlow",
    "features_from_book",
    "markouts_for",
    "summarise_markouts",
    "trade_flow",
    "transitions",
]

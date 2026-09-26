"""Whether rare cross-venue dislocations are large enough to matter, and whether they are real.

MVP 0.2.7 established that the validated lead-lag signal is two orders of magnitude too small to
collect. This module tests the remaining falsifiable hypothesis: that although *average*
cross-venue dislocations sit far below trading costs, rare *tail* events might be large and
persistent enough to clear them.

**The one result that dominates every design decision here.** During development a 45-day
cross-venue screen produced dislocations that looked promising — ETH/MXN p50 of 24.3 bps and a p99
of 171.9 bps, comfortably above the 160.4 bps economic threshold at the tail. The measurement was
an artefact, and finding that out is the central piece of work in this milestone:

* Binance's ETH/MXN book quoted a **38.46 bps** spread while Bitso's eth_mxn quoted **0.63 bps**.
  The entire median dislocation sits *inside* the reference's own spread, so it is not a
  disagreement about price — it is a thin book's bid-ask being wide.
* One-minute return correlation between the two venues was **+0.068** for ETH and **+0.063** for
  SOL. Two venues quoting the same asset with the same quote currency should be far more
  correlated than that; the MXN books barely track each other.
* The dislocation's *sign* persisted for a median of 12 and a mean of 41 consecutive minutes on
  ETH, with a maximum of 740. A genuine arbitrage gap is closed by arbitrageurs; a standing offset
  of forty minutes is a stale or thinly-quoted reference, not an opportunity.
* There was no evidence of Bitso lagging: the correlation with the reference's *previous* bar
  (+0.052 on BTC) was no larger than the reference's correlation with Bitso's previous bar
  (+0.063).

So the module refuses to treat a wide reference book as a signal. `ReferenceQuality` measures each
reference's spread, and a measurement whose dislocation is not larger than the reference's own
spread is classified `REFERENCE_SPREAD_ARTIFACT` and excluded from alpha conclusions. This is the
mechanism that stops the milestone reproducing a number it cannot trade.

**Terminology is load-bearing.** A price difference is not arbitrage. With Bitso-only execution
there is no hedge, so an entry carries full directional risk and the exit depends on convergence
that may never happen. Everything here is called a *dislocation*, a *lag* or *convergence*.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ONE, ZERO, financial

from .execution_model import BPS

DISLOCATION_VERSION = "autofund.cross-venue-dislocation.v1"

# ---- dislocation kinds (spec section 7) ----
# Mid-versus-mid is a screen and never evidence. A hypothetical Bitso BUY pays Bitso's ask, and a
# hypothetical Bitso SELL receives Bitso's bid, so those are the only two executable quantities.
RAW_MID_DISLOCATION = "RAW_MID_DISLOCATION"
EXECUTABLE_BUY_DISLOCATION = "EXECUTABLE_BUY_DISLOCATION"
EXECUTABLE_SELL_DISLOCATION = "EXECUTABLE_SELL_DISLOCATION"
DISLOCATION_KINDS: tuple[str, ...] = (RAW_MID_DISLOCATION, EXECUTABLE_BUY_DISLOCATION,
                                      EXECUTABLE_SELL_DISLOCATION)

# Bitso's ask above the reference means a buyer would pay more than fair value; to profit the
# price must fall back. Bitso's bid below the reference means a seller receives less. Both are
# *disadvantageous* at entry and depend on convergence, which is why neither is called an edge.
BUY = "BUY"
SELL = "SELL"
DIRECTIONS: tuple[str, ...] = (BUY, SELL)

# ---- evidence quality (spec section 9) ----
EXECUTABLE_BOOK = "EXECUTABLE_BOOK"
TRADE_TAPE = "TRADE_TAPE"
CANDLE_SCREENING_ONLY = "CANDLE_SCREENING_ONLY"
EVIDENCE_CLASSES: tuple[str, ...] = (EXECUTABLE_BOOK, TRADE_TAPE, CANDLE_SCREENING_ONLY)

# Candle screening may propose candidate periods; it may not prove an executable dislocation.
# Encoding that as a property rather than a comment means the certificate cannot overstate it.
EVIDENCE_PROVES_EXECUTABILITY: dict[str, bool] = {
    EXECUTABLE_BOOK: True, TRADE_TAPE: True, CANDLE_SCREENING_ONLY: False,
}

# ---- validity of a single comparison ----
VALID = "VALID"
STALE_CROSS_VENUE_COMPARISON = "STALE_CROSS_VENUE_COMPARISON"
CROSSED_BOOK = "CROSSED_BOOK"
BROKEN_QUOTE = "BROKEN_QUOTE"
IMPLAUSIBLE_MOVE = "IMPLAUSIBLE_MOVE"
REFERENCE_SPREAD_ARTIFACT = "REFERENCE_SPREAD_ARTIFACT"
UNSYNCHRONIZED = "UNSYNCHRONIZED"

# ---- mechanism classification (spec section 17) ----
BITSO_LAG = "BITSO_LAG"
REFERENCE_MOVE_ONLY = "REFERENCE_MOVE_ONLY"
BITSO_LOCAL_DISLOCATION = "BITSO_LOCAL_DISLOCATION"
UNRESOLVED = "UNRESOLVED"
MECHANISMS: tuple[str, ...] = (BITSO_LAG, REFERENCE_MOVE_ONLY, BITSO_LOCAL_DISLOCATION,
                              UNRESOLVED)

# ---- terminal results (spec section 30) ----
CROSS_VENUE_ALPHA_CANDIDATE_FOUND = "CROSS_VENUE_ALPHA_CANDIDATE_FOUND"
CROSS_VENUE_SIGNAL_NOT_ECONOMIC = "CROSS_VENUE_SIGNAL_NOT_ECONOMIC"
INSUFFICIENT_CROSS_VENUE_EVIDENCE = "INSUFFICIENT_CROSS_VENUE_EVIDENCE"
ACTIVE_TRADING_THESIS_NOT_SUPPORTED = "ACTIVE_TRADING_THESIS_NOT_SUPPORTED"
BLOCKED = "BLOCKED"
TERMINAL_RESULTS: tuple[str, ...] = (CROSS_VENUE_ALPHA_CANDIDATE_FOUND,
                                    CROSS_VENUE_SIGNAL_NOT_ECONOMIC,
                                    INSUFFICIENT_CROSS_VENUE_EVIDENCE,
                                    ACTIVE_TRADING_THESIS_NOT_SUPPORTED, BLOCKED)

# ---- synchronization (spec section 8) ----
# Predeclared, before any measurement. One minute is the resolution of the candle data used for
# screening, and a comparison wider than that is not contemporaneous at all. Forward capture can
# tighten this because it sees real receive timestamps.
DEFAULT_MAX_SKEW_SECONDS = Decimal("60")

# A single-minute move larger than this is treated as a broken quote rather than a dislocation.
# Set far above any plausible one-minute range: the widest observed 99.9th percentile in this
# milestone's screen was under 400 bps, so 2000 bps is a data-integrity bound and not a
# parameter a result can hide behind.
DEFAULT_IMPLAUSIBLE_MOVE_BPS = Decimal("2000")

# ---- persistence (spec section 14) ----
# How many consecutive valid observations a dislocation must span to count as an episode. One
# observation is a tick; a tick cannot be captured, and grouping at one tick would turn every
# wick into an opportunity.
MINIMUM_EPISODE_OBSERVATIONS = 2

# ---- delay sensitivity (spec section 15) ----
# Predeclared. Candle data has one-minute resolution, which cannot resolve any of these, so every
# candle-based delay result is UNKNOWN rather than interpolated. Forward capture with real receive
# timestamps can populate them.
PREDECLARED_DELAYS_SECONDS: tuple[int, ...] = (1, 2, 5, 60)

# ---- evidence sufficiency (spec section 19) ----
# **The capture duration an economic conclusion requires, predeclared before measurement.**
#
# This is the guard that stops a short sample being reported as a market conclusion. The spec
# explicitly permits rare events — "1 credible event every several days or less" — so a capture
# covering a few minutes has essentially no probability of containing one. Concluding "no
# dislocation exists" from it would be concluding from an absence of opportunity to observe, which
# is the same category of error as an insensitive measurement reported as a negative result.
#
# 72 hours gives an event occurring once per day three independent chances to appear. It is
# deliberately a duration rather than an observation count: a few hundred observations taken in
# thirteen minutes and the same number taken over three days carry completely different evidential
# weight, and a count threshold could not tell them apart.
REQUIRED_CAPTURE_HOURS = 72

# The largest gap between consecutive observations before the capture counts as having a hole. A
# capture with long gaps covers less time than its span suggests, so covered time is measured with
# the holes removed rather than read off the first and last timestamps.
MAXIMUM_OBSERVATION_GAP_SECONDS = Decimal("120")


class DislocationError(ValueError):
    """A cross-venue measurement is malformed or inconsistent with itself."""


@dataclass(frozen=True, slots=True)
class Quote:
    """One venue's top of book, with its own timestamps and provenance (spec section 5).

    The exchange's event time and the local receive time are both carried, because their
    difference *is* the observation age and because a comparison between a fresh quote and a stale
    one is not a comparison. `mid` is derived rather than supplied so it cannot disagree with the
    bid and ask it came from.
    """

    venue: str
    symbol: str
    base_asset: str
    quote_asset: str
    event_time: datetime
    received_time: datetime
    bid: Decimal
    ask: Decimal
    quality: str = VALID
    provenance: str = "REAL_PUBLIC_TOP_OF_BOOK"

    def __post_init__(self) -> None:
        if not self.venue or not self.symbol:
            raise DislocationError("venue and symbol are required")
        if self.bid <= ZERO or self.ask <= ZERO:
            raise DislocationError("prices must be positive")
        if self.ask < self.bid:
            raise DislocationError(f"crossed book on {self.venue}: bid {self.bid} > ask "
                                   f"{self.ask} means the book is inverted")
        if self.event_time.tzinfo is None or self.received_time.tzinfo is None:
            raise DislocationError("timestamps must be timezone-aware")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / self.mid * BPS

    @property
    def observation_age_seconds(self) -> Decimal:
        return Decimal(str((self.received_time - self.event_time).total_seconds()))


@dataclass(frozen=True, slots=True)
class CrossVenueObservation:
    """A synchronized comparison of the incumbent against a reference on the same quote currency.

    **Same-quote is a hard requirement, not a preference.** Comparing a MXN price with a USD price
    requires an FX rate, and an FX rate brings its own spread, staleness and cost, so the measured
    difference would no longer be attributable to the venues. When a conversion genuinely is
    needed `fx_*` fields must be populated, the conversion is then explicitly *not* executable
    evidence, and the comparison is labelled as normalized rather than raw.
    """

    incumbent: Quote
    reference: Quote
    evidence: str
    max_skew_seconds: Decimal = DEFAULT_MAX_SKEW_SECONDS
    measured_skew_seconds: Decimal | None = None
    fx_source: str | None = None
    fx_timestamp: datetime | None = None
    fx_rate: Decimal | None = None
    fx_staleness_seconds: Decimal | None = None
    fx_spread_bps: Decimal | None = None
    implausible_move_bps: Decimal = DEFAULT_IMPLAUSIBLE_MOVE_BPS

    def __post_init__(self) -> None:
        if self.evidence not in EVIDENCE_CLASSES:
            raise DislocationError(f"unknown evidence class: {self.evidence}")
        if self.max_skew_seconds <= ZERO:
            raise DislocationError("max_skew_seconds must be positive")

    @property
    def same_quote(self) -> bool:
        return (self.incumbent.quote_asset.upper()
                == self.reference.quote_asset.upper())

    @property
    def base_matches(self) -> bool:
        return self.incumbent.base_asset.upper() == self.reference.base_asset.upper()

    @property
    def fx_normalised(self) -> bool:
        return self.fx_rate is not None

    @property
    def skew_seconds(self) -> Decimal:
        """Inter-venue skew: how far apart the two reads were.

        A measured elapsed time is authoritative when one was recorded, because two HTTP reads
        never return simultaneously and the receive timestamps are both stamped *after* their
        read completes. At polling granularity their difference collapses toward zero, which
        would make every comparison look more simultaneous than it was. Falling back to the
        timestamp difference is the honest degradation when no measurement exists, because an
        unknown skew is then at least not understated.
        """
        if self.measured_skew_seconds is not None:
            return abs(self.measured_skew_seconds)
        return Decimal(str(abs((self.incumbent.event_time
                                - self.reference.event_time).total_seconds())))

    @property
    def reference_price(self) -> Decimal:
        """The reference mid, converted into the incumbent's quote currency when required."""
        if self.reference.quote_asset.upper() == self.incumbent.quote_asset.upper():
            return self.reference.mid
        if self.fx_rate is None:
            raise DislocationError(
                "a cross-quote comparison needs an explicit FX rate; raw prices are not "
                "comparable and this will not guess one")
        return self.reference.mid * self.fx_rate

    @property
    def usable(self) -> str:
        """Whether this comparison may be used at all, before any dislocation claim is made.

        This is the general data-integrity question and it deliberately does NOT include the
        reference-spread attribution test. The two questions are different, and conflating them
        was a live defect: an observation whose gap has narrowed to inside the reference's spread
        is *excluded* as a dislocation claim, but it is the strongest possible evidence that
        convergence happened. If the attribution guard were part of general usability, the forward
        window could never contain a converged observation and convergence could never be
        measured — the test would be unable to detect the thing it exists to detect.
        """
        if not self.base_matches:
            return BROKEN_QUOTE
        if not self.same_quote and not self.fx_normalised:
            return BROKEN_QUOTE
        if self.incumbent.quality != VALID or self.reference.quality != VALID:
            return BROKEN_QUOTE
        if self.incumbent.bid > self.incumbent.ask or self.reference.bid > self.reference.ask:
            return CROSSED_BOOK
        if self.skew_seconds > self.max_skew_seconds:
            return STALE_CROSS_VENUE_COMPARISON
        if abs(self.raw_mid_dislocation_bps) > self.implausible_move_bps:
            return IMPLAUSIBLE_MOVE
        return VALID

    @property
    def dislocation_attributable(self) -> bool:
        """Whether a non-zero gap here can be attributed to the venues rather than to book width.

        The reference's own spread is the floor: a difference smaller than it is explained by
        where the reference's bid and ask happen to sit. This is the guard that stops a thin book
        being read as a signal, and it applies to *claiming a dislocation*, not to using the
        observation as evidence about one.
        """
        return abs(self.raw_mid_dislocation_bps) > self.reference_spread_bps

    @property
    def validity(self) -> str:
        """The single value a caller uses to decide whether to act on this comparison.

        Combines general usability with the attribution test, ordered so the most disqualifying
        condition is reported first. `usable` is exposed separately for the forward window, where
        an unattributable gap is a convergence observation rather than a rejected sample.
        """
        status = self.usable
        if status != VALID:
            return status
        if not self.dislocation_attributable:
            return REFERENCE_SPREAD_ARTIFACT
        return VALID

    @property
    def reference_spread_bps(self) -> Decimal:
        """The reference's own bid-ask spread, the floor for an attributable difference.

        This is the guard that stops a thin book being read as a signal. If the two venues
        disagree by less than the reference's own spread, the difference is explained by where
        the reference's bid and ask happen to sit and says nothing about the incumbent's price.
        """
        if self.fx_normalised and self.fx_spread_bps is not None:
            return self.reference.spread_bps + self.fx_spread_bps
        return self.reference.spread_bps

    @property
    def raw_mid_dislocation_bps(self) -> Decimal:
        """Mid against the reference, signed positive when the incumbent is the more expensive.

        Symmetric and unsigned in meaning, so it is a screen and never a trade signal. Reported
        for the distribution and for the reference-spread attribution test, which is a symmetric
        question about whether the two venues disagree at all.
        """
        return (self.incumbent.mid - self.reference_price) / self.reference_price * BPS

    @property
    def executable_buy_headroom_bps(self) -> Decimal:
        """How much cheaper Bitso is to BUY than the reference, positive meaning cheap.

        **The sign is the whole milestone.** A cross-venue lag means the reference market moved
        first and the incumbent has not yet caught up, so the exploitable state is the incumbent
        being *behind* the reference — buying on Bitso below where the reference says the price is,
        then holding while Bitso converges upward. A positive value here is therefore the
        favourable direction for a long entry.

        Bitso's ask is used because that is the price a hypothetical BUY would actually pay, so
        Bitso's spread is already inside this number and must not be charged again in the economic
        threshold.

        An earlier version of this property had the sign inverted, which turned the measurement
        into a bet on the incumbent being overpriced and reverting downward. That is a
        mean-reversion hypothesis rather than cross-venue lag, and it would have been reported
        under this milestone's name. The test suite pins the direction explicitly.
        """
        return (self.reference_price - self.incumbent.ask) / self.reference_price * BPS

    @property
    def executable_sell_headroom_bps(self) -> Decimal:
        """How much richer Bitso is to SELL into than the reference, positive meaning rich.

        The mirror of the buy direction: the incumbent sitting *above* the reference is the
        favourable state for an exit, which is what a long position would be waiting for.
        """
        return (self.incumbent.bid - self.reference_price) / self.reference_price * BPS

    # Retained as explicit aliases because the certificate and the existing reports refer to
    # "dislocation", and a reader should be able to see that the dislocation and the headroom are
    # the same measurement with the sign stated.
    @property
    def executable_buy_dislocation_bps(self) -> Decimal:
        """The buy dislocation: negative when the incumbent is cheap, i.e. the favourable state."""
        return -self.executable_buy_headroom_bps

    @property
    def executable_sell_dislocation_bps(self) -> Decimal:
        """The sell dislocation: negative when the incumbent is rich, i.e. the favourable state."""
        return -self.executable_sell_headroom_bps

    def dislocation_bps(self, kind: str) -> Decimal:
        if kind == RAW_MID_DISLOCATION:
            return self.raw_mid_dislocation_bps
        if kind == EXECUTABLE_BUY_DISLOCATION:
            return self.executable_buy_dislocation_bps
        if kind == EXECUTABLE_SELL_DISLOCATION:
            return self.executable_sell_dislocation_bps
        raise DislocationError(f"unknown dislocation kind: {kind}")

    def magnitude_bps(self, kind: str, direction: str) -> Decimal:
        """The *favourable* gap for a hypothetical trade, positive meaning exploitable.

        Positive means the incumbent is behind the reference in the direction that would pay: cheap
        to buy, or rich to sell. Negative means the incumbent is already ahead, which is not an
        opportunity and must not be reported as one.

        The sign convention is enforced by construction rather than by convention: each branch
        returns the headroom property whose definition states its direction, so a future edit
        cannot flip a sign without changing which property is named.
        """
        if kind == RAW_MID_DISLOCATION:
            raise DislocationError("mid dislocation has no trade direction")
        if direction == BUY:
            if kind != EXECUTABLE_BUY_DISLOCATION:
                raise DislocationError(f"{direction} must be measured with the buy kind")
            return self.executable_buy_headroom_bps
        if direction == SELL:
            if kind != EXECUTABLE_SELL_DISLOCATION:
                raise DislocationError(f"{direction} must be measured with the sell kind")
            return self.executable_sell_headroom_bps
        raise DislocationError(f"unknown direction: {direction}")

    def public(self) -> dict[str, Any]:
        return {
            "incumbent_venue": self.incumbent.venue,
            "incumbent_symbol": self.incumbent.symbol,
            "reference_venue": self.reference.venue,
            "reference_symbol": self.reference.symbol,
            "base_asset": self.incumbent.base_asset,
            "quote_asset": self.incumbent.quote_asset,
            "reference_quote_asset": self.reference.quote_asset,
            "same_quote": self.same_quote,
            "fx_normalised": self.fx_normalised,
            "fx_source": self.fx_source,
            "incumbent_event_time": self.incumbent.event_time.isoformat(),
            "reference_event_time": self.reference.event_time.isoformat(),
            "skew_seconds": str(self.skew_seconds),
            "incumbent_bid": str(self.incumbent.bid),
            "incumbent_ask": str(self.incumbent.ask),
            "reference_bid": str(self.reference.bid),
            "reference_ask": str(self.reference.ask),
            "incumbent_spread_bps": str(self.incumbent.spread_bps),
            "reference_spread_bps": str(self.reference.spread_bps),
            "raw_mid_dislocation_bps": str(self.raw_mid_dislocation_bps),
            "executable_buy_dislocation_bps": str(self.executable_buy_dislocation_bps),
            "executable_sell_dislocation_bps": str(self.executable_sell_dislocation_bps),
            "evidence": self.evidence,
            "validity": self.validity,
            "provenance": (f"{self.incumbent.provenance};{self.reference.provenance}"),
        }


@dataclass(frozen=True, slots=True)
class CaptureCoverage:
    """How much time a capture actually covers, and whether that is enough to conclude anything."""

    observations: int
    span_seconds: Decimal | None
    largest_gap_seconds: Decimal | None
    covered_seconds: Decimal | None
    required_hours: int = REQUIRED_CAPTURE_HOURS

    @property
    def span_hours(self) -> Decimal | None:
        if self.span_seconds is None:
            return None
        return self.span_seconds / Decimal("3600")

    @property
    def covered_hours(self) -> Decimal | None:
        if self.covered_seconds is None:
            return None
        return self.covered_seconds / Decimal("3600")

    @property
    def continuous(self) -> bool | None:
        if self.largest_gap_seconds is None:
            return None
        return self.largest_gap_seconds <= MAXIMUM_OBSERVATION_GAP_SECONDS

    @property
    def sufficient(self) -> bool:
        """Whether an economic conclusion about a rare event is licensed by this window."""
        if self.covered_seconds is None:
            return False
        return self.covered_seconds >= Decimal(self.required_hours) * Decimal("3600")

    def public(self) -> dict[str, Any]:
        return {
            "observations": self.observations,
            "span_seconds": None if self.span_seconds is None else str(self.span_seconds),
            "span_hours": None if self.span_hours is None else str(self.span_hours),
            "largest_gap_seconds": (None if self.largest_gap_seconds is None
                                    else str(self.largest_gap_seconds)),
            "covered_hours": None if self.covered_hours is None else str(self.covered_hours),
            "required_hours": self.required_hours,
            "continuous": self.continuous,
            "sufficient": self.sufficient,
            "maximum_observation_gap_seconds": str(MAXIMUM_OBSERVATION_GAP_SECONDS),
        }


def capture_coverage(*, observations: tuple[CrossVenueObservation, ...]) -> CaptureCoverage:
    """Measure covered time rather than merely counting samples.

    A capture that ran for three days with a two-day outage covers one day of evidence, so the
    span alone would overstate it and the largest gap is subtracted.
    """
    if not observations:
        return CaptureCoverage(observations=0, span_seconds=None, largest_gap_seconds=None,
                               covered_seconds=None)
    moments = sorted(item.incumbent.event_time for item in observations)
    span = Decimal(str((moments[-1] - moments[0]).total_seconds()))
    gaps = [Decimal(str((moments[index + 1] - moments[index]).total_seconds()))
            for index in range(len(moments) - 1)]
    largest = max(gaps) if gaps else Decimal("0")
    holes = [gap for gap in gaps if gap > MAXIMUM_OBSERVATION_GAP_SECONDS]
    return CaptureCoverage(observations=len(observations), span_seconds=span,
                           largest_gap_seconds=largest,
                           covered_seconds=span - sum(holes, Decimal("0")))

# ---- safety constants, restated so the module is self-contained ----
MAX_SINGLE_ORDER_MXN = Decimal("11")


@dataclass(frozen=True, slots=True)
class ReferenceQuality:
    """Whether a reference venue's book is tight enough for its prices to mean anything.

    Exists because the milestone's own development screen produced a false positive without it.
    A reference quoting a 38 bps spread against an incumbent quoting 0.63 bps cannot support a
    24 bps dislocation claim: the number is a property of the reference's book depth, not of any
    disagreement between the venues.
    """

    venue: str
    symbol: str
    samples: int
    median_spread_bps: Decimal
    incumbent_median_spread_bps: Decimal

    @property
    def ratio(self) -> Decimal | None:
        if self.incumbent_median_spread_bps <= ZERO:
            return None
        return self.median_spread_bps / self.incumbent_median_spread_bps

    @property
    def usable(self) -> bool:
        """Usable when the reference is not dramatically wider than the incumbent.

        Three times is generous on purpose: it admits a reference somewhat thinner than Bitso while
        excluding one whose spread dwarfs the incumbent's, which is the case that produced the
        artefact. The threshold is reported so a reader can disagree with it.
        """
        ratio = self.ratio
        if ratio is None:
            return False
        return ratio <= Decimal("3")

    def public(self) -> dict[str, Any]:
        return {"venue": self.venue, "symbol": self.symbol, "samples": self.samples,
                "median_spread_bps": str(self.median_spread_bps),
                "incumbent_median_spread_bps": str(self.incumbent_median_spread_bps),
                "spread_ratio": None if self.ratio is None else str(self.ratio),
                "usable": self.usable}


@financial
def percentile(*, values: tuple[Decimal, ...], fraction: Decimal) -> Decimal | None:
    """The value at a fraction of the sorted sample, by nearest rank.

    Nearest rank rather than interpolated, because interpolating between two observed
    dislocations would report a magnitude that was never measured. With tails that is the
    difference between a real observation and a plausible-looking invention.
    """
    if not values:
        return None
    if not (ZERO < fraction <= ONE):
        raise DislocationError(f"fraction must be in (0, 1], got {fraction}")
    ordered = sorted(values)
    index = int(Decimal(len(ordered) - 1) * fraction)
    return ordered[index]


# The tail percentiles the spec asks for, predeclared so a promising one cannot be selected
# afterwards.
TAIL_FRACTIONS: tuple[tuple[str, Decimal], ...] = (
    ("p50", Decimal("0.50")), ("p90", Decimal("0.90")), ("p95", Decimal("0.95")),
    ("p99", Decimal("0.99")), ("p99.5", Decimal("0.995")), ("p99.9", Decimal("0.999")),
)


@dataclass(frozen=True, slots=True)
class TailDistribution:
    """The distribution of executable dislocation for one asset, reference and direction."""

    asset: str
    reference_venue: str
    kind: str
    direction: str
    evidence: str
    observations: int
    valid_observations: int
    excluded: dict[str, int]
    values_bps: tuple[Decimal, ...]

    @property
    def percentiles(self) -> dict[str, Decimal | None]:
        return {label: percentile(values=self.values_bps, fraction=fraction)
                for label, fraction in TAIL_FRACTIONS}

    @property
    def maximum_credible_bps(self) -> Decimal | None:
        """The largest value that survived validity checks.

        Reported as "credible" rather than "maximum" because every value here has already passed
        staleness, crossed-book, plausibility and reference-spread screening. A raw maximum without
        those checks is the single most misleading number this milestone could publish.
        """
        return max(self.values_bps) if self.values_bps else None

    def exceedance(self, thresholds: tuple[Decimal, ...]) -> dict[str, int]:
        return {str(threshold): sum(1 for value in self.values_bps if value > threshold)
                for threshold in thresholds}

    def public(self) -> dict[str, Any]:
        return {"asset": self.asset, "reference_venue": self.reference_venue,
                "kind": self.kind, "direction": self.direction, "evidence": self.evidence,
                "observations": self.observations, "valid_observations": self.valid_observations,
                "excluded": dict(self.excluded),
                "percentiles": {k: (None if v is None else str(v))
                                for k, v in self.percentiles.items()},
                "maximum_credible_bps": (None if self.maximum_credible_bps is None
                                         else str(self.maximum_credible_bps)),
                "proves_executability": EVIDENCE_PROVES_EXECUTABILITY[self.evidence]}


def build_distribution(*, observations: tuple[CrossVenueObservation, ...], asset: str,
                       kind: str, direction: str
                       ) -> TailDistribution:
    """Collect every valid observation's magnitude for one asset/reference/direction."""
    if kind == RAW_MID_DISLOCATION:
        raise DislocationError("a distribution needs an executable dislocation kind")
    if not observations:
        raise DislocationError("a distribution needs at least one observation")
    excluded: dict[str, int] = {}
    values: list[Decimal] = []
    for observation in observations:
        validity = observation.validity
        if validity != VALID:
            excluded[validity] = excluded.get(validity, 0) + 1
            continue
        values.append(observation.magnitude_bps(kind, direction))
    return TailDistribution(
        asset=asset, reference_venue=observations[0].reference.venue, kind=kind,
        direction=direction, evidence=observations[0].evidence,
        observations=len(observations), valid_observations=len(values),
        excluded=excluded, values_bps=tuple(values))


@dataclass(frozen=True, slots=True)
class DislocationEpisode:
    """A run of consecutive valid observations above a threshold (spec section 14).

    Durations are derived from the observation timestamps rather than a count of samples, so a
    one-minute candle series and a one-second capture stream produce comparable numbers in
    seconds. Counting samples would make persistence look resolution-dependent.
    """

    asset: str
    reference_venue: str
    direction: str
    kind: str
    start: datetime
    end: datetime
    observations: int
    start_bps: Decimal
    peak_bps: Decimal
    peak_at: datetime
    threshold_bps: Decimal

    @property
    def duration_seconds(self) -> Decimal:
        return Decimal(str((self.end - self.start).total_seconds()))

    @property
    def time_to_peak_seconds(self) -> Decimal:
        return Decimal(str((self.peak_at - self.start).total_seconds()))

    @property
    def time_from_peak_seconds(self) -> Decimal:
        return Decimal(str((self.end - self.peak_at).total_seconds()))

    def public(self) -> dict[str, Any]:
        return {"asset": self.asset, "reference_venue": self.reference_venue,
                "direction": self.direction, "kind": self.kind,
                "start": self.start.isoformat(), "end": self.end.isoformat(),
                "observations": self.observations, "start_bps": str(self.start_bps),
                "peak_bps": str(self.peak_bps),
                "peak_at": self.peak_at.isoformat(),
                "threshold_bps": str(self.threshold_bps),
                "duration_seconds": str(self.duration_seconds),
                "time_to_peak_seconds": str(self.time_to_peak_seconds),
                "time_from_peak_seconds": str(self.time_from_peak_seconds)}


def group_episodes(*, observations: tuple[CrossVenueObservation, ...], kind: str, direction: str,
                   threshold_bps: Decimal,
                   minimum_observations: int = MINIMUM_EPISODE_OBSERVATIONS
                   ) -> tuple[DislocationEpisode, ...]:
    """Group consecutive threshold crossings into episodes (spec sections 13 and 14).

    Consecutive means the *next source observation* is also above the threshold: an observation
    excluded for staleness or a bad quote breaks the run rather than being skipped over. Treating
    an excluded sample as "still above" would let a gap in the data masquerade as persistence.
    """
    if threshold_bps < ZERO:
        raise DislocationError("threshold cannot be negative")
    if minimum_observations < 1:
        raise DislocationError("minimum_observations must be at least 1")
    episodes: list[DislocationEpisode] = []
    run: list[tuple[datetime, Decimal]] = []

    def flush() -> None:
        if len(run) >= minimum_observations:
            peak_at, peak = max(run, key=lambda item: item[1])
            episodes.append(DislocationEpisode(
                asset=observations[0].incumbent.base_asset,
                reference_venue=observations[0].reference.venue, direction=direction,
                kind=kind, start=run[0][0], end=run[-1][0], observations=len(run),
                start_bps=run[0][1], peak_bps=peak, peak_at=peak_at,
                threshold_bps=threshold_bps))
        run.clear()

    for observation in observations:
        if observation.validity != VALID:
            flush()
            continue
        magnitude = observation.magnitude_bps(kind, direction)
        if magnitude > threshold_bps:
            run.append((observation.incumbent.event_time, magnitude))
        else:
            flush()
    flush()
    return tuple(episodes)


@dataclass(frozen=True, slots=True)
class DelayResult:
    """Whether an episode still clears the threshold after a hypothetical execution delay."""

    delay_seconds: int
    supported: bool
    episodes_still_above: int | None
    best_remaining_headroom_bps: Decimal | None
    reason: str

    def public(self) -> dict[str, Any]:
        return {"delay_seconds": self.delay_seconds, "supported": self.supported,
                "episodes_still_above": self.episodes_still_above,
                "best_remaining_headroom_bps": (None if self.best_remaining_headroom_bps is None
                                                else str(self.best_remaining_headroom_bps)),
                "reason": self.reason}


def delay_sensitivity(*, observations: tuple[CrossVenueObservation, ...], kind: str,
                      direction: str, threshold_bps: Decimal,
                      delays: tuple[int, ...] = PREDECLARED_DELAYS_SECONDS,
                      ) -> tuple[DelayResult, ...]:
    """What a delay costs an episode's headroom, at the data's own resolution.

    The honest answer for candle data is UNKNOWN rather than an interpolated number. One-minute
    candles cannot distinguish a 250 ms delay from a 500 ms one, and reporting either would be
    precision the data does not have. A delay is only evaluated when the source resolution can
    actually resolve it.
    """
    if not observations:
        raise DislocationError("delay sensitivity needs observations")
    resolution = _source_resolution_seconds(observations=observations)
    results: list[DelayResult] = []
    for delay in delays:
        if delay < resolution:
            results.append(DelayResult(
                delay_seconds=delay, supported=False, episodes_still_above=None,
                best_remaining_headroom_bps=None,
                reason=(f"source resolution is {resolution}s, which cannot resolve a {delay}s "
                        f"delay; the result would be interpolated precision")))
            continue
        episodes = group_episodes(observations=observations, kind=kind, direction=direction,
                                  threshold_bps=threshold_bps)
        still_above = 0
        best: Decimal | None = None
        for episode in episodes:
            shifted = _value_after_delay(observations=observations, episode=episode, kind=kind,
                                         direction=direction, delay_seconds=delay)
            if shifted is not None and shifted > threshold_bps:
                still_above += 1
                best = shifted if best is None else max(best, shifted)
        results.append(DelayResult(
            delay_seconds=delay, supported=True, episodes_still_above=still_above,
            best_remaining_headroom_bps=best,
            reason=(f"evaluated at {resolution}s resolution; headroom is the dislocation "
                    f"observed {delay}s after the episode began")))
    return tuple(results)


def _source_resolution_seconds(*, observations: tuple[CrossVenueObservation, ...]) -> int:
    """The smallest gap between consecutive observations, as an integer number of seconds."""
    if len(observations) < 2:
        return 0
    times = sorted(item.incumbent.event_time for item in observations)
    gaps = [(times[index + 1] - times[index]).total_seconds()
            for index in range(len(times) - 1)]
    positive = [gap for gap in gaps if gap > 0]
    if not positive:
        return 0
    return int(min(positive))


def _value_after_delay(*, observations: tuple[CrossVenueObservation, ...],
                       episode: DislocationEpisode, kind: str, direction: str,
                       delay_seconds: int) -> Decimal | None:
    target = episode.start.timestamp() + delay_seconds
    for observation in observations:
        if observation.incumbent.event_time.timestamp() < target:
            continue
        if observation.usable != VALID:
            return None
        return observation.magnitude_bps(kind, direction)
    return None


@dataclass(frozen=True, slots=True)
class ConvergenceOutcome:
    """What happened after detection, measured strictly forward (spec sections 16 and 27).

    Detection uses only data at or before the episode's start. Everything here is measured from
    observations strictly after it, because a convergence measured from the same bar that
    detected the dislocation would include the dislocation itself.
    """

    episode: DislocationEpisode
    horizon_seconds: Decimal
    convergence_bps: Decimal | None
    maximum_favourable_excursion_bps: Decimal | None
    maximum_adverse_excursion_bps: Decimal | None
    converged: bool
    time_to_partial_convergence_seconds: Decimal | None
    time_to_maximum_convergence_seconds: Decimal | None
    observations_after: int

    def public(self) -> dict[str, Any]:
        return {
            "episode_start": self.episode.start.isoformat(),
            "asset": self.episode.asset, "reference_venue": self.episode.reference_venue,
            "direction": self.episode.direction,
            "threshold_bps": str(self.episode.threshold_bps),
            "peak_bps": str(self.episode.peak_bps),
            "horizon_seconds": str(self.horizon_seconds),
            "observations_after": self.observations_after,
            "convergence_bps": (None if self.convergence_bps is None
                                else str(self.convergence_bps)),
            "mfe_bps": (None if self.maximum_favourable_excursion_bps is None
                        else str(self.maximum_favourable_excursion_bps)),
            "mae_bps": (None if self.maximum_adverse_excursion_bps is None
                        else str(self.maximum_adverse_excursion_bps)),
            "converged": self.converged,
            "time_to_partial_convergence_seconds": (
                None if self.time_to_partial_convergence_seconds is None
                else str(self.time_to_partial_convergence_seconds)),
            "time_to_maximum_convergence_seconds": (
                None if self.time_to_maximum_convergence_seconds is None
                else str(self.time_to_maximum_convergence_seconds)),
            "future_used_in_detection": False,
        }


def convergence_after(*, observations: tuple[CrossVenueObservation, ...],
                      episode: DislocationEpisode, kind: str, direction: str,
                      horizon_seconds: Decimal,
                      convergence_target_bps: Decimal = ZERO) -> ConvergenceOutcome:
    """Measure convergence strictly after detection.

    **Entry is the dislocation at detection, not at the peak.** A trade would be placed when the
    episode was first detected, so excursion must be measured from that value. Measuring from the
    peak would make adverse excursion identically zero by construction — any later observation is
    at or below the maximum — and would hide exactly the loss a position takes while waiting for
    convergence that may never arrive.

    "Favourable" means the dislocation *narrowed*, because narrowing is what a trade relies on.
    Failure to converge is a first-class outcome rather than an exception, because for this
    hypothesis it is the expected one.
    """
    if horizon_seconds <= ZERO:
        raise DislocationError("horizon must be positive")
    start = episode.start.timestamp()
    end = start + float(horizon_seconds)
    forward = [item for item in observations
               if item.incumbent.event_time.timestamp() > start
               and item.incumbent.event_time.timestamp() <= end]
    if not forward:
        return ConvergenceOutcome(
            episode=episode, horizon_seconds=horizon_seconds, convergence_bps=None,
            maximum_favourable_excursion_bps=None, maximum_adverse_excursion_bps=None,
            converged=False, time_to_partial_convergence_seconds=None,
            time_to_maximum_convergence_seconds=None, observations_after=0)

    entry = episode.start_bps
    narrowing: list[tuple[float, Decimal]] = []
    widening: list[Decimal] = []
    for observation in forward:
        # General usability, not the attribution guard. A gap that has narrowed inside the
        # reference's spread is the convergence being measured, and rejecting it here would make
        # convergence undetectable by construction.
        if observation.usable != VALID:
            continue
        remaining = observation.magnitude_bps(kind, direction)
        narrowing.append((observation.incumbent.event_time.timestamp(), entry - remaining))
        widening.append(remaining - entry)

    if not narrowing:
        return ConvergenceOutcome(
            episode=episode, horizon_seconds=horizon_seconds, convergence_bps=None,
            maximum_favourable_excursion_bps=None, maximum_adverse_excursion_bps=None,
            converged=False, time_to_partial_convergence_seconds=None,
            time_to_maximum_convergence_seconds=None, observations_after=0)

    best_time, best = max(narrowing, key=lambda item: item[1])
    final = narrowing[-1][1]
    reached = [time for time, value in narrowing if value > convergence_target_bps]
    # Strictly greater, not greater-or-equal. A gap that stays exactly where it was has narrowed
    # by zero, and counting that as convergence would let a permanently wide dislocation satisfy
    # the hypothesis that it closes. This matters most for this milestone's own shape: the
    # observed dislocations are standing offsets that barely move, so a permissive test would
    # report convergence everywhere.
    return ConvergenceOutcome(
        episode=episode, horizon_seconds=horizon_seconds, convergence_bps=final,
        maximum_favourable_excursion_bps=best,
        maximum_adverse_excursion_bps=max(widening) if widening else None,
        converged=final > convergence_target_bps,
        time_to_partial_convergence_seconds=(Decimal(str(reached[0] - start))
                                             if reached else None),
        time_to_maximum_convergence_seconds=Decimal(str(best_time - start)),
        observations_after=len(narrowing))


@dataclass(frozen=True, slots=True)
class CandidateManifest:
    """A frozen cross-venue dislocation candidate (spec section 22).

    Frozen before the holdout is read, and carrying the fields a validation would need so the
    definition cannot be adjusted once results are seen. `holdout_touched` must be False, and the
    economic threshold is recorded with its derivation rather than as a number, so a later change
    to the policy is visible as a change in provenance.
    """

    asset: str
    reference_venue: str
    kind: str
    direction: str
    minimum_dislocation_bps: Decimal
    maximum_staleness_seconds: Decimal
    delay_assumption_seconds: int
    economic_threshold_bps: Decimal
    threshold_source: str
    development_start: datetime
    development_end: datetime
    development_episodes: int
    development_median_duration_seconds: Decimal | None
    development_median_convergence_bps: Decimal | None
    evidence: str
    holdout_touched: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.holdout_touched:
            raise DislocationError("a candidate may not be frozen after the holdout was read")
        if self.kind == RAW_MID_DISLOCATION:
            raise DislocationError("a candidate must use an executable dislocation kind")
        if self.direction not in DIRECTIONS:
            raise DislocationError(f"unknown direction: {self.direction}")
        if self.minimum_dislocation_bps < ZERO:
            raise DislocationError("minimum dislocation cannot be negative")
        if self.development_end <= self.development_start:
            raise DislocationError("development window must be forward in time")

    @property
    def fingerprint(self) -> str:
        """A stable identity for the frozen definition, excluding timestamps and counts.

        Counts are excluded deliberately: they describe what development happened to contain, not
        what the candidate *is*, and including them would make the fingerprint change whenever the
        same rule was frozen against a different sample.
        """
        from hashlib import sha256

        payload = "|".join((
            self.asset, self.reference_venue, self.kind, self.direction,
            str(self.minimum_dislocation_bps), str(self.maximum_staleness_seconds),
            str(self.delay_assumption_seconds), str(self.economic_threshold_bps),
            self.threshold_source, self.evidence))
        return sha256(payload.encode("utf-8")).hexdigest()

    def public(self) -> dict[str, Any]:
        return {"asset": self.asset, "reference_venue": self.reference_venue,
                "kind": self.kind, "direction": self.direction,
                "minimum_dislocation_bps": str(self.minimum_dislocation_bps),
                "maximum_staleness_seconds": str(self.maximum_staleness_seconds),
                "delay_assumption_seconds": self.delay_assumption_seconds,
                "economic_threshold_bps": str(self.economic_threshold_bps),
                "threshold_source": self.threshold_source,
                "development_start": self.development_start.isoformat(),
                "development_end": self.development_end.isoformat(),
                "development_episodes": self.development_episodes,
                "development_median_duration_seconds": (
                    None if self.development_median_duration_seconds is None
                    else str(self.development_median_duration_seconds)),
                "development_median_convergence_bps": (
                    None if self.development_median_convergence_bps is None
                    else str(self.development_median_convergence_bps)),
                "evidence": self.evidence,
                "holdout_touched": self.holdout_touched,
                "fingerprint": self.fingerprint,
                "is_arbitrage": False,
                "trading_policy_defined": False,
                "created_at": self.created_at.isoformat()}


def validate_candidate(*, candidate: CandidateManifest,
                       holdout: tuple[CrossVenueObservation, ...],
                       convergence_horizon_seconds: Decimal
                       ) -> dict[str, Any]:
    """Test a frozen candidate on data it has never seen (spec section 23).

    The candidate's own minimum dislocation, staleness ceiling and delay assumption are used
    unchanged. There is no parameter here that could loosen them, which is the mechanism that
    makes "do not retune on validation" enforceable rather than a promise: a caller who wants a
    different threshold has to build a different candidate, and that candidate would have a
    different fingerprint and would be visible as a second attempt.
    """
    if candidate.holdout_touched:
        raise DislocationError("the holdout for this candidate was already read")
    usable = tuple(item for item in holdout
                   if item.validity == VALID
                   and item.skew_seconds <= candidate.maximum_staleness_seconds)
    if not usable:
        # Same reasoning as the empty episode statistics: the shape is identical whether or not
        # the holdout had usable data, so a caller reads one contract rather than two.
        return {"ran": False, "reason": "NO_VALID_HOLDOUT_OBSERVATIONS",
                "observations": len(holdout), "usable": 0,
                "episodes": 0, "episodes_converged": 0,
                "median_duration_seconds": None, "median_convergence_bps": None,
                "convergence_rate": None,
                "reproduced": None, "threshold_changed": False,
                "minimum_dislocation_used": str(candidate.minimum_dislocation_bps),
                "delay_assumption_used": candidate.delay_assumption_seconds,
                "holdout_was_untouched_before_freeze": True,
                "candidate_fingerprint": candidate.fingerprint,
                "episode_detail": []}

    episodes = group_episodes(observations=usable, kind=candidate.kind,
                              direction=candidate.direction,
                              threshold_bps=candidate.minimum_dislocation_bps)
    outcomes = [convergence_after(observations=usable, episode=episode, kind=candidate.kind,
                                  direction=candidate.direction,
                                  horizon_seconds=convergence_horizon_seconds)
                for episode in episodes]
    converged = [item for item in outcomes if item.converged]
    # Durations come from the episodes themselves; `outcomes` pairs each with its forward
    # measurement, and reaching through it for the episode would work only by accident.
    durations = [episode.duration_seconds for episode in episodes]
    return {
        "ran": True,
        "observations": len(holdout),
        "usable": len(usable),
        "episodes": len(episodes),
        "episodes_converged": len(converged),
        "median_duration_seconds": str(median(values=tuple(durations)))
        if durations else None,
        "median_convergence_bps": str(median(values=tuple(
            item.convergence_bps for item in outcomes
            if item.convergence_bps is not None))),
        "convergence_rate": (str(Decimal(len(converged)) / Decimal(len(episodes)))
                             if episodes else None),
        "reproduced": bool(episodes) and bool(converged),
        "threshold_changed": False,
        "minimum_dislocation_used": str(candidate.minimum_dislocation_bps),
        "delay_assumption_used": candidate.delay_assumption_seconds,
        "holdout_was_untouched_before_freeze": True,
        "candidate_fingerprint": candidate.fingerprint,
        "episode_detail": [item.public() for item in outcomes],
    }


@financial
def median(*, values: tuple[Decimal, ...]) -> Decimal | None:
    """The middle value, or the mean of the two middle values for an even count."""
    if not values:
        return None
    ordered = sorted(values)
    size = len(ordered)
    if size % 2 == 1:
        return ordered[size // 2]
    return (ordered[size // 2 - 1] + ordered[size // 2]) / 2


def classify_mechanism(*, incumbent_before: Decimal, incumbent_after: Decimal,
                       reference_before: Decimal, reference_after: Decimal,
                       move_tolerance_bps: Decimal = Decimal("1")
                       ) -> str:
    """Attribute an episode to a mechanism where the evidence permits (spec section 17).

    The distinction that matters for a convergence trade is whether the *incumbent* moves, because
    that is where any position would be closed. If the reference moves and the incumbent follows,
    there is a lag to trade. If the incumbent moves and the reference follows, the incumbent is
    the leader and a reference-based signal has nothing to offer. If neither moves, the gap is
    local to the incumbent's book. Anything ambiguous stays UNRESOLVED, because forcing a
    classification is how a mechanism gets invented that the data does not show.
    """
    if move_tolerance_bps < ZERO:
        raise DislocationError("move_tolerance_bps cannot be negative")
    incumbent_moved = abs(incumbent_after - incumbent_before) > move_tolerance_bps
    reference_moved = abs(reference_after - reference_before) > move_tolerance_bps
    if incumbent_moved and not reference_moved:
        return BITSO_LAG
    if reference_moved and not incumbent_moved:
        return REFERENCE_MOVE_ONLY
    if incumbent_moved and reference_moved:
        # Both moved. Which one led is not determinable from two points, so this is unresolved
        # rather than assigned to whichever ordering happens to look favourable.
        return UNRESOLVED
    return BITSO_LOCAL_DISLOCATION


def episode_statistics(*, episodes: tuple[DislocationEpisode, ...]) -> dict[str, Any]:
    """Persistence summary for a set of episodes (spec section 14)."""
    if not episodes:
        # Every key the populated branch provides is present here too, set to None rather than
        # omitted. A caller must not have to know which branch ran to read the result, and a
        # missing key is worse than an explicit null because it fails at the call site instead of
        # reporting that nothing was observed.
        return {"episodes": 0, "median_duration_seconds": None,
                "p25_duration_seconds": None, "p75_duration_seconds": None,
                "p90_duration_seconds": None, "shortest_duration_seconds": None,
                "longest_duration_seconds": None, "total_seconds_above_threshold": None,
                "dominant_episode_share": None, "clustered": None,
                "single_anomaly_dominates": None}
    durations = tuple(item.duration_seconds for item in episodes)
    ordered = tuple(sorted(durations))
    total = sum(durations, ZERO)
    longest = ordered[-1]
    # One episode dominating the evidence is an anomaly, not an alpha source (spec section 20).
    share = (longest / total) if total > ZERO else ZERO
    return {
        "episodes": len(episodes),
        "median_duration_seconds": str(median(values=durations)),
        "p25_duration_seconds": str(percentile(values=ordered, fraction=Decimal("0.25"))),
        "p75_duration_seconds": str(percentile(values=ordered, fraction=Decimal("0.75"))),
        "p90_duration_seconds": str(percentile(values=ordered, fraction=Decimal("0.90"))),
        "shortest_duration_seconds": str(ordered[0]),
        "longest_duration_seconds": str(longest),
        "total_seconds_above_threshold": str(total),
        "dominant_episode_share": str(share),
        "clustered": share > Decimal("0.5"),
        "single_anomaly_dominates": share > Decimal("0.5"),
    }


@financial
def required_executable_dislocation_bps(*, taker_fee_rate: Decimal,
                                        slippage_bps: Decimal,
                                        policy: Any) -> Decimal:
    """The dislocation a Bitso trade must cover, derived from the project's own policy.

    **The spread term is zero and that is deliberate.** The dislocation being measured is built
    from Bitso's executable ask or bid, so Bitso's spread is already inside the number. Passing a
    spread here as well would charge the same basis points twice and raise the bar above what the
    economics actually require — the same double-counting error the project guards against in
    `friction_bps_for`.

    Derived through the canonical `minimum_viable_gross_edge_bps` rather than hardcoded, so a
    change to the policy or the account's fee tier is reflected rather than hidden behind a
    remembered 173.
    """
    from .viability import minimum_viable_gross_edge_bps

    return minimum_viable_gross_edge_bps(
        taker_fee_rate=taker_fee_rate, spread_bps=ZERO, slippage_bps=slippage_bps,
        policy=policy)


def classify_result(*, episodes_above_threshold: int, episodes_converged: int,
                    assets_with_episodes: int, minimum_independent_episodes: int,
                    evidence_proves_executability: bool,
                    validation_reproduced: bool | None
                    ) -> dict[str, Any]:
    """Choose the milestone's single dominant result (spec section 30).

    The ordering encodes the reasoning. Evidence that cannot prove executability can never yield a
    candidate, however large the numbers look. A repeated, converging, executable dislocation is
    the only path to a candidate, and it must also survive validation. Anything short of that is
    reported as not economic rather than as a candidate under review, because the spec's stop rule
    exists precisely to prevent a weak signal being carried forward as if it were promising.
    """
    if not evidence_proves_executability:
        return {"result": INSUFFICIENT_CROSS_VENUE_EVIDENCE,
                "reason": ("the available evidence class cannot prove an executable dislocation, "
                           "so no economic conclusion about one is available")}
    if episodes_above_threshold == 0:
        return {"result": CROSS_VENUE_SIGNAL_NOT_ECONOMIC,
                "reason": ("no episode exceeded the economic threshold, so no repeated "
                           "executable dislocation was observed")}
    if episodes_above_threshold < minimum_independent_episodes or assets_with_episodes < 1:
        return {"result": INSUFFICIENT_CROSS_VENUE_EVIDENCE,
                "reason": (f"only {episodes_above_threshold} episode(s) exceeded the threshold, "
                           f"below the {minimum_independent_episodes} required to distinguish a "
                           f"repeated dislocation from a single anomaly")}
    if episodes_converged == 0:
        return {"result": CROSS_VENUE_SIGNAL_NOT_ECONOMIC,
                "reason": ("dislocations exceeded the threshold but none converged, so a "
                           "directional entry would not have been resolvable")}
    if validation_reproduced is None:
        return {"result": INSUFFICIENT_CROSS_VENUE_EVIDENCE,
                "reason": "a candidate exists but has not been validated on the holdout"}
    if not validation_reproduced:
        return {"result": CROSS_VENUE_SIGNAL_NOT_ECONOMIC,
                "reason": ("the frozen candidate did not reproduce on the untouched holdout, so "
                           "the development structure does not generalise")}
    return {"result": CROSS_VENUE_ALPHA_CANDIDATE_FOUND,
            "reason": ("repeated executable dislocations exceeded the economic threshold and "
                       "converged on data unseen when the candidate was frozen")}

"""Alpha discovery: information content measured before any trade is contemplated.

This module exists because MVP 0.2.5 ended in `NO_CURRENT_EDGE` and the honest response was
not another indicator. Across five milestones the price-only directional work explored mean
reversion, trend continuation, volatility-aware mean reversion, range expansion, wider
horizons, explicit invalidation, asymmetric pullback entries and breakout/retest structures.
The last experiment produced 4,374 round trips across 64 configurations, none profitable, with
a 2.35% target-hit rate against a 75.6% invalidation rate. Narrowing risk fixed the risk-shape
gate completely and moved profitability not at all.

That outcome is informative in a specific way: it says the obstacle is **directional
prediction**, not risk geometry. So the next question is not "what is a better stop" but "is
there any independent information at all". That is a different kind of measurement, and this
module is built to make it honestly.

**Alpha is information, not PnL.** Nothing here opens a position, sizes anything, or has an
opinion about a stop. The only question is whether a feature observed at time T has predictive
content about the *executable* return after T. Keeping the measurement separate from the
trading problem matters because a PnL backtest confounds two things — whether the signal knows
something and whether the execution can monetise it — and the previous milestone showed how
easily the second can be mistaken for the first.

**The measurement is arranged so it can come back negative.** Four design choices do that work:

* Horizons are predeclared and few. Testing dozens and reporting the best is how a search
  manufactures a finding out of noise.
* Every result is paired with a **negative control** that should destroy it: a time-shifted
  leader, a randomised pairing, an intentional future leak that must be *detected*. A pipeline
  that always finds alpha is broken, and the controls are how that is caught.
* Sample counts are reported alongside the number of **effective independent opportunities**,
  because minute data is heavily autocorrelated and a correlation computed over 40,000
  overlapping observations is not 40,000 independent facts.
* Structure is required, not point estimates: a monotone response across feature buckets is
  credible, a single lucky bucket is not.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial
from autofund.replay.data import Candle

ALPHA_VERSION = "autofund.alpha-discovery.v1"

# ---- the frozen price-only baseline (section 1) ----
PRICE_ONLY_RESEARCH_BASELINE = "PRICE_ONLY_RESEARCH_BASELINE"

# Every profile whose conclusions are now historical evidence. Listed explicitly rather than
# derived from a registry so that a future addition cannot silently inherit this label.
FROZEN_PRICE_ONLY_PROFILES: tuple[str, ...] = (
    "mean-reversion-safe-v1",
    "trend-continuation-v1",
    "volatility-mean-reversion-v1",
    "volatility-mean-reversion-v2",
    "range-expansion-v1",
    "volatility-mean-reversion-15m-v1",
    "volatility-mean-reversion-1h-v1",
    "range-expansion-15m-v1",
    "range-expansion-1h-v1",
    "structural-invalidation-pullback-15m-PB-A-v1",
    "structural-invalidation-pullback-15m-PB-B-v1",
    "structural-invalidation-pullback-15m-PB-C-v1",
    "structural-invalidation-pullback-15m-PB-D-v1",
    "structural-invalidation-pullback-1h-PB-A-v1",
    "structural-invalidation-pullback-1h-PB-B-v1",
    "structural-invalidation-pullback-1h-PB-C-v1",
    "structural-invalidation-pullback-1h-PB-D-v1",
    "expansion-retest-15m-RT-A-v1", "expansion-retest-15m-RT-B-v1",
    "expansion-retest-15m-RT-C-v1", "expansion-retest-15m-RT-D-v1",
    "expansion-retest-1h-RT-A-v1", "expansion-retest-1h-RT-B-v1",
    "expansion-retest-1h-RT-C-v1", "expansion-retest-1h-RT-D-v1",
)

# ---- the alpha question (section 3) ----
PREDICTIVE = "PREDICTIVE"
NOT_PREDICTIVE = "NOT_PREDICTIVE"
INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"

# ---- candidate classifications (section 26) ----
#
# These describe WHAT WAS FOUND. They are not the milestone's terminal status, which is a
# different vocabulary (see TERMINAL_STATUSES) covering the whole research question including
# whether the evidence was even sufficient to answer it. Keeping them separate matters: the first
# run of this module reported `NO_SIGNAL` as its status, which reads as a market conclusion when
# the run had in fact failed to reach the question at all.
NO_SIGNAL = "NO_SIGNAL"
WEAK_UNSTABLE_SIGNAL = "WEAK_UNSTABLE_SIGNAL"
PREDICTIVE_NOT_ECONOMIC = "PREDICTIVE_NOT_ECONOMIC"
VALIDATED_ALPHA_SOURCE = "VALIDATED_ALPHA_SOURCE"
MICROSTRUCTURE_ACCUMULATING = "MICROSTRUCTURE_ACCUMULATING"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

# ---- terminal statuses (section 32) ----
# The closed vocabulary the milestone may report. Every one of these is an acceptable outcome;
# none is a failure to complete the work.
VALIDATED_ALPHA_SOURCE_FOUND = "VALIDATED_ALPHA_SOURCE_FOUND"
PREDICTIVE_BUT_NOT_ECONOMIC = "PREDICTIVE_BUT_NOT_ECONOMIC"
MICROSTRUCTURE_ACCUMULATING_STATUS = "MICROSTRUCTURE_ACCUMULATING"
NO_ALPHA_SOURCE_FOUND = "NO_ALPHA_SOURCE_FOUND"
INSUFFICIENT_EVIDENCE_STATUS = "INSUFFICIENT_EVIDENCE"
BLOCKED = "BLOCKED"

TERMINAL_STATUSES = (
    VALIDATED_ALPHA_SOURCE_FOUND,
    PREDICTIVE_BUT_NOT_ECONOMIC,
    MICROSTRUCTURE_ACCUMULATING_STATUS,
    NO_ALPHA_SOURCE_FOUND,
    INSUFFICIENT_EVIDENCE_STATUS,
    BLOCKED,
)

ALL_CLASSIFICATIONS: tuple[str, ...] = (
    NO_SIGNAL, WEAK_UNSTABLE_SIGNAL, PREDICTIVE_NOT_ECONOMIC, VALIDATED_ALPHA_SOURCE,
    MICROSTRUCTURE_ACCUMULATING, INSUFFICIENT_EVIDENCE)

# ---- predeclared prediction horizons (section 6) ----
# A small fixed set, frozen before any outcome is examined. Three horizons spanning roughly an
# order of magnitude, chosen because the friction analysis of 0.2.5 says the interesting
# question is whether predictive content scales faster than the horizon does.
PREDECLARED_HORIZONS_MINUTES: tuple[int, ...] = (1, 5, 15)

# Minimum observations before this module will state anything. Below it the verdict is
# INSUFFICIENT_SAMPLE rather than a weak claim, because a rank relationship over a handful of
# points is not evidence.
MINIMUM_OBSERVATIONS = 200

# Minimum *effective independent* observations. Minute-return data is autocorrelated, so a
# correlation over thousands of overlapping windows may rest on a few dozen independent
# movements. Requiring this separately is what stops a large nominal count from being read as
# statistical weight.
MINIMUM_EFFECTIVE_OBSERVATIONS = 50

# How many equal-count buckets a feature is split into when testing structure. Five is enough to
# see monotonicity and few enough that each bucket retains a usable sample.
DEFAULT_BUCKET_COUNT = 5

# A bucket's mean future return must be distinguishable from zero by at least this many
# multiples of its own standard error before the bucket is called directional. Chosen at 2.0
# rather than a p-value because it is a descriptive effect-size screen, not a significance test,
# and it is applied only as a *precondition* for the structural check that follows.
BUCKET_EFFECT_MULTIPLE = Decimal("2.0")

# Retained only so an old artifact referencing it still resolves. It is NOT a gate: the verdict
# is decided by `rank_significance` against the effective sample. An earlier version gated on a
# fixed floor of 0.03 chosen after the first run's output was seen, which is circular -- a
# threshold must not depend on the answer it is used to judge -- and a fixed floor cannot be right
# anyway, because 0.02 is meaningless evidence at 400 observations and overwhelming at 40,000.
MINIMUM_RANK_ASSOCIATION = Decimal("0")

# Two-sided critical value of the standard normal at alpha = 0.001, used by the rank
# significance test. Predeclared, and deliberately strict because this milestone runs 84
# comparisons: at this level the expected number of false positives across the whole family is
# 84 * 0.001 = 0.084, so a single surviving relationship is unlikely to be a fluke.
#
# Using a unit-normal critical value rather than a t critical value is a declared approximation.
# It is justified by the same quantity that makes the test possible at all: the test is only
# applied when the effective sample exceeds MINIMUM_EFFECTIVE_OBSERVATIONS, where the difference
# between the two is negligible. It is not applied to small samples, where it would matter.
RANK_CRITICAL_VALUE = Decimal("3.2905")

# The spread between the extreme bucket means, in bps, below which the information is reported as
# economically immaterial. This is a REPORTING threshold, not a gate on `PREDICTIVE`: the
# milestone asks whether information exists and separately whether it could pay, and collapsing
# those two questions is the mistake this constant exists to prevent. A relationship can carry
# genuine information and still describe a move far smaller than the friction a trade would pay.
MINIMUM_BUCKET_SPREAD_BPS = Decimal("1.0")

# Minimum chronological subwindows a relationship must hold in to be called stable. A signal
# present in one part of the sample and absent in another is a regime artefact.
MINIMUM_STABLE_SUBWINDOWS = 4
STABILITY_SUBWINDOWS = 4

# Multiple-testing budget (section 24). Declared so the search size is reportable, and enforced
# so the budget cannot quietly grow after a promising result.
MAX_FEATURE_FAMILIES = 6
MAX_RELATIONSHIPS = 12


class AlphaError(ValueError):
    """Alpha discovery was asked for something it cannot defend."""


@dataclass(frozen=True, slots=True)
class DiscoveryWindows:
    """Chronological split for alpha work.

    The previous milestone's holdout was never inspected and section 2 requires that it stays
    that way. Rather than reusing it, this milestone defines its own development window and a
    *future* validation window that must remain invisible until a candidate is frozen.

    Both are declared as absolute instants, so a later run cannot shift a boundary to include a
    favourable stretch of data.
    """

    development_start: datetime
    development_end: datetime
    validation_start: datetime
    validation_end: datetime
    note: str

    def __post_init__(self) -> None:
        for moment in (self.development_start, self.development_end,
                       self.validation_start, self.validation_end):
            if moment.tzinfo is None:
                raise AlphaError("window boundaries must be timezone-aware")
        if self.development_end <= self.development_start:
            raise AlphaError("development end must follow its start")
        if self.validation_end <= self.validation_start:
            raise AlphaError("validation end must follow its start")
        # Strictly after: an overlapping validation window would reuse development data as
        # though it were unseen, which is the specific failure the split exists to prevent.
        if self.validation_start < self.development_end:
            raise AlphaError("validation must start at or after development ends")

    @property
    def development_hours(self) -> Decimal:
        return Decimal(str((self.development_end
                            - self.development_start).total_seconds())) / Decimal("3600")

    def contains_development(self, moment: datetime) -> bool:
        return self.development_start <= moment < self.development_end

    def contains_validation(self, moment: datetime) -> bool:
        return self.validation_start <= moment < self.validation_end

    def public(self) -> dict[str, Any]:
        return {"development_start": self.development_start.isoformat(),
                "development_end": self.development_end.isoformat(),
                "validation_start": self.validation_start.isoformat(),
                "validation_end": self.validation_end.isoformat(),
                "development_hours": str(self.development_hours),
                "validation_untouched": True, "note": self.note}


def declare_windows(*, now: datetime, development_hours: int = 720,
                    validation_hours: int = 168,
                    gap_hours: int = 1) -> DiscoveryWindows:
    """Declare a chronological split ending at `now`.

    Development is the older period and validation is the most recent one, both inside the
    history that can actually be fetched. An earlier version placed validation *after* `now`,
    which sounds stricter but is unusable: no such data exists at run time, so validation
    returned zero observations and the candidate appeared to have failed a test it never took.
    A split whose second half cannot exist is not a clean split, it is a broken one.

    Validation is nonetheless untouched during discovery, and that guarantee comes from the
    measurement code rather than from the dates: every discovery measurement filters to
    `contains_development` only, and the validation slice is read solely by the validation step
    after a candidate has been frozen. The one-hour gap keeps the two windows from sharing a bar,
    so no observation can be counted in both.

    The constraint the dates *do* enforce is the one that matters for interpretation: validation
    lies strictly after development, so a candidate chosen on development cannot have been chosen
    using later data.
    """
    if development_hours <= 0 or validation_hours <= 0 or gap_hours < 0:
        raise AlphaError("window durations must be positive")
    moment = now.astimezone(UTC)
    validation_end = moment
    validation_start = validation_end - timedelta(hours=validation_hours)
    development_end = validation_start - timedelta(hours=gap_hours)
    development_start = development_end - timedelta(hours=development_hours)
    return DiscoveryWindows(
        development_start=development_start, development_end=development_end,
        validation_start=validation_start, validation_end=validation_end,
        note=("validation is the most recent period and lies strictly after development; it is "
              "read only by the validation step, after a candidate is frozen"))


@dataclass(frozen=True, slots=True)
class MultipleTestLedger:
    """How many things were tried.

    Reported because the alternative is publishing the best of an undisclosed number of
    attempts. Every family, relationship, horizon and bucket examined is counted here, and the
    count is part of the result rather than an appendix to it.
    """

    feature_families_examined: int
    relationships_examined: int
    horizons_examined: int
    buckets_per_feature: int
    negative_controls_run: int
    failed_candidates_retained: int

    def __post_init__(self) -> None:
        if self.feature_families_examined < 0 or self.relationships_examined < 0:
            raise AlphaError("counts must not be negative")
        if self.feature_families_examined > MAX_FEATURE_FAMILIES:
            raise AlphaError(
                f"feature family budget exceeded ({MAX_FEATURE_FAMILIES})")
        if self.relationships_examined > MAX_RELATIONSHIPS:
            raise AlphaError(f"relationship budget exceeded ({MAX_RELATIONSHIPS})")

    @property
    def total_comparisons(self) -> int:
        """The multiplicity a reader needs in order to interpret the best result."""
        return (self.relationships_examined * self.horizons_examined
                * max(1, self.buckets_per_feature))

    def public(self) -> dict[str, Any]:
        return {"feature_families_examined": self.feature_families_examined,
                "relationships_examined": self.relationships_examined,
                "horizons_examined": self.horizons_examined,
                "buckets_per_feature": self.buckets_per_feature,
                "negative_controls_run": self.negative_controls_run,
                "failed_candidates_retained": self.failed_candidates_retained,
                "total_comparisons": self.total_comparisons,
                "best_result_interpreted_against_this_multiplicity": True,
                "budget": {"max_feature_families": MAX_FEATURE_FAMILIES,
                           "max_relationships": MAX_RELATIONSHIPS}}


@dataclass(frozen=True, slots=True)
class FeatureBucket:
    """One equal-count bucket of a feature, with the forward return that followed it."""

    index: int
    lower_bound: Decimal
    upper_bound: Decimal
    observations: int
    mean_forward_return_bps: Decimal
    standard_error_bps: Decimal
    hit_rate_above_zero: Decimal
    median_forward_return_bps: Decimal

    @property
    def effect_multiple(self) -> Decimal | None:
        """Mean divided by its own standard error. None when the bucket has no spread."""
        if self.standard_error_bps <= ZERO:
            return None
        return self.mean_forward_return_bps / self.standard_error_bps

    @property
    def directional(self) -> bool:
        """Whether this bucket's mean is distinguishable from zero in the declared sense."""
        multiple = self.effect_multiple
        return multiple is not None and abs(multiple) >= BUCKET_EFFECT_MULTIPLE

    def public(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {"index": self.index, "lower_bound": str(self.lower_bound),
                "upper_bound": str(self.upper_bound), "observations": self.observations,
                "mean_forward_return_bps": str(self.mean_forward_return_bps),
                "standard_error_bps": str(self.standard_error_bps),
                "median_forward_return_bps": str(self.median_forward_return_bps),
                "hit_rate_above_zero": str(self.hit_rate_above_zero),
                "effect_multiple": s(self.effect_multiple),
                "directional": self.directional}


@dataclass(frozen=True, slots=True)
class PredictiveContent:
    """What a feature said about what happened next, with its sample honestly counted."""

    feature_name: str
    market: str
    horizon_minutes: int
    observations: int
    effective_observations: Decimal
    rank_relationship: Decimal | None
    buckets: tuple[FeatureBucket, ...]
    monotone: bool
    monotone_direction: str
    stable_subwindows: int
    stable: bool
    feature_distribution: tuple[Decimal, Decimal, Decimal]
    forward_return_distribution: tuple[Decimal, Decimal, Decimal]
    verdict: str
    notes: dict[str, str]

    @property
    def predictive(self) -> bool:
        return self.verdict == PREDICTIVE

    def public(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {"feature_name": self.feature_name, "market": self.market,
                "horizon_minutes": self.horizon_minutes,
                "observations": self.observations,
                "effective_observations": str(self.effective_observations),
                "rank_relationship": s(self.rank_relationship),
                "buckets": [b.public() for b in self.buckets],
                "monotone": self.monotone, "monotone_direction": self.monotone_direction,
                "stable_subwindows": self.stable_subwindows, "stable": self.stable,
                "feature_distribution_min_median_max": [str(v)
                                                        for v in self.feature_distribution],
                "forward_return_distribution_min_median_max": [
                    str(v) for v in self.forward_return_distribution],
                "verdict": self.verdict, "notes": dict(self.notes),
                "sample_counts_reported": True,
                "effective_count_estimated": True}


@financial
def forward_return_bps(*, candles: tuple[Candle, ...], index: int,
                       horizon: int) -> Decimal | None:
    """The return from the bar *after* `index` to `index + horizon`.

    The entry is priced at the open of `index + 1`, not the close of `index`. The distinction is
    the whole reason this function exists as its own unit: a feature computed from data up to and
    including `index` cannot be traded at that bar's close, so measuring the forward return from
    the close would credit the signal with a move it could not have captured. Starting strictly
    after the observation is what makes the number a prediction rather than a restatement.

    Returns None when the future is not available, rather than a zero or a partial return. A
    truncated forward window is missing data, and substituting zero would drag every estimate
    toward no-effect.
    """
    entry_index = index + 1
    exit_index = index + horizon
    if index < 0 or horizon <= 0:
        raise AlphaError("horizon must be positive")
    if entry_index >= len(candles) or exit_index >= len(candles):
        return None
    entry = candles[entry_index].open
    exit_price = candles[exit_index].close
    if entry <= ZERO:
        return None
    return (exit_price - entry) / entry * Decimal("10000")


@financial
def effective_observation_count(*, values: tuple[Decimal, ...],
                                max_lag: int = 60) -> Decimal:
    """A rough count of independent observations in an autocorrelated series.

    Minute returns are strongly autocorrelated, so a rank relationship computed over thousands
    of overlapping windows may rest on a small number of genuine movements. This estimates how
    many independent movements the sample actually contains by summing the autocorrelation
    function until it turns negative, which is the standard crude correction.

    Deliberately crude and reported as an estimate: the purpose is to stop a large nominal count
    being read as statistical weight, not to produce a precise figure that could itself be
    over-trusted. It can only reduce the apparent sample size, never increase it.
    """
    count = len(values)
    if count < 3:
        return Decimal(count)
    mean = sum(values, ZERO) / Decimal(count)
    centred = [value - mean for value in values]
    variance = sum((value * value for value in centred), ZERO) / Decimal(count)
    if variance <= ZERO:
        return Decimal(count)
    limit = min(max_lag, count - 2)
    factor = ONE_VALUE
    for lag in range(1, limit + 1):
        pairs = count - lag
        if pairs <= 0:
            break
        total = sum((centred[i] * centred[i + lag] for i in range(pairs)), ZERO)
        correlation = (total / Decimal(pairs)) / variance
        if correlation <= ZERO:
            break
        factor += Decimal("2") * correlation
    if factor <= ZERO:
        return Decimal(count)
    return Decimal(count) / factor


ONE_VALUE = Decimal("1")


@financial
def rank_significance(*, relationship: Decimal, effective_observations: Decimal
                      ) -> tuple[bool, Decimal]:
    """Whether a rank association is distinguishable from no association.

    Under the null of independence the rank correlation has standard error `1 / sqrt(n - 1)`,
    where `n` is the number of INDEPENDENT observations, so the association is judged against
    that scale rather than against a fixed number.

    This is the correction for the defect that made the first run report 21 of 84 combinations
    predictive. The nominal count was ~43,000, at which `1 / sqrt(n)` is about 0.0048, so almost
    any association at all was "significant". The nominal count is wrong, because minute returns
    overlap and the series is heavily autocorrelated: thousands of windows can rest on a handful
    of movements. Substituting the autocorrelation-adjusted effective count asks whether the
    relationship is strong relative to the number of genuinely independent movements, which is
    the question that was always meant.

    The test deliberately uses only the effective count and does not also demand a minimum raw
    count. Adding an independent bar on the raw count would be a second, redundant chance to
    reject the same measurement, and the effective count already falls below the floor exactly
    when the raw count carries that little information.
    """
    if effective_observations <= ONE_VALUE:
        return False, ZERO
    standard_error = ONE_VALUE / (effective_observations - ONE_VALUE).sqrt()
    if standard_error <= ZERO:
        return False, ZERO
    statistic = abs(relationship) / standard_error
    return statistic >= RANK_CRITICAL_VALUE, statistic


@financial
def split_buckets(*, feature: tuple[Decimal, ...], forward: tuple[Decimal, ...],
                  bucket_count: int = DEFAULT_BUCKET_COUNT,
                  ) -> tuple[FeatureBucket, ...]:
    """Equal-count buckets of the feature, each with the forward return that followed.

    Equal-count rather than equal-width because a feature's distribution is rarely uniform, and
    equal-width buckets would put almost every observation in one bucket for a heavy-tailed
    feature. Buckets are formed on the feature's own quantiles, so each carries a comparable
    sample and the comparison between them is meaningful.
    """
    if len(feature) != len(forward):
        raise AlphaError("feature and forward series must be aligned")
    if len(feature) < bucket_count:
        return ()
    order = sorted(range(len(feature)), key=lambda i: feature[i])
    buckets: list[FeatureBucket] = []
    size = len(order) // bucket_count
    if size < 1:
        return ()
    for index in range(bucket_count):
        start = index * size
        # The final bucket absorbs the remainder so every observation is accounted for. Dropping
        # the tail would quietly discard the most extreme feature values, which are exactly the
        # ones a directional claim depends on.
        end = len(order) if index == bucket_count - 1 else (index + 1) * size
        members = order[start:end]
        if not members:
            continue
        returns = [forward[i] for i in members]
        features = [feature[i] for i in members]
        mean = sum(returns, ZERO) / Decimal(len(returns))
        ordered_returns = sorted(returns)
        median = ordered_returns[len(ordered_returns) // 2]
        if len(returns) > 1:
            variance = (sum(((value - mean) ** 2 for value in returns), ZERO)
                        / Decimal(len(returns) - 1))
            standard_error = (variance.sqrt() / Decimal(len(returns)).sqrt()
                              if variance > ZERO else ZERO)
        else:
            standard_error = ZERO
        above = sum(1 for value in returns if value > ZERO)
        buckets.append(FeatureBucket(
            index=index, lower_bound=min(features), upper_bound=max(features),
            observations=len(returns), mean_forward_return_bps=mean,
            standard_error_bps=standard_error, median_forward_return_bps=median,
            hit_rate_above_zero=Decimal(above) / Decimal(len(returns))))
    return tuple(buckets)


def _rank_relationship(*, feature: tuple[Decimal, ...],
                       forward: tuple[Decimal, ...]) -> Decimal | None:
    """Spearman rank correlation, computed without a statistics dependency.

    Rank rather than Pearson because a genuine relationship between a leader's move and a
    follower's future return need not be linear, and rank correlation detects any monotone
    association. Ties are averaged, which is what makes it a rank correlation rather than a
    disguised parameterisation.
    """
    count = len(feature)
    if count < 3:
        return None

    def ranks(values: tuple[Decimal, ...]) -> list[Decimal]:
        order = sorted(range(count), key=lambda i: values[i])
        out = [ZERO] * count
        position = 0
        while position < count:
            end = position
            while end + 1 < count and values[order[end + 1]] == values[order[position]]:
                end += 1
            average = Decimal(position + end) / Decimal("2") + Decimal("1")
            for slot in range(position, end + 1):
                out[order[slot]] = average
            position = end + 1
        return out

    rank_a = ranks(feature)
    rank_b = ranks(forward)
    mean_a = sum(rank_a, ZERO) / Decimal(count)
    mean_b = sum(rank_b, ZERO) / Decimal(count)
    cov = sum(((rank_a[i] - mean_a) * (rank_b[i] - mean_b) for i in range(count)), ZERO)
    var_a = sum(((value - mean_a) ** 2 for value in rank_a), ZERO)
    var_b = sum(((value - mean_b) ** 2 for value in rank_b), ZERO)
    if var_a <= ZERO or var_b <= ZERO:
        return None
    return cov / (var_a.sqrt() * var_b.sqrt())


def _tie_fraction(*, values: tuple[Decimal, ...]) -> Decimal:
    """The share of a series that sits exactly at its most common value.

    Reported purely to explain why a measurement was insensitive. Minute bars frequently do not
    move at all, so a large fraction of forward returns can be exactly zero; when that happens the
    bucket means collapse onto a handful of values and a genuinely perfect relationship can fail
    to look monotone. Reporting the fraction makes that visible instead of leaving a null result
    that looks like evidence when it is only an absence of resolution.
    """
    if not values:
        return ZERO
    counts: dict[Decimal, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return Decimal(max(counts.values())) / Decimal(len(values))


def _monotone(*, buckets: tuple[FeatureBucket, ...]) -> tuple[bool, str]:
    """Whether the bucket means move consistently in one direction.

    Requires every consecutive step to move the same way and at least one bucket to be
    individually directional. A monotone sequence of tiny, indistinguishable values is not
    structure; it is noise that happens to be ordered, which is why the directional condition is
    part of the definition rather than a separate observation.
    """
    if len(buckets) < 3:
        return False, "NONE"
    means = [bucket.mean_forward_return_bps for bucket in buckets]
    increasing = all(means[i] < means[i + 1] for i in range(len(means) - 1))
    decreasing = all(means[i] > means[i + 1] for i in range(len(means) - 1))
    if not (increasing or decreasing):
        return False, "NONE"
    if not any(bucket.directional for bucket in buckets):
        return False, "NONE"
    return True, "INCREASING" if increasing else "DECREASING"


def _stable(*, feature: tuple[Decimal, ...], forward: tuple[Decimal, ...],
            bucket_count: int, subwindows: int = STABILITY_SUBWINDOWS,
            minimum: int = MINIMUM_STABLE_SUBWINDOWS) -> tuple[int, bool]:
    """How many chronological subwindows reproduce the same sign of relationship.

    Split by time, not at random. A relationship that appears in four consecutive independent
    periods is far more credible than one that appears in one and reverses in the others, and a
    random split would let the same period appear in several subwindows and inflate agreement.
    """
    count = len(feature)
    if count < subwindows * bucket_count:
        return 0, False
    size = count // subwindows
    held = 0
    signs: list[int] = []
    for index in range(subwindows):
        start = index * size
        end = count if index == subwindows - 1 else (index + 1) * size
        relationship = _rank_relationship(feature=feature[start:end],
                                        forward=forward[start:end])
        if relationship is None:
            continue
        signs.append(0 if relationship == ZERO else (1 if relationship > ZERO else -1))
    if not signs:
        return 0, False
    from collections import Counter

    common, occurrences = Counter(signs).most_common(1)[0]
    del common
    held = occurrences
    return held, held >= minimum


def assess_predictive_content(*, feature_name: str, market: str, horizon_minutes: int,
                              feature: tuple[Decimal, ...],
                              forward: tuple[Decimal, ...],
                              bucket_count: int = DEFAULT_BUCKET_COUNT,
                              ) -> PredictiveContent:
    """Measure what one feature said about what followed, and say so honestly.

    The verdict requires all four of: enough observations, enough *effective* observations,
    interpretable structure, and stability across chronological subwindows. Any one of those
    failing returns a non-predictive verdict with the reason recorded, so a near-miss is
    distinguishable from an absence of signal rather than being flattened into one outcome.
    """
    if len(feature) != len(forward):
        raise AlphaError("feature and forward series must be aligned")
    observations = len(feature)
    notes: dict[str, str] = {}
    effective = effective_observation_count(values=feature)
    notes["effective_count_method"] = "autocorrelation-summed, can only reduce the count"

    def unavailable(reason: str) -> PredictiveContent:
        return PredictiveContent(
            feature_name=feature_name, market=market, horizon_minutes=horizon_minutes,
            observations=observations, effective_observations=effective,
            rank_relationship=None, buckets=(), monotone=False, monotone_direction="NONE",
            stable_subwindows=0, stable=False, feature_distribution=(ZERO, ZERO, ZERO),
            forward_return_distribution=(ZERO, ZERO, ZERO),
            verdict=INSUFFICIENT_SAMPLE, notes={**notes, "reason": reason})

    if observations < MINIMUM_OBSERVATIONS:
        return unavailable("BELOW_MINIMUM_OBSERVATIONS")
    if effective < MINIMUM_EFFECTIVE_OBSERVATIONS:
        return unavailable("BELOW_MINIMUM_EFFECTIVE_OBSERVATIONS")

    ordered_feature = sorted(feature)
    ordered_forward = sorted(forward)
    feature_summary = (ordered_feature[0], ordered_feature[len(ordered_feature) // 2],
                       ordered_feature[-1])
    forward_summary = (ordered_forward[0], ordered_forward[len(ordered_forward) // 2],
                       ordered_forward[-1])
    buckets = split_buckets(feature=feature, forward=forward, bucket_count=bucket_count)
    monotone, direction = _monotone(buckets=buckets)
    relationship = _rank_relationship(feature=feature, forward=forward)
    stable_count, stable = _stable(feature=feature, forward=forward,
                                   bucket_count=bucket_count)
    # The spread between the extreme bucket means: the economic size of the effect, as opposed
    # to how confidently it was detected. Reported so a reader can see both.
    spread = ZERO
    if buckets:
        means = [bucket.mean_forward_return_bps for bucket in buckets]
        spread = max(means) - min(means)
    notes["rank_relationship"] = "Spearman; detects any monotone association"
    notes["stability_method"] = "chronological subwindows, sign agreement"
    notes["extreme_bucket_spread_bps"] = str(spread)
    notes["minimum_bucket_spread_bps"] = str(MINIMUM_BUCKET_SPREAD_BPS)
    notes["spread_is_reporting_only"] = (
        "the spread describes whether the information could pay; it does not gate PREDICTIVE, "
        "because this milestone asks whether information exists and separately whether it is "
        "economic")

    # Tie mass, reported because it explains an otherwise mysterious failure mode. When a large
    # share of forward returns are exactly zero -- which is what one-minute bars look like when
    # most bars do not move -- the bucket means collapse onto a few distinct values, consecutive
    # buckets become equal, and `_monotone` cannot see an ordering that is in fact present. This
    # is not a gate: it is a diagnostic, and the `FUTURE_LEAK` control is what actually decides
    # whether a measurement could have seen anything.
    notes["forward_tie_fraction"] = str(_tie_fraction(values=forward))
    notes["feature_tie_fraction"] = str(_tie_fraction(values=feature))

    # Judged against the effective sample, not the nominal one. See `rank_significance`.
    significant = False
    statistic = ZERO
    if relationship is not None:
        significant, statistic = rank_significance(relationship=relationship,
                                                   effective_observations=effective)
    notes["rank_statistic"] = str(statistic)
    notes["rank_critical_value"] = str(RANK_CRITICAL_VALUE)
    notes["rank_test"] = "association over 1/sqrt(effective - 1), two-sided alpha 0.001"

    verdict = PREDICTIVE
    if relationship is None:
        verdict = NOT_PREDICTIVE
        notes["structure"] = "rank association undefined, so no ordering can be assessed"
    elif not significant:
        verdict = NOT_PREDICTIVE
        notes["structure"] = (
            f"rank association {abs(relationship):.4f} is not distinguishable from none on "
            f"{effective} effective observations (statistic {statistic:.3f} < "
            f"{RANK_CRITICAL_VALUE})")
    elif not monotone:
        verdict = NOT_PREDICTIVE
        notes["structure"] = "bucket means are not monotone in the feature"
    elif not stable:
        verdict = NOT_PREDICTIVE
        notes["structure"] = "relationship does not reproduce across chronological subwindows"
    if verdict == PREDICTIVE:
        notes["structure"] = (
            f"monotone {direction}, stable in {stable_count} subwindows, and material to "
            f"{spread} bps across the extreme buckets")
        notes["economically_immaterial"] = str(spread < MINIMUM_BUCKET_SPREAD_BPS)

    return PredictiveContent(
        feature_name=feature_name, market=market, horizon_minutes=horizon_minutes,
        observations=observations, effective_observations=effective,
        rank_relationship=relationship, buckets=buckets, monotone=monotone,
        monotone_direction=direction, stable_subwindows=stable_count, stable=stable,
        feature_distribution=feature_summary, forward_return_distribution=forward_summary,
        verdict=verdict, notes=notes)


__all__ = [
    "ALL_CLASSIFICATIONS",
    "ALPHA_VERSION",
    "BUCKET_EFFECT_MULTIPLE",
    "DEFAULT_BUCKET_COUNT",
    "FROZEN_PRICE_ONLY_PROFILES",
    "INSUFFICIENT_EVIDENCE",
    "INSUFFICIENT_SAMPLE",
    "MAX_FEATURE_FAMILIES",
    "MAX_RELATIONSHIPS",
    "MICROSTRUCTURE_ACCUMULATING",
    "MINIMUM_EFFECTIVE_OBSERVATIONS",
    "MINIMUM_OBSERVATIONS",
    "MINIMUM_STABLE_SUBWINDOWS",
    "NOT_PREDICTIVE",
    "NO_SIGNAL",
    "PREDICTIVE",
    "PREDICTIVE_NOT_ECONOMIC",
    "PRICE_ONLY_RESEARCH_BASELINE",
    "STABILITY_SUBWINDOWS",
    "VALIDATED_ALPHA_SOURCE",
    "WEAK_UNSTABLE_SIGNAL",
    "AlphaError",
    "DiscoveryWindows",
    "FeatureBucket",
    "MultipleTestLedger",
    "PredictiveContent",
    "assess_predictive_content",
    "declare_windows",
    "effective_observation_count",
    "forward_return_bps",
    "split_buckets",
]

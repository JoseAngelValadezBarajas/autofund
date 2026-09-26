"""A synchronized multi-market panel, and the lead-lag features built on it.

The panel is the part of this milestone where a defect would be hardest to see and most
destructive. Every feature here compares one market's past against another market's future, so a
single misalignment produces a correlation that looks like alpha and is an artefact of the
join.

**Alignment is explicit and one-directional.** `synchronize` builds, for each decision instant,
a mapping from market to the *most recent bar that had closed at or before that instant*. It
never forward-fills a price into a feature: a market whose last close is too old is reported as
stale and excluded from that observation rather than carried forward. That distinction is the
whole point — a stale price carried forward is a fabricated observation, and a feature computed
from one has no interpretation.

**Timestamps are bucket-open labels**, which is the convention `backfill` already uses: a candle
labelled 12:00 covers the minute 12:00 to 12:01 and its close is known at 12:01. The panel
therefore maps an instant to `label + interval <= instant`, which is why the comparison is
against a shifted boundary rather than the label itself. Getting this wrong by one interval
would systematically shift every feature one bar earlier and hand the experiment a genuine leak.

**A lead-lag relationship is not correlation.** Contemporaneous co-movement is reported
separately and explicitly, because two assets that move together tell you nothing about which
one moves first. Only a leader's value at or before T predicting a follower's return strictly
after T is treated as predictive.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial
from autofund.replay.data import Candle

PANEL_VERSION = "autofund.market-panel.v1"

# A market's most recent close may be at most this old to be usable at a decision instant.
# Exceeding it means the market is stale: still trading somewhere, but not represented in this
# observation. Chosen at twice the base interval so a single missing bar is tolerated while a
# genuinely halted or illiquid market is not silently included.
DEFAULT_MAX_STALENESS_SECONDS = 120

# ---- the predeclared relationship set (section 5) ----
# Directional pairs plus the two aggregate constructions. Small and economically interpretable:
# BTC is the venue's reference asset, the basket represents the market factor, and the divergence
# measures an asset moving against its own sector.
# Both directions of each pair are NOT declared: a leader is a claim about which market moves
# first, and declaring both directions would double the search without adding a hypothesis.
PREDECLARED_RELATIONSHIPS: tuple[tuple[str, str], ...] = (
    ("BTC/MXN", "ETH/MXN"),
    ("BTC/MXN", "SOL/MXN"),
    ("BTC/MXN", "XRP/MXN"),
    ("ETH/MXN", "SOL/MXN"),
    ("BASKET", "BTC/MXN"),
    ("BASKET", "ETH/MXN"),
    ("BASKET", "SOL/MXN"),
    ("BASKET", "XRP/MXN"),
    ("DIVERGENCE", "BTC/MXN"),
    ("DIVERGENCE", "ETH/MXN"),
    ("DIVERGENCE", "SOL/MXN"),
    ("DIVERGENCE", "XRP/MXN"),
)

BASKET = "BASKET"
DIVERGENCE = "DIVERGENCE"

# ---- the predeclared feature set (section 7) ----
LAGGED_RETURN = "LAGGED_RETURN"
RELATIVE_RETURN_VS_BASKET = "RELATIVE_RETURN_VS_BASKET"
CROSS_SECTIONAL_DISPERSION = "CROSS_SECTIONAL_DISPERSION"
LEADER_FOLLOWER_DIVERGENCE = "LEADER_FOLLOWER_DIVERGENCE"
VOLATILITY_ADJUSTED_RELATIVE_MOVE = "VOLATILITY_ADJUSTED_RELATIVE_MOVE"
MARKET_BREADTH = "MARKET_BREADTH"

PREDECLARED_FEATURES: tuple[str, ...] = (
    LAGGED_RETURN, RELATIVE_RETURN_VS_BASKET, CROSS_SECTIONAL_DISPERSION,
    LEADER_FOLLOWER_DIVERGENCE, VOLATILITY_ADJUSTED_RELATIVE_MOVE, MARKET_BREADTH)

FEATURE_INTERPRETATION: dict[str, str] = {
    LAGGED_RETURN: "the leader's own recent move; tests whether a move persists into the "
                   "follower at all",
    RELATIVE_RETURN_VS_BASKET: "the follower's move relative to the market, which mean "
                               "reversion predicts should reverse",
    CROSS_SECTIONAL_DISPERSION: "how far apart the markets are; high dispersion suggests a "
                                "common factor is being repriced",
    LEADER_FOLLOWER_DIVERGENCE: "leader move minus follower move; the direct statement of the "
                                "lead-lag hypothesis",
    VOLATILITY_ADJUSTED_RELATIVE_MOVE: "a relative move scaled by its own recent volatility, so "
                                       "a large move in a quiet market is not equated with a "
                                       "small move in a violent one",
    MARKET_BREADTH: "the fraction of markets moving in the same direction; a breadth extreme "
                    "is a candidate regime marker",
}

# Bars used to estimate recent volatility for the adjusted feature. Declared once so the feature
# cannot be re-tuned after its result is known.
VOLATILITY_WINDOW_BARS = 20


class PanelError(ValueError):
    """The panel was asked for something it cannot defend."""


@dataclass(frozen=True, slots=True)
class MarketSeries:
    """One market's bars, indexed for repeated lookup by decision instant."""

    market: str
    candles: tuple[Candle, ...]
    interval: timedelta

    def __post_init__(self) -> None:
        if not self.market:
            raise PanelError("market is required")
        if self.interval <= timedelta(0):
            raise PanelError("interval must be positive")

    @property
    def close_times(self) -> tuple[datetime, ...]:
        """When each bar's close became known: its label plus one interval.

        Materialised on demand. Callers inside a hot loop use `index_at`, which compares against
        the shifted boundary directly rather than building this sequence, because at 54,721 bars
        rebuilding it per lookup makes the panel quadratic and unusable.
        """
        return tuple(candle.timestamp + self.interval for candle in self.candles)

    def index_at(self, moment: datetime, *,
                 max_staleness_seconds: int = DEFAULT_MAX_STALENESS_SECONDS) -> int | None:
        """The index of the most recent bar that had closed by `moment`, or None if stale.

        Binary search comparing each candidate's *close* against `moment`, computed inline rather
        than from a prebuilt list of close times. A label marks the start of its bar, so
        comparing `moment` against a label would return the bar that was still forming, whose
        close the decision could not have known -- that off-by-one is the most likely way this
        module could leak.

        The comparisons are written as `timestamp <= moment - interval` so the subtraction
        happens once instead of once per probe.
        """
        if not self.candles:
            return None
        boundary = moment - self.interval
        low, high = 0, len(self.candles) - 1
        found = -1
        while low <= high:
            middle = (low + high) // 2
            if self.candles[middle].timestamp <= boundary:
                found = middle
                low = middle + 1
            else:
                high = middle - 1
        if found < 0:
            return None
        age = (moment - (self.candles[found].timestamp + self.interval)).total_seconds()
        if age > max_staleness_seconds:
            return None
        return found

    def return_bps(self, index: int, *, bars: int = 1) -> Decimal | None:
        """Close-to-close return over `bars`, ending at `index`."""
        start = index - bars
        if start < 0 or index >= len(self.candles) or index < 0:
            return None
        begin = self.candles[start].close
        end = self.candles[index].close
        if begin <= ZERO:
            return None
        return (end - begin) / begin * Decimal("10000")

    def volatility_bps(self, index: int, *, window: int = VOLATILITY_WINDOW_BARS
                       ) -> Decimal | None:
        """Mean absolute close-to-close move over the window ending at `index`.

        Mean absolute rather than standard deviation because it is more robust to the isolated
        large bars that crypto data contains, and because the feature only needs a scale, not a
        distributional assumption.
        """
        start = index - window
        if start < 0 or index >= len(self.candles):
            return None
        moves: list[Decimal] = []
        for position in range(start + 1, index + 1):
            previous = self.candles[position - 1].close
            current = self.candles[position].close
            if previous > ZERO:
                moves.append(abs(current - previous) / previous * Decimal("10000"))
        if not moves:
            return None
        return sum(moves, ZERO) / Decimal(len(moves))

    def forward_return_bps(self, index: int, *, bars: int) -> Decimal | None:
        """The return from the bar *after* `index` through `index + bars`.

        Entry at the open of `index + 1`: a decision taken at `index` cannot be executed at that
        bar's close, so pricing the forward return from the close would credit the signal with a
        move that had already happened.
        """
        entry_index = index + 1
        exit_index = index + bars
        if entry_index >= len(self.candles) or exit_index >= len(self.candles):
            return None
        entry = self.candles[entry_index].open
        exit_price = self.candles[exit_index].close
        if entry <= ZERO:
            return None
        return (exit_price - entry) / entry * Decimal("10000")


@dataclass(frozen=True, slots=True)
class PanelObservation:
    """One synchronized instant: what was known, and what happened next.

    Features and forward returns are held as mappings rather than tuples because every
    combination in the search reads them once per observation. A linear scan over ~40 features
    per lookup turns a few hundred passes over 54,721 observations into billions of comparisons,
    which is the difference between a search that finishes and one that appears to hang.
    """

    moment: datetime
    indices: tuple[tuple[str, int], ...]
    stale_markets: tuple[str, ...]
    features: dict[str, Decimal]
    forward: dict[str, Decimal]

    def feature(self, name: str) -> Decimal | None:
        return self.features.get(name)

    def forward_for(self, key: str) -> Decimal | None:
        return self.forward.get(key)

    def public(self) -> dict[str, Any]:
        return {"moment": self.moment.isoformat(),
                "indices": [list(item) for item in self.indices],
                "stale_markets": list(self.stale_markets),
                "features": [[k, str(v)] for k, v in sorted(self.features.items())],
                "forward": [[k, str(v)] for k, v in sorted(self.forward.items())],
                "uses_only_past": True}


@dataclass(frozen=True, slots=True)
class MarketPanel:
    """Synchronized observations across markets, with the features derived from them."""

    interval: timedelta
    markets: tuple[str, ...]
    observations: tuple[PanelObservation, ...]
    decision_instants: int
    markets_excluded_stale: int
    feature_names: tuple[str, ...]

    def public(self) -> dict[str, Any]:
        return {"version": PANEL_VERSION, "interval_seconds": self.interval.total_seconds(),
                "markets": list(self.markets),
                "observations": len(self.observations),
                "decision_instants": self.decision_instants,
                "markets_excluded_stale": self.markets_excluded_stale,
                "features": list(self.feature_names),
                "forward_fill_used": False,
                "stale_markets_excluded": True}

    def series(self, *, feature: str, market: str) -> tuple[tuple[Decimal, ...],
                                                           tuple[Decimal, ...]]:
        """Aligned (feature, forward return) pairs for one feature and follower market.

        Only observations where both exist are returned, and they are returned in chronological
        order so the stability subwindows remain chronological.
        """
        features: list[Decimal] = []
        forwards: list[Decimal] = []
        for observation in self.observations:
            value = observation.feature(feature)
            outcome = observation.forward_for(market)
            if value is None or outcome is None:
                continue
            features.append(value)
            forwards.append(outcome)
        return tuple(features), tuple(forwards)

    def pairs_for(self, *, feature: str, market: str, horizon: int
                  ) -> tuple[tuple[datetime, ...], tuple[Decimal, ...],
                             tuple[Decimal, ...]]:
        """Moments, feature values and forward returns for one combination, in ONE pass.

        A single traversal is what makes the search tractable: extracting the moments and the
        values separately would double the work per combination, and with a few hundred
        combinations over 54,721 observations that doubling is measured in minutes.
        """
        key = f"{market}|{horizon}"
        moments: list[datetime] = []
        features: list[Decimal] = []
        forwards: list[Decimal] = []
        for observation in self.observations:
            value = observation.features.get(feature)
            if value is None:
                continue
            outcome = observation.forward.get(key)
            if outcome is None:
                continue
            moments.append(observation.moment)
            features.append(value)
            forwards.append(outcome)
        return tuple(moments), tuple(features), tuple(forwards)

    def keys_for(self, *, feature: str, market: str,
                 horizon: int) -> tuple[datetime, ...]:
        """Decision instants at which both the feature and that follower's return exist."""
        return self.pairs_for(feature=feature, market=market, horizon=horizon)[0]

    def aligned(self, *, feature: str, market: str,
                horizon: int) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...]]:
        """Feature and forward-return series for one feature, follower and horizon."""
        pairs = self.pairs_for(feature=feature, market=market, horizon=horizon)
        return pairs[1], pairs[2]


@financial
def _basket_return(*, returns: dict[str, Decimal]) -> Decimal | None:
    """Equal-weighted mean return across the available markets.

    Equal-weighted rather than capitalisation-weighted because the panel's purpose is to
    represent the common factor, and the available books are four assets whose relative
    capitalisations are not observable from the data this project collects.
    """
    values = [value for value in returns.values()]
    if not values:
        return None
    return sum(values, ZERO) / Decimal(len(values))


def build_panel(*, series: Sequence[MarketSeries], interval_seconds: int,
                horizons: Sequence[int] = (),
                max_staleness_seconds: int = DEFAULT_MAX_STALENESS_SECONDS,
                lookback_bars: int = 1,
                ) -> MarketPanel:
    """Synchronize markets and derive the predeclared features at each decision instant.

    Decision instants are the close times of the *first* market's bars, so the panel is anchored
    to a real observation grid rather than an arbitrary clock. At each instant every market is
    resolved to its most recent closed bar; a market is recorded as stale rather than carried
    forward, and a market with no usable bar contributes no features and no forward returns for
    that instant.

    The forward returns are computed for every market at every instant, including ones whose own
    features were unavailable, so a feature from one market can be aligned against the future of
    another. That is the entire purpose of the panel and the alignment is by instant, not by
    index.
    """
    if not series:
        raise PanelError("at least one market series is required")
    if interval_seconds <= 0:
        raise PanelError("interval must be positive")
    if lookback_bars < 1:
        raise PanelError("lookback must be at least one bar")

    markets = tuple(item.market for item in series)
    anchor = series[0]
    observations: list[PanelObservation] = []
    stale_exclusions = 0
    for candle in anchor.candles:
        moment = candle.timestamp + anchor.interval
        resolved: dict[str, int] = {}
        stale: list[str] = []
        for item in series:
            index = item.index_at(moment, max_staleness_seconds=max_staleness_seconds)
            if index is None:
                stale.append(item.market)
                stale_exclusions += 1
                continue
            resolved[item.market] = index

        features: list[tuple[str, Decimal]] = []
        forward: list[tuple[str, Decimal]] = []

        # ---- forward returns, strictly after the instant ----
        for item in series:
            index = resolved.get(item.market)
            if index is None:
                continue
            for horizon in horizons:
                outcome = item.forward_return_bps(index, bars=horizon)
                if outcome is not None:
                    forward.append((f"{item.market}|{horizon}", outcome))

        # ---- features, from data at or before the instant ----
        if len(resolved) >= 2:
            by_market: dict[str, MarketSeries] = {item.market: item for item in series}
            returns: dict[str, Decimal] = {}
            for market, index in resolved.items():
                value = by_market[market].return_bps(index, bars=lookback_bars)
                if value is not None:
                    returns[market] = value
            basket = _basket_return(returns=returns)
            if basket is not None and len(returns) >= 2:
                dispersion = (max(returns.values()) - min(returns.values()))
                breadth = Decimal(sum(1 for v in returns.values() if v > ZERO)) / Decimal(
                    len(returns))
                for market, index in resolved.items():
                    own = returns.get(market)
                    if own is None:
                        continue
                    series_for_market = by_market[market]
                    # The leader features attach to the *follower* pair declared in section 5,
                    # so a feature is only emitted for a market that some relationship names as
                    # a follower. Emitting them for every market would grow the search without a
                    # hypothesis behind the extra rows.
                    features.append((f"{LAGGED_RETURN}|{market}", own))
                    features.append((f"{RELATIVE_RETURN_VS_BASKET}|{market}", own - basket))
                    features.append((f"{CROSS_SECTIONAL_DISPERSION}|{market}", dispersion))
                    features.append((f"{MARKET_BREADTH}|{market}", breadth))
                    volatility = series_for_market.volatility_bps(index)
                    if volatility is not None and volatility > ZERO:
                        features.append((f"{VOLATILITY_ADJUSTED_RELATIVE_MOVE}|{market}",
                                         (own - basket) / volatility))
                    for leader, follower in PREDECLARED_RELATIONSHIPS:
                        if follower != market:
                            continue
                        if leader in returns:
                            features.append(
                                (f"{LEADER_FOLLOWER_DIVERGENCE}|{leader}|{market}",
                                 returns[leader] - own))

        observations.append(PanelObservation(
            moment=moment, indices=tuple(sorted(resolved.items())),
            stale_markets=tuple(stale), features=dict(features), forward=dict(forward)))

    return MarketPanel(
        interval=timedelta(seconds=interval_seconds), markets=markets,
        observations=tuple(observations), decision_instants=len(observations),
        markets_excluded_stale=stale_exclusions, feature_names=PREDECLARED_FEATURES)


def basket_series(*, series: Sequence[MarketSeries], interval_seconds: int) -> MarketSeries:
    """The equal-weighted basket as a synthetic series, for BASKET-leader relationships.

    Constructed from the *past* only: each bar's close is the mean of the constituent closes at
    the same label, and bars are only emitted where every constituent has one. A partially
    populated basket bar would represent a market that did not exist.
    """
    if not series:
        raise PanelError("at least one market series is required")
    by_label: dict[datetime, list[Decimal]] = {}
    counts: dict[datetime, int] = {}
    expected = len(series)
    for item in series:
        for candle in item.candles:
            by_label.setdefault(candle.timestamp, []).append(candle.close)
            counts[candle.timestamp] = counts.get(candle.timestamp, 0) + 1
    averaged: list[Candle] = []
    for label in sorted(by_label):
        if counts[label] != expected:
            continue
        closes = by_label[label]
        close = sum(closes, ZERO) / Decimal(len(closes))
        averaged.append(Candle(timestamp=label, open=close, high=close, low=close,
                               close=close, volume=ZERO))
    return MarketSeries(market=BASKET, candles=tuple(averaged),
                        interval=timedelta(seconds=interval_seconds))


__all__ = [
    "BASKET",
    "CROSS_SECTIONAL_DISPERSION",
    "DEFAULT_MAX_STALENESS_SECONDS",
    "DIVERGENCE",
    "FEATURE_INTERPRETATION",
    "LAGGED_RETURN",
    "LEADER_FOLLOWER_DIVERGENCE",
    "MARKET_BREADTH",
    "PANEL_VERSION",
    "PREDECLARED_FEATURES",
    "PREDECLARED_RELATIONSHIPS",
    "RELATIVE_RETURN_VS_BASKET",
    "VOLATILITY_ADJUSTED_RELATIVE_MOVE",
    "VOLATILITY_WINDOW_BARS",
    "MarketPanel",
    "MarketSeries",
    "PanelError",
    "PanelObservation",
    "basket_series",
    "build_panel",
]

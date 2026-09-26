"""Structural levels known strictly before entry.

This module is the one place where a lookahead defect would be both easy to introduce and
hard to notice, so the design is built around making it *impossible to express* an
after-the-fact level rather than around being careful.

The hazard is specific and worth naming. "Enter near support" is trivially testable if you
are allowed to look at the whole series and ask which prior low the price happened to
respect — every losing trade was, in hindsight, near *some* level that later held or failed.
A strategy written that way reports excellent geometry and has measured nothing, because the
level was chosen by the outcome it is supposed to predict.

The defence used here is **one-sided windows with an explicit exclusion**:

    pivot_high(candles, index, left, right)

accepts a candidate index and asks only whether the bars at `index - left .. index - 1` and
`index + 1 .. index + right` are all lower. It cannot see past `index + right`, and callers
in this milestone pass `right=0`-equivalent windows anchored so that the newest bar a pivot
may occupy is the last *completed* bar at decision time.

Two further properties are enforced structurally rather than documented:

* `levels_known_at(candles, decision_index)` takes the decision index explicitly and slices
  `candles[:decision_index + 1]`. A caller cannot accidentally pass the future, because the
  future is not an argument it can supply.
* Every returned level carries the index of the bar that produced it, so a test can assert
  `level.source_index < decision_index` for every level, on every trade, mechanically. That
  assertion is what turns "we were careful" into something checkable.

All levels are level *prices*, never "the best support" — the module has no API for ranking
levels by how well price later respected them, which is the operation that would smuggle the
future back in.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial
from autofund.replay.data import Candle

from .execution_model import BPS

STRUCTURE_VERSION = "autofund.market-structure.v1"

# A pivot must have at least this many bars on each side. One is the minimum that makes
# "higher than its neighbours" a meaningful statement rather than a tautology, and it keeps
# the level recent enough to be relevant. Declared here so a challenger cannot quietly
# widen it after seeing results.
DEFAULT_PIVOT_WINDOW = 2

# How far back a structural level may be drawn from. A level from very long ago describes a
# market regime that has since changed, and including it would let a challenger find
# "support" far below the current price and claim a wide invalidation that nothing supports.
DEFAULT_LOOKBACK_BARS = 60

# A candidate entry must be within this distance of the level to count as "at" it, expressed
# as a fraction of the level's distance to the invalidation. The rule is proportional rather
# than a fixed bps figure because the meaningful question is whether the level is close
# relative to the risk being taken, not close in absolute terms.
DEFAULT_PROXIMITY_FRACTION = Decimal("0.25")


class StructureError(ValueError):
    """A structural level was requested in a way that cannot be defended."""


@dataclass(frozen=True, slots=True)
class StructuralLevel:
    """One level, with the index of the bar that created it.

    `source_index` is mandatory and is the mechanism by which leakage becomes detectable:
    a caller that received this level can assert it predates its decision, and the assertion
    is about data, not about intent.
    """

    kind: str
    price_mxn: Decimal
    source_index: int
    age_bars: int
    touches: int

    def __post_init__(self) -> None:
        if self.kind not in ("PIVOT_LOW", "PIVOT_HIGH", "RANGE_LOW", "RANGE_HIGH"):
            raise StructureError(f"unknown level kind: {self.kind}")
        if self.price_mxn <= ZERO:
            raise StructureError("a structural level must have a positive price")
        if self.source_index < 0:
            raise StructureError("source_index must not be negative")
        if self.age_bars < 0:
            raise StructureError("age_bars must not be negative")

    def public(self) -> dict[str, Any]:
        return {"version": STRUCTURE_VERSION, "kind": self.kind,
                "price_mxn": str(self.price_mxn), "source_index": self.source_index,
                "age_bars": self.age_bars, "touches": self.touches,
                "known_before_decision": True}


@financial
def _is_pivot_low(candles: Sequence[Candle], index: int, half_width: int) -> bool:
    """Whether `index` is a local low using only bars before and after it.

    The bar at `index` is compared with `half_width` bars on each side. The caller is
    responsible for ensuring `index + half_width` has already occurred at decision time;
    `levels_known_at` is what guarantees that.
    """
    if index - half_width < 0 or index + half_width >= len(candles):
        return False
    low = candles[index].low
    for offset in range(1, half_width + 1):
        if candles[index - offset].low <= low or candles[index + offset].low <= low:
            return False
    return True


@financial
def _is_pivot_high(candles: Sequence[Candle], index: int, half_width: int) -> bool:
    if index - half_width < 0 or index + half_width >= len(candles):
        return False
    high = candles[index].high
    for offset in range(1, half_width + 1):
        if candles[index - offset].high >= high or candles[index + offset].high >= high:
            return False
    return True


def _inclusive_end(*, decision_index: int, half_width: int, lookback: int) -> int:
    """The newest index a pivot may occupy given that its right-hand bars are confirmed.

    A pivot at index `i` is only *confirmed* once bar `i + half_width` has closed. At a
    decision taken on bar `decision_index`, the newest confirmable pivot therefore sits at
    `decision_index - half_width`. Returning anything larger would be reporting a level
    whose confirming bars had not yet happened.
    """
    newest = decision_index - half_width
    if newest < 0:
        return -1
    return max(newest, decision_index - lookback)


@financial
def levels_known_at(*, candles: Sequence[Candle], decision_index: int,
                    half_width: int = DEFAULT_PIVOT_WINDOW,
                    lookback: int = DEFAULT_LOOKBACK_BARS) -> tuple[StructuralLevel, ...]:
    """Every structural level a decision at `decision_index` could legitimately know.

    `candles[:decision_index + 1]` is the whole visible history; the function cannot read a
    bar the decision had not seen even if one exists in the passed sequence, because it
    never indexes beyond `decision_index`. Pivot confirmation subtracts `half_width` more.

    Costs nothing at the boundary: a decision with too little history returns no levels
    rather than a level inferred from insufficient data.
    """
    if decision_index < 0:
        raise StructureError("decision_index must not be negative")
    if half_width < 1:
        raise StructureError("pivot half-width must be at least one bar")
    if lookback < 1:
        raise StructureError("lookback must be at least one bar")
    visible = candles[:decision_index + 1]
    if len(visible) < (half_width * 2) + 1:
        return ()

    newest = _inclusive_end(decision_index=decision_index, half_width=half_width,
                            lookback=lookback)
    if newest < 0:
        return ()
    oldest = max(half_width, decision_index - lookback)
    levels: list[StructuralLevel] = []
    for index in range(oldest, newest + 1):
        if _is_pivot_low(visible, index, half_width):
            levels.append(StructuralLevel(
                kind="PIVOT_LOW", price_mxn=visible[index].low, source_index=index,
                age_bars=decision_index - index, touches=1))
        if _is_pivot_high(visible, index, half_width):
            levels.append(StructuralLevel(
                kind="PIVOT_HIGH", price_mxn=visible[index].high, source_index=index,
                age_bars=decision_index - index, touches=1))
    return tuple(levels)


@financial
def nearest_level_below(*, levels: Sequence[StructuralLevel], price_mxn: Decimal,
                        kind: str | None = None,
                        ) -> StructuralLevel | None:
    """The highest level strictly below `price_mxn`.

    Chosen by geometry, not by outcome. Nothing here asks whether price later held the
    level, which is the query that would make the choice retroactive.
    """
    candidates = [level for level in levels
                  if level.price_mxn < price_mxn and (kind is None or level.kind == kind)]
    if not candidates:
        return None
    return max(candidates, key=lambda level: level.price_mxn)


@financial
def nearest_level_above(*, levels: Sequence[StructuralLevel], price_mxn: Decimal,
                        kind: str | None = None,
                        ) -> StructuralLevel | None:
    candidates = [level for level in levels
                  if level.price_mxn > price_mxn and (kind is None or level.kind == kind)]
    if not candidates:
        return None
    return min(candidates, key=lambda level: level.price_mxn)


@financial
def recent_range(*, candles: Sequence[Candle], decision_index: int,
                 window: int = DEFAULT_LOOKBACK_BARS,
                 ) -> tuple[Decimal, Decimal]:
    """Lowest low and highest high over a window ending at the decision bar.

    The decision bar is included because its own close has happened, and excluding it would
    discard the most recent information for no defensibility gain. Bars after it are not
    reachable: the slice ends at `decision_index`.
    """
    if decision_index < 0:
        raise StructureError("decision_index must not be negative")
    start = max(0, decision_index - window + 1)
    visible = candles[start:decision_index + 1]
    if not visible:
        return ZERO, ZERO
    return (min(candle.low for candle in visible),
            max(candle.high for candle in visible))


@financial
def proximity_bps(*, price_mxn: Decimal, level_mxn: Decimal) -> Decimal:
    """Distance from a price to a level, in bps of the price."""
    if price_mxn <= ZERO or level_mxn <= ZERO:
        return ZERO
    return abs(price_mxn - level_mxn) / price_mxn * BPS


@financial
def retest_confirmed(*, candles: Sequence[Candle], decision_index: int,
                     level_mxn: Decimal, tolerance_bps: Decimal,
                     ) -> bool:
    """Whether a prior bar traded back to a level within tolerance, using only past bars.

    This is the "wait for the retest" building block for a breakout-retest challenger. It
    looks only at bars before `decision_index`, so the retest it reports has already
    happened when the decision is taken. It deliberately does not require that the level
    subsequently *held*: that would be reading the future.
    """
    if decision_index <= 0 or level_mxn <= ZERO:
        return False
    for index in range(decision_index - 1, -1, -1):
        candle = candles[index]
        if candle.low <= level_mxn <= candle.high:
            return True
        distance = proximity_bps(price_mxn=candle.close, level_mxn=level_mxn)
        if distance <= tolerance_bps:
            return True
    return False


__all__ = [
    "DEFAULT_LOOKBACK_BARS",
    "DEFAULT_PIVOT_WINDOW",
    "DEFAULT_PROXIMITY_FRACTION",
    "STRUCTURE_VERSION",
    "StructuralLevel",
    "StructureError",
    "levels_known_at",
    "nearest_level_above",
    "nearest_level_below",
    "proximity_bps",
    "recent_range",
    "retest_confirmed",
]

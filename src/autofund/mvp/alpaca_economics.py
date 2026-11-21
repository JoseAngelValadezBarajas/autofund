"""Alpaca venue economics (A2): what a round trip costs, with the provenance of every component.

Pure arithmetic over measured quotes and declared assumptions. There is no trading capability here.

Two rules carry over from ``venue_economics``:

* A component whose value is not known is **unknown**, and it propagates. It is never replaced by
  zero, so the reported total is then explicitly a *lower bound*.
* A measured number and an assumed number are labelled differently and never merged.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal
from statistics import median

from autofund.decimal_utils import ZERO, financial
from autofund.exchanges.alpaca.models import Quote

from .execution_model import BPS
from .venue_economics import ACCOUNT_RATE_UNKNOWN, PUBLISHED_BASELINE, UNVERIFIED

ECONOMICS_VERSION = "autofund.alpaca-economics.v1"
MEASURED = "MEASURED"

# Regular US session is 09:30-16:00 Eastern: 13:30-20:00 UTC in summer and 14:30-21:00 UTC in
# winter. Only the overlap is certain in both, so a quote counts as in-session only inside it. This
# errs toward excluding borderline quotes, which is the safe direction for a spread estimate.
_SESSION_START, _SESSION_END = time(14, 30), time(20, 0)


def in_regular_session(at: datetime) -> bool:
    moment = at.astimezone(UTC)
    return moment.weekday() < 5 and _SESSION_START <= moment.time() < _SESSION_END


@financial
def half_spread_bps(quote: Quote) -> Decimal:
    """Half of the quoted spread, in basis points of the mid price."""
    mid = (quote.bid + quote.ask) / 2
    return (quote.ask - quote.bid) / mid / 2 * BPS


@dataclass(frozen=True, slots=True)
class SpreadMeasurement:
    symbol: str
    samples: int
    excluded_out_of_session: int
    median_half_spread_bps: Decimal | None
    max_half_spread_bps: Decimal | None
    provenance: str = MEASURED

    @property
    def usable(self) -> bool:
        return self.median_half_spread_bps is not None


def measure_spread(symbol: str, quotes: list[Quote]) -> SpreadMeasurement:
    """Summarise recorded quotes for one symbol. Out-of-session quotes are counted, not used."""
    if any(q.symbol != symbol for q in quotes):
        raise ValueError("quotes for a different symbol were supplied")
    inside = [q for q in quotes if in_regular_session(q.at)]
    if not inside:
        return SpreadMeasurement(symbol, 0, len(quotes), None, None)
    values = [half_spread_bps(q) for q in inside]
    return SpreadMeasurement(
        symbol=symbol,
        samples=len(values),
        excluded_out_of_session=len(quotes) - len(inside),
        median_half_spread_bps=median(values),
        max_half_spread_bps=max(values),
    )


@dataclass(frozen=True, slots=True)
class Component:
    name: str
    bps: Decimal | None
    provenance: str


@dataclass(frozen=True, slots=True)
class RoundTripFriction:
    """Round-trip cost in bps. ``total_bps`` is a lower bound whenever a component is unknown."""

    symbol: str
    components: tuple[Component, ...]
    version: str = ECONOMICS_VERSION

    @property
    def total_bps(self) -> Decimal:
        return sum((c.bps for c in self.components if c.bps is not None), ZERO)

    @property
    def is_lower_bound(self) -> bool:
        return any(c.bps is None for c in self.components)

    @property
    def unknown(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.components if c.bps is None)


@financial
def round_trip_friction(
    spread: SpreadMeasurement,
    *,
    slippage_bps_per_side: Decimal,
    commission_bps_per_side: Decimal | None = ZERO,
) -> RoundTripFriction:
    """Compose a round trip: cross the half-spread twice, slip twice, pay commission twice.

    ``slippage_bps_per_side`` is a declared assumption and stays labelled ``UNVERIFIED`` until it is
    compared with paper fills. Regulatory sell-side fees have no rate declared here, so they are
    carried as unknown and make the total a lower bound rather than being silently set to zero.
    """
    if not spread.usable:
        raise ValueError("no in-session quotes: spread is unmeasured, friction cannot be composed")
    if spread.median_half_spread_bps is None:  # unreachable after `usable`, but never skipped under -O
        raise ValueError("no in-session quotes: spread is unmeasured")
    components = (
        Component("spread", spread.median_half_spread_bps * 2, MEASURED),
        Component("slippage", slippage_bps_per_side * 2, UNVERIFIED),
        Component(
            "commission",
            None if commission_bps_per_side is None else commission_bps_per_side * 2,
            PUBLISHED_BASELINE if commission_bps_per_side is not None else ACCOUNT_RATE_UNKNOWN,
        ),
        Component("regulatory_sell_fees", None, ACCOUNT_RATE_UNKNOWN),
    )
    return RoundTripFriction(spread.symbol, components)


@dataclass(frozen=True, slots=True)
class Headroom:
    """Predictive movement versus friction, in separate fields. Neither is the other."""

    movement_bps: Decimal
    friction_bps: Decimal
    friction_is_lower_bound: bool

    @property
    def headroom_bps(self) -> Decimal:
        return self.movement_bps - self.friction_bps

    @property
    def pays_for_itself(self) -> bool:
        # Only a *positive* headroom against a lower-bound friction is inconclusive; a negative one
        # is conclusive, because the true friction can only be larger.
        return self.headroom_bps > ZERO and not self.friction_is_lower_bound

    @property
    def conclusive(self) -> bool:
        return self.headroom_bps <= ZERO or not self.friction_is_lower_bound

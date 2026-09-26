"""Whether an external venue could supply INFORMATION to AutoFund without becoming its venue.

This is a different question from whether a venue could *execute* the strategy, and the spec is
careful to keep them apart. A venue can be useless for trading and still be the most informative
price available; conversely, a cheap venue is worthless as a reference if its data cannot be read
in time to be informative. The module therefore assesses both halves separately and refuses to
let a conclusion about one leak into the other.

**A price difference is not arbitrage, and this module never calls it that.** Two venues quoting
the same asset at different prices is the normal state of the world, not an opportunity: the
difference can sit inside the spread of both books, can be stale on one side, can be explained by
the quote currency, and could not be collected without simultaneously holding inventory on both
venues. The spec's section 16 is explicit that transfer arbitrage is out of scope, and section 15
is explicit that a raw difference must not be labelled arbitrage.

**What this module does claim.** It measures the contemporaneous dislocation between the
incumbent and a candidate reference venue that quotes the *same* currency, so no conversion is
needed and the comparison is like-for-like. That measurement answers the spec's section 15
questions about scale directly, and its honest use is as a bound on what cross-venue timing could
possibly offer before any execution cost is considered — the same role the 2.51 bps predictive
movement plays for the incumbent's own signal.

**Feasibility is returned, not an adapter.** The spec forbids building an integration here. What
is recorded is whether each venue's public surface *could* support a synchronized study later, so
a future milestone can start from an evidence-based answer instead of rediscovering it.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ONE, ZERO, financial

from .execution_model import BPS

REFERENCE_VERSION = "autofund.cross-venue-reference.v1"

# ---- what a dislocation may be compared against ----
# A dislocation is only interesting if it is larger than the round-trip cost of collecting it, so
# every assessment reports the comparison explicitly rather than leaving the reader to make it.
DISLOCATION_EXCEEDS_FRICTION = "EXCEEDS_REQUIRED_FRICTION"
DISLOCATION_INSIDE_FRICTION = "INSIDE_REQUIRED_FRICTION"
DISLOCATION_EVIDENCE_INSUFFICIENT = "DISLOCATION_EVIDENCE_INSUFFICIENT"

# ---- reference-data feasibility verdicts ----
REFERENCE_DATA_READY = "READY_FOR_SYNCHRONIZED_RESEARCH"
REFERENCE_DATA_PARTIAL = "PARTIAL_PUBLIC_SURFACE"
REFERENCE_DATA_UNAVAILABLE = "PUBLIC_SURFACE_INSUFFICIENT"
REFERENCE_DATA_UNKNOWN = "PUBLIC_SURFACE_UNKNOWN"


class ReferenceError(ValueError):
    """A cross-venue measurement is malformed or inconsistent with itself."""


@dataclass(frozen=True, slots=True)
class DislocationObservation:
    """One contemporaneous comparison of the same pair on two venues.

    Both prices are in the **same quote currency** by construction. That is a deliberate
    restriction: comparing a MXN price with a USD price would require an FX rate, and an FX rate
    brings its own spread, its own staleness and its own fee, so the measured difference would no
    longer be attributable to the venues. Only like-quoted pairs are admitted, and the constructor
    refuses anything else rather than silently normalising it.

    `stale_seconds` is recorded because two prices are never simultaneous. A difference measured
    across a five-second gap is a different object from one measured across five minutes, and
    without this field the two would be indistinguishable in the results.
    """

    pair: str
    reference_venue: str
    incumbent_venue: str
    reference_price: Decimal
    incumbent_price: Decimal
    observed_at: str
    stale_seconds: Decimal

    def __post_init__(self) -> None:
        if not self.pair:
            raise ReferenceError("pair is required")
        if self.reference_price <= ZERO or self.incumbent_price <= ZERO:
            raise ReferenceError("prices must be positive")
        if self.stale_seconds < ZERO:
            raise ReferenceError("staleness cannot be negative")

    @property
    def dislocation_bps(self) -> Decimal:
        """How far the incumbent sits from the reference, in bps of the reference.

        Signed so the direction is preserved: positive means the incumbent is quoting higher than
        the reference. The reference is the denominator because it is the venue being proposed as
        the informative one, so the measurement reads as "how far behind is the incumbent".
        """
        return ((self.incumbent_price - self.reference_price)
                / self.reference_price * BPS)

    def public(self) -> dict[str, Any]:
        return {"pair": self.pair, "reference_venue": self.reference_venue,
                "incumbent_venue": self.incumbent_venue,
                "reference_price": str(self.reference_price),
                "incumbent_price": str(self.incumbent_price),
                "dislocation_bps": str(self.dislocation_bps),
                "observed_at": self.observed_at,
                "stale_seconds": str(self.stale_seconds)}


@dataclass(frozen=True, slots=True)
class DislocationStudy:
    """The measured distribution of a dislocation sample, and what it can support.

    The summary deliberately reports the **absolute** magnitude alongside the signed mean. A
    signed mean near zero would otherwise suggest no dislocation exists when in fact large
    differences in both directions may be cancelling, so the absolute figure is what a
    timing-based hypothesis would actually have to work with.
    """

    pair: str
    reference_venue: str
    incumbent_venue: str
    observations: tuple[DislocationObservation, ...]
    maximum_stale_seconds: Decimal

    def __post_init__(self) -> None:
        if self.maximum_stale_seconds < ZERO:
            raise ReferenceError("maximum_stale_seconds cannot be negative")
        if not self.observations:
            raise ReferenceError("a study needs at least one observation")
        pair = self.observations[0].pair
        for observation in self.observations:
            if observation.pair != pair:
                raise ReferenceError("a study compares one pair; found mixed pairs")

    @property
    def usable(self) -> tuple[DislocationObservation, ...]:
        """Observations fresh enough for the comparison to mean anything.

        A pair of prices separated by more than the declared staleness ceiling is not a
        contemporaneous comparison, so it is excluded rather than averaged in. Excluding it is the
        conservative direction: staleness can only inflate a measured difference, so dropping
        stale samples cannot manufacture a dislocation that is not there.
        """
        return tuple(item for item in self.observations
                     if item.stale_seconds <= self.maximum_stale_seconds)

    @property
    def absolute_mean_bps(self) -> Decimal | None:
        if not self.usable:
            return None
        total = sum((abs(item.dislocation_bps) for item in self.usable), ZERO)
        return total / Decimal(len(self.usable))

    @property
    def absolute_max_bps(self) -> Decimal | None:
        if not self.usable:
            return None
        return max(abs(item.dislocation_bps) for item in self.usable)

    @property
    def signed_mean_bps(self) -> Decimal | None:
        if not self.usable:
            return None
        total = sum((item.dislocation_bps for item in self.usable), ZERO)
        return total / Decimal(len(self.usable))

    def public(self) -> dict[str, Any]:
        return {"pair": self.pair, "reference_venue": self.reference_venue,
                "incumbent_venue": self.incumbent_venue,
                "observations": len(self.observations),
                "usable_observations": len(self.usable),
                "maximum_stale_seconds": str(self.maximum_stale_seconds),
                "absolute_mean_bps": (None if self.absolute_mean_bps is None
                                      else str(self.absolute_mean_bps)),
                "absolute_max_bps": (None if self.absolute_max_bps is None
                                     else str(self.absolute_max_bps)),
                "signed_mean_bps": (None if self.signed_mean_bps is None
                                    else str(self.signed_mean_bps)),
                "is_arbitrage_claim": False,
                "interpretation": (
                    "a contemporaneous price difference between two venues quoting the same "
                    "currency; NOT arbitrage, and not collectable without inventory on both "
                    "venues and the costs that implies")}


def assess_dislocation(*, study: DislocationStudy, friction_bps: Decimal,
                       capture_fraction: Decimal = ONE) -> dict[str, Any]:
    """Whether measured dislocation is larger than the cost of acting on it.

    The comparison is between the *absolute* mean and the friction, because a timing hypothesis
    would be trading the magnitude of the gap rather than its average sign. Even so, exceeding
    friction is reported as a bound and not as an opportunity: the dislocation would still have to
    persist long enough to be entered and exited, which this measurement cannot show.
    """
    if capture_fraction <= ZERO or capture_fraction > ONE:
        raise ReferenceError("capture_fraction must be in (0, 1]")
    if friction_bps < ZERO:
        raise ReferenceError("friction_bps cannot be negative")
    magnitude = study.absolute_mean_bps
    if magnitude is None:
        return {"verdict": DISLOCATION_EVIDENCE_INSUFFICIENT,
                "usable_observations": 0,
                "reason": ("no observation was fresh enough to be a contemporaneous "
                           "comparison, so no scale can be reported")}
    budget = friction_bps / capture_fraction
    exceeds = magnitude > budget
    return {
        "verdict": DISLOCATION_EXCEEDS_FRICTION if exceeds else DISLOCATION_INSIDE_FRICTION,
        "absolute_mean_dislocation_bps": str(magnitude),
        "absolute_max_dislocation_bps": str(study.absolute_max_bps),
        "signed_mean_dislocation_bps": str(study.signed_mean_bps),
        "capture_fraction": str(capture_fraction),
        "friction_implied_budget_bps": str(budget),
        "usable_observations": len(study.usable),
        "exceeds_required_friction": exceeds,
        "is_arbitrage_claim": False,
        "transfer_arbitrage_considered": False,
        "reason": (f"mean absolute dislocation {magnitude:.4f} bps is "
                   f"{'above' if exceeds else 'below'} the {budget:.4f} bps a "
                   f"{capture_fraction * Decimal('100')}% capture would need to clear "
                   f"{friction_bps:.2f} bps of friction"),
    }


@dataclass(frozen=True, slots=True)
class ReferenceDataCapability:
    """What a venue's public surface could support for future synchronized research.

    Every field is optional because the honest answer to "can this venue provide a trade tape" is
    sometimes "not established from the sources consulted", and that is a different answer from
    "no". Collapsing the two would let an unread documentation page read as a limitation of the
    venue.
    """

    venue: str
    trade_tape: bool | None
    best_bid_ask: bool | None
    order_book: bool | None
    timestamps: bool | None
    candles: bool | None
    sequence_data: bool | None
    websocket: bool | None
    authentication_required: bool | None
    notes: str = ""

    @property
    def verdict(self) -> str:
        """Whether the surface could support a synchronized cross-venue study.

        A trade tape plus best bid and ask, readable without authentication, is the minimum for
        the spec's section 14 questions: without a tape there is no aggressor direction, and
        without a top of book there is no reference price to synchronize against.
        """
        fields = (self.trade_tape, self.best_bid_ask, self.order_book)
        if all(value is None for value in fields):
            return REFERENCE_DATA_UNKNOWN
        if self.trade_tape and self.best_bid_ask and self.authentication_required is False:
            return REFERENCE_DATA_READY
        if self.order_book or self.trade_tape:
            return REFERENCE_DATA_PARTIAL
        return REFERENCE_DATA_UNAVAILABLE

    def public(self) -> dict[str, Any]:
        return {"venue": self.venue, "trade_tape": self.trade_tape,
                "best_bid_ask": self.best_bid_ask, "order_book": self.order_book,
                "timestamps": self.timestamps, "candles": self.candles,
                "sequence_data": self.sequence_data, "websocket": self.websocket,
                "authentication_required": self.authentication_required,
                "verdict": self.verdict, "notes": self.notes}


@financial
def implied_lag_bps(*, leader_move_bps: Decimal, responsiveness: Decimal) -> Decimal:
    """How much of a leader's move a follower would need to have not yet made.

    A bounded, deliberately simple model used only to frame the reference-venue hypothesis: it
    states what would have to be true, not what is. `responsiveness` is the fraction of a leader
    move the follower has already taken, so `1 - responsiveness` is what remains unabsorbed.

    Reported as a requirement rather than an estimate because this milestone has not measured it.
    """
    if not (ZERO <= responsiveness <= ONE):
        raise ReferenceError("responsiveness must be in [0, 1]")
    return leader_move_bps * (ONE - responsiveness)


def reference_venue_hypothesis(*, leader_move_bps: Decimal, friction_bps: Decimal,
                               responsiveness: Decimal) -> dict[str, Any]:
    """Frame what a cross-venue lead-lag would have to deliver to be worth pursuing (sec. 13).

    The spec calls this REFERENCE_VENUE_ALPHA rather than arbitrage, and the distinction is
    substantive: the external venue would supply a *signal* about where the incumbent is about to
    move, and any trade would still happen on the incumbent with the incumbent's costs and
    liquidity. Nothing here proposes trading the other venue, and nothing here proposes moving
    funds between venues.

    The arithmetic is deliberately a requirement and not a forecast. It says how large a leader
    move would have to be, given an assumed responsiveness, before the unabsorbed remainder could
    clear the incumbent's friction. Whether such moves occur is exactly what a future study would
    measure, and this milestone does not claim to have done so.
    """
    if friction_bps < ZERO:
        raise ReferenceError("friction_bps cannot be negative")
    required = friction_bps
    residual = implied_lag_bps(leader_move_bps=leader_move_bps, responsiveness=responsiveness)
    return {
        "hypothesis": "REFERENCE_VENUE_ALPHA",
        "is_arbitrage": False,
        "is_execution_migration": False,
        "trade_venue": "incumbent",
        "information_venue": "external",
        "assumed_responsiveness": str(responsiveness),
        "leader_move_bps": str(leader_move_bps),
        "unabsorbed_lag_bps": str(residual),
        "incumbent_friction_bps": str(friction_bps),
        "residual_exceeds_friction": residual > required,
        "required_leader_move_bps": str(friction_bps / (ONE - responsiveness)
                                        if responsiveness < ONE else Decimal("Infinity")),
        "note": ("a requirement, not a measurement: this states the leader move that would be "
                 "needed, and whether such moves occur is what a synchronized study would test"),
    }


def build_capability(*, venue: str, trade_tape: bool | None, best_bid_ask: bool | None,
                     order_book: bool | None, timestamps: bool | None,
                     candles: bool | None, sequence_data: bool | None,
                     websocket: bool | None, authentication_required: bool | None,
                     notes: str = "") -> ReferenceDataCapability:
    return ReferenceDataCapability(
        venue=venue, trade_tape=trade_tape, best_bid_ask=best_bid_ask, order_book=order_book,
        timestamps=timestamps, candles=candles, sequence_data=sequence_data,
        websocket=websocket, authentication_required=authentication_required, notes=notes)

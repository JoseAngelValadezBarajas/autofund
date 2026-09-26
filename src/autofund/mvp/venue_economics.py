"""Whether AutoFund's product thesis is economically feasible under any realistic retail venue.

This module answers a question the previous milestones could not, because they had not yet
established the thing it needs: a signal whose predictive content is measured. MVP 0.2.6 found
one. It exists, it survived a clean validation on unseen data, and it is far too small to pay
for itself at this account's costs. That converts the open question from *"is there information?"*
into *"is there any execution environment in which the information could be collected?"*

**This is a decision milestone, so the module produces evidence and not a strategy.** There is no
trading capability here, no order adapter, no venue integration, and nothing that could move
money. Every function is pure arithmetic over numbers that were either measured in 0.2.6 or read
from an official venue source.

**The separation this module exists to enforce.** Predictive movement is not trade return. A
conditional mean future movement of ~2.5 bps says what the signal knows, not what a trade would
earn: capturing it requires being filled at the price it was measured from, and every real
execution pays fees, spread, slippage and latency that the measurement never saw. Confusing the
two is the single most tempting error in this area, so the module keeps them in separate fields
and never lets one be reported as the other.

**Failure is a legitimate and expected output.** The spec's own instruction is not to force a
positive result, and the honest answer for a 2.5 bps signal against a 173 bps reality is likely to
be that no retail venue fixes it. A model that could only conclude "viable" would be useless, so
`THESIS_CLASSIFICATIONS` includes three negative outcomes and the classifier can reach each of
them on the evidence.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ONE, ZERO, financial

from .execution_model import BPS

VENUE_VERSION = "autofund.venue-economics.v1"

# ---- accessibility and fee provenance ----
# The spec requires that an external venue claim be traceable to current official documentation,
# and that an account-specific rate which cannot be seen without authenticating be marked as
# unknown rather than guessed. These three values are the whole vocabulary for that, and
# `UNKNOWN` is deliberately the value that propagates into every downstream calculation.
VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
ACCOUNT_RATE_UNKNOWN = "ACCOUNT_RATE_UNKNOWN"
PUBLISHED_BASELINE = "PUBLISHED_BASELINE"
ACCOUNT_CONFIRMED = "ACCOUNT_CONFIRMED"

# ---- execution modes, reusing the project's existing vocabulary ----
TAKER_TAKER = "TAKER_TAKER"
MAKER_TAKER = "MAKER_TAKER"
MAKER_MAKER = "MAKER_MAKER"
EXECUTION_MODES: tuple[str, ...] = (TAKER_TAKER, MAKER_TAKER, MAKER_MAKER)

# ---- fee-doubling conventions ----
# The project carries two, and they disagree by a small but real amount, so a caller has to say
# which one it means rather than inheriting a default that might restate a past conclusion.
#
# GEOMETRIC compounds the two legs and is fee-currency aware: the buy fee is charged in the base
# asset and the sell fee in the quote, so the round trip is `1/((1-buy)(1-sell)) - 1`. This is
# what `passive_execution.FeeFloor` computes and it is the more accurate of the two.
#
# DOUBLED is `2 * rate`. It is a simplification that ignores the compounding, and it is what
# `asymmetry_gate.friction_bps_for` uses. The historically recorded 173 bps all-in figure was
# produced with this convention, so reproducing that figure requires selecting it explicitly.
FEE_CONVENTION_GEOMETRIC = "GEOMETRIC_COMPOUNDED"
FEE_CONVENTION_DOUBLED = "DOUBLED_RATE"
FEE_CONVENTIONS: tuple[str, ...] = (FEE_CONVENTION_GEOMETRIC, FEE_CONVENTION_DOUBLED)

# ---- capture scenarios (spec section 3) ----
# What fraction of the measured conditional movement a real execution might collect. Declared as a
# descending ladder because the whole point is to show how quickly the answer turns negative, and
# because 100% is not a straw man: it is the bound that shows the ceiling is unreachable even with
# a perfect fill.
CAPTURE_SCENARIOS: tuple[tuple[str, Decimal], ...] = (
    ("100%", ONE),
    ("75%", Decimal("0.75")),
    ("50%", Decimal("0.50")),
    ("25%", Decimal("0.25")),
)

# ---- feasibility verdicts (spec section 9) ----
CLEARLY_NOT_ECONOMIC = "CLEARLY_NOT_ECONOMIC"
THEORETICALLY_POSSIBLE_ONLY_UNDER_PASSIVE_EXECUTION = (
    "THEORETICALLY_POSSIBLE_ONLY_UNDER_PASSIVE_EXECUTION")
POTENTIALLY_ECONOMIC = "POTENTIALLY_ECONOMIC"
INSUFFICIENT_COST_EVIDENCE = "INSUFFICIENT_COST_EVIDENCE"

# ---- early-fail classification (spec section 10) ----
STRUCTURALLY_UNTRADEABLE_FOR_THIS_ALPHA = "STRUCTURALLY_UNTRADEABLE_FOR_THIS_ALPHA"

# ---- minimum-order compatibility (spec section 11) ----
COMPATIBLE_WITH_CURRENT_EXPERIMENT = "COMPATIBLE_WITH_CURRENT_EXPERIMENT"
NOT_COMPATIBLE_WITH_CURRENT_EXPERIMENT = "NOT_COMPATIBLE_WITH_CURRENT_EXPERIMENT"
COMPATIBILITY_UNKNOWN = "COMPATIBILITY_UNKNOWN"

# ---- engineering migration effort (spec section 20) ----
EFFORT_LOW = "LOW"
EFFORT_MEDIUM = "MEDIUM"
EFFORT_HIGH = "HIGH"

# ---- product thesis classification (spec section 17) ----
CURRENT_VENUE_VIABLE = "CURRENT_VENUE_VIABLE"
LOWER_COST_VENUE_REQUIRED = "LOWER_COST_VENUE_REQUIRED"
NEW_ALPHA_SOURCE_REQUIRED = "NEW_ALPHA_SOURCE_REQUIRED"
MICROSTRUCTURE_EVIDENCE_PENDING = "MICROSTRUCTURE_EVIDENCE_PENDING"
ACTIVE_TRADING_THESIS_NOT_SUPPORTED = "ACTIVE_TRADING_THESIS_NOT_SUPPORTED"

THESIS_CLASSIFICATIONS: tuple[str, ...] = (
    CURRENT_VENUE_VIABLE,
    LOWER_COST_VENUE_REQUIRED,
    NEW_ALPHA_SOURCE_REQUIRED,
    MICROSTRUCTURE_EVIDENCE_PENDING,
    ACTIVE_TRADING_THESIS_NOT_SUPPORTED,
)

# The experiment envelope, restated here so a venue minimum can be tested against it without the
# caller having to remember the numbers. Deliberately duplicated rather than imported from the
# orchestrator: this module is research and must not gain a dependency on the trading state
# machine, and a test pins the two to the same values.
AUTHORIZED_CAPITAL_MXN = Decimal("50")
MAX_DEPLOYMENT_MXN = Decimal("25")
MAX_SINGLE_ORDER_MXN = Decimal("11")


class VenueError(ValueError):
    """A venue fact is malformed or inconsistent with itself."""


@dataclass(frozen=True, slots=True)
class CostCeiling:
    """The maximum all-in round-trip friction compatible with capturing a share of a signal.

    The spec's section 3 asks for this in the form "if X% captured, all-in cost must be below Y".
    The arithmetic is a subtraction and its simplicity is the point: the signal offers a fixed
    number of basis points, so every basis point of cost spends the same budget, and any buffer
    the project wants must come out of that same budget before the comparison is made.

    `buffer_bps` is not padding. A measured conditional movement is an average over a bucket, so
    realising it requires the mean to hold on the specific trade taken; the buffer is the portion
    of the movement deliberately withheld from the cost budget so that a trade which merely
    matches the average does not fail. It defaults to zero because the project has no measured
    distribution for this signal, and inventing one would be exactly the kind of favourable
    assumption the spec forbids.
    """

    label: str
    capture_fraction: Decimal
    captured_bps: Decimal
    buffer_bps: Decimal

    @property
    def maximum_all_in_friction_bps(self) -> Decimal:
        """The cost budget: what is left of the captured movement once the buffer is withheld."""
        return self.captured_bps - self.buffer_bps

    def public(self) -> dict[str, str]:
        return {"label": self.label,
                "capture_fraction": str(self.capture_fraction),
                "captured_bps": str(self.captured_bps),
                "buffer_bps": str(self.buffer_bps),
                "maximum_all_in_friction_bps": str(self.maximum_all_in_friction_bps)}


@financial
def cost_ceiling(*, movement_bps: Decimal, capture_fraction: Decimal,
                 buffer_bps: Decimal = ZERO, label: str = "") -> CostCeiling:
    """The all-in friction a venue may charge and still leave this capture economically intact.

    Returns a ceiling, never a permission. Reaching this number is necessary for a trade to break
    even and is not sufficient for one to be worth taking: the project's own economic guard is
    what decides that, and this module cannot influence it.
    """
    if capture_fraction <= ZERO or capture_fraction > ONE:
        raise VenueError(f"capture_fraction must be in (0, 1], got {capture_fraction}")
    if movement_bps <= ZERO:
        raise VenueError("movement_bps must be positive; a null signal has no ceiling")
    if buffer_bps < ZERO:
        raise VenueError("buffer_bps cannot be negative")
    captured = movement_bps * capture_fraction
    return CostCeiling(label=label or f"{capture_fraction * Decimal('100')}%",
                       capture_fraction=capture_fraction, captured_bps=captured,
                       buffer_bps=buffer_bps)


def capture_ladder(*, movement_bps: Decimal, buffer_bps: Decimal = ZERO
                   ) -> tuple[CostCeiling, ...]:
    """The full predeclared capture ladder, in descending order of capture."""
    return tuple(cost_ceiling(movement_bps=movement_bps, capture_fraction=fraction,
                              buffer_bps=buffer_bps, label=label)
                 for label, fraction in CAPTURE_SCENARIOS)


@financial
def fee_only_round_trip_bps(*, buy_fee_rate: Decimal, sell_fee_rate: Decimal) -> Decimal:
    """Fee-only round-trip cost, reproducing the project's established fee-currency semantics.

    The BUY fee is charged in the BASE asset and the SELL fee in the QUOTE asset, so the round
    trip is `1 / ((1 - buy) * (1 - sell)) - 1` and NOT the sum of the two rates. Summing them
    would overstate a small fee pair and understate the value of removing one leg. This is the
    same formula as `passive_execution.FeeFloor.fee_only_round_trip_bps`, restated here so the
    venue model is self-contained; a test pins the two to agreement.

    Spread is deliberately excluded. It is charged once per round trip, by a single leg, and
    folding it in here would let the same basis points be counted twice once a caller adds spread.
    """
    if not (ZERO <= buy_fee_rate < ONE) or not (ZERO <= sell_fee_rate < ONE):
        raise VenueError("fee rates must be in [0, 1)")
    denominator = (ONE - buy_fee_rate) * (ONE - sell_fee_rate)
    if denominator <= ZERO:
        raise VenueError("fee rates leave nothing of the notional")
    return (ONE / denominator - ONE) * BPS


@financial
def all_in_friction_bps(*, buy_fee_rate: Decimal, sell_fee_rate: Decimal,
                        spread_bps: Decimal, slippage_bps: Decimal,
                        fee_convention: str = FEE_CONVENTION_GEOMETRIC) -> Decimal:
    """Fees plus the spread crossed once plus expected slippage.

    Spread is charged ONCE because only one leg crosses it: the entry pays the ask and the exit
    receives the bid. Charging it on both legs would double-count it, which is the specific error
    a test in this module exists to prevent.

    **Why a fee convention is a parameter.** The project contains two fee-doubling conventions and
    they are not the same number. `passive_execution.FeeFloor` compounds the two legs properly
    (`1/((1-b)(1-s)) - 1`, fee-currency aware) and yields 157.84 bps at this account's rates.
    `asymmetry_gate.friction_bps_for` simply doubles the rate (`2 * taker`) and yields 156 bps.
    The difference is 1.84 bps, and it arises because the geometric form accounts for the buy fee
    being charged in the base asset while the sell fee is charged in the quote asset.

    The historically recorded all-in figure of 173 bps uses the DOUBLED convention, so reproducing
    it requires that convention; the established fee-only floor of 157.84 uses the GEOMETRIC one.
    Silently choosing either would mean restating a historical conclusion differently, which the
    spec forbids, so both are available, the default is the more accurate geometric form, and the
    certificate reports both and accounts for the difference rather than hiding it.
    """
    if spread_bps < ZERO:
        raise VenueError("spread_bps cannot be negative")
    if slippage_bps < ZERO:
        raise VenueError("slippage_bps cannot be negative")
    if fee_convention == FEE_CONVENTION_GEOMETRIC:
        fees = fee_only_round_trip_bps(buy_fee_rate=buy_fee_rate, sell_fee_rate=sell_fee_rate)
    elif fee_convention == FEE_CONVENTION_DOUBLED:
        fees = sell_fee_rate * BPS * Decimal("2")
    else:
        raise VenueError(f"unknown fee convention: {fee_convention!r}")
    return fees + spread_bps + slippage_bps


@financial
def fee_rate_from_percent(percent: Decimal) -> Decimal:
    """Convert a published percentage (0.78 meaning 0.78%) into a rate (0.0078).

    A published venue schedule states percentages and the model works in rates, so the conversion
    happens in exactly one place. Getting it wrong by a factor of a hundred is the kind of error
    that would make every venue look either impossibly cheap or impossibly expensive, so it is
    isolated and unit-tested rather than inlined.
    """
    if percent < ZERO:
        raise VenueError("a fee percentage cannot be negative")
    return percent / Decimal("100")


@financial
def percent_from_fee_rate(rate: Decimal) -> Decimal:
    """The inverse conversion, for reporting a rate back in the units a venue publishes."""
    return rate * Decimal("100")


@financial
def spread_bps_from_prices(*, bid: Decimal, ask: Decimal) -> Decimal:
    """The quoted spread in bps, measured against the mid.

    Mid rather than the bid or the ask, because the round trip pays half of it on each side in
    expectation and the mid is the only reference that is symmetric between a buyer and a seller.
    """
    if bid <= ZERO or ask <= ZERO:
        raise VenueError("prices must be positive")
    if ask < bid:
        raise VenueError(f"ask {ask} is below bid {bid}; the book is crossed")
    mid = (bid + ask) / 2
    return (ask - bid) / mid * BPS


@dataclass(frozen=True, slots=True)
class VenueFeeSchedule:
    """A venue's fee rates, tagged with where they came from and whether they are knowable.

    `basis` is the field that keeps this honest. A published baseline available to any new account
    can be compared against another; a rate that is only visible after authenticating cannot, and
    must be reported as unknown rather than filled in from a remembered figure. The spec is
    explicit that credentials must not be requested to resolve it, so `KNOWN` is not achievable
    for such a venue in this milestone and the model has to carry that gap rather than close it.
    """

    venue: str
    maker_rate: Decimal | None
    taker_rate: Decimal | None
    basis: str
    source: str
    retrieved_at: str
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.venue:
            raise VenueError("venue is required")
        if self.basis not in (PUBLISHED_BASELINE, ACCOUNT_CONFIRMED, ACCOUNT_RATE_UNKNOWN):
            raise VenueError(f"unknown fee basis: {self.basis}")
        # An unknown basis and known rates together would mean the gap was filled by guessing.
        if self.basis == ACCOUNT_RATE_UNKNOWN and self.maker_rate is not None:
            raise VenueError("a venue whose rates are unknown cannot carry a maker rate")
        if self.basis == ACCOUNT_RATE_UNKNOWN and self.taker_rate is not None:
            raise VenueError("a venue whose rates are unknown cannot carry a taker rate")
        if self.basis != ACCOUNT_RATE_UNKNOWN and (self.maker_rate is None
                                                   or self.taker_rate is None):
            raise VenueError(f"{self.venue}: a known basis requires both maker and taker rates")
        for rate in (self.maker_rate, self.taker_rate):
            if rate is not None and not (ZERO <= rate < ONE):
                raise VenueError(f"{self.venue}: fee rate {rate} is outside [0, 1)")

    @property
    def rates_known(self) -> bool:
        return self.maker_rate is not None and self.taker_rate is not None

    def rates_for(self, mode: str) -> tuple[Decimal, Decimal] | None:
        """The (buy, sell) rates for an execution mode, or None when they cannot be known.

        The narrowing below is a real runtime check rather than an `assert`, even though
        `rates_known` has already been tested. An `assert` is removed under `-O`, and if it were
        the only guard then an optimised interpreter could reach the arithmetic with a None fee
        and produce either an exception far from the cause or, worse, a silently wrong cost.
        """
        if not self.rates_known:
            return None
        if mode not in EXECUTION_MODES:
            raise VenueError(f"unknown execution mode: {mode}")
        maker = self.maker_rate
        taker = self.taker_rate
        if maker is None or taker is None:  # pragma: no cover - unreachable while validated
            raise VenueError(f"{self.venue}: execution mode {mode} needs both fee rates")
        if mode == MAKER_TAKER:
            return (maker, taker)
        if mode == MAKER_MAKER:
            return (maker, maker)
        return (taker, taker)

    def public(self) -> dict[str, Any]:
        return {"venue": self.venue,
                "maker_rate": None if self.maker_rate is None else str(self.maker_rate),
                "taker_rate": None if self.taker_rate is None else str(self.taker_rate),
                "basis": self.basis, "source": self.source,
                "retrieved_at": self.retrieved_at, "rates_known": self.rates_known,
                "notes": self.notes}


@dataclass(frozen=True, slots=True)
class VenueProfile:
    """One candidate venue's feasibility contract (spec section 4).

    Every field the spec asks for is present, and the ones that could not be established from
    public sources are set to `None` or to an explicit UNKNOWN rather than to a plausible value.
    That is the whole discipline of this milestone: a missing fact must stay missing, because a
    model that quietly fills its own gaps produces a comparison nobody can rely on.
    """

    venue: str
    accessibility: str
    spot_api: bool | None
    public_market_data_api: bool | None
    authenticated_trading_api: bool | None
    supports_market_orders: bool | None
    supports_limit: bool | None
    supports_post_only: bool | None
    supports_client_order_id: bool | None
    order_status_api: bool | None
    fills_api: bool | None
    open_orders_api: bool | None
    cancel_api: bool | None
    websocket_support: bool | None
    order_book_api: bool | None
    trade_tape_api: bool | None
    minimum_order_quote: Decimal | None
    minimum_order_currency: str | None
    mxn_quote_pairs: bool | None
    mxn_pair_symbols: tuple[str, ...] = ()
    fee_tier_requirements: str = ""
    fee_currency_semantics: str = ""
    rate_limits: str = ""
    liquidity_evidence: str = ""
    recovery_feasibility: str = ""
    migration_effort: str = EFFORT_HIGH
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.venue:
            raise VenueError("venue is required")
        if self.accessibility not in (VERIFIED, UNVERIFIED):
            raise VenueError(f"{self.venue}: accessibility must be {VERIFIED} or {UNVERIFIED}")
        if self.migration_effort not in (EFFORT_LOW, EFFORT_MEDIUM, EFFORT_HIGH):
            raise VenueError(f"{self.venue}: unknown migration effort {self.migration_effort}")
        if self.minimum_order_quote is not None and self.minimum_order_quote < ZERO:
            raise VenueError(f"{self.venue}: minimum order cannot be negative")
        if self.minimum_order_quote is not None and not self.minimum_order_currency:
            raise VenueError(f"{self.venue}: a minimum order must state its currency")

    def minimum_order_compatibility(
        self, *, order_mxn: Decimal = MAX_SINGLE_ORDER_MXN,
        quote_to_mxn: Decimal | None = None,
    ) -> str:
        """Whether the single-order envelope can satisfy this venue's minimum (spec section 11).

        Two ways to be incompatible, and both are reported rather than assumed away. A venue with
        no MXN quote requires a conversion to price the trade at all, so without an FX rate the
        answer is UNKNOWN rather than a guess. A venue whose minimum exceeds the envelope is
        incompatible, and the spec forbids raising capital to fix that.
        """
        if self.minimum_order_quote is None or self.minimum_order_currency is None:
            return COMPATIBILITY_UNKNOWN
        currency = self.minimum_order_currency.upper()
        if currency == "MXN":
            return (COMPATIBLE_WITH_CURRENT_EXPERIMENT
                    if self.minimum_order_quote <= order_mxn
                    else NOT_COMPATIBLE_WITH_CURRENT_EXPERIMENT)
        # A non-MXN minimum cannot be compared to an MXN envelope without a conversion rate.
        # Returning UNKNOWN rather than assuming parity keeps the gap visible.
        if quote_to_mxn is None:
            return COMPATIBILITY_UNKNOWN
        if quote_to_mxn <= ZERO:
            raise VenueError("quote_to_mxn must be positive")
        minimum_mxn = self.minimum_order_quote * quote_to_mxn
        return (COMPATIBLE_WITH_CURRENT_EXPERIMENT
                if minimum_mxn <= order_mxn
                else NOT_COMPATIBLE_WITH_CURRENT_EXPERIMENT)

    def public(self) -> dict[str, Any]:
        return {
            "venue": self.venue, "accessibility": self.accessibility,
            "spot_api": self.spot_api, "public_market_data_api": self.public_market_data_api,
            "authenticated_trading_api": self.authenticated_trading_api,
            "supports_market_orders": self.supports_market_orders,
            "supports_limit": self.supports_limit,
            "supports_post_only": self.supports_post_only,
            "supports_client_order_id": self.supports_client_order_id,
            "order_status_api": self.order_status_api, "fills_api": self.fills_api,
            "open_orders_api": self.open_orders_api, "cancel_api": self.cancel_api,
            "websocket_support": self.websocket_support,
            "order_book_api": self.order_book_api, "trade_tape_api": self.trade_tape_api,
            "minimum_order_quote": (None if self.minimum_order_quote is None
                                    else str(self.minimum_order_quote)),
            "minimum_order_currency": self.minimum_order_currency,
            "mxn_quote_pairs": self.mxn_quote_pairs,
            "mxn_pair_symbols": list(self.mxn_pair_symbols),
            "fee_tier_requirements": self.fee_tier_requirements,
            "fee_currency_semantics": self.fee_currency_semantics,
            "rate_limits": self.rate_limits, "liquidity_evidence": self.liquidity_evidence,
            "recovery_feasibility": self.recovery_feasibility,
            "migration_effort": self.migration_effort, "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class VenueEconomics:
    """One venue's cost picture for one execution mode, plus whether it could clear the alpha.

    The two-stage structure is the spec's section 10 early-fail: the fee-only floor is computed
    first and, if it already exceeds the largest plausible capture, the verdict is settled without
    modelling spread or slippage at all. Over-engineering a slippage estimate for a venue that
    cannot pass on fees alone would add false precision to a conclusion that is already decided.
    """

    venue: str
    mode: str
    fee_only_bps: Decimal | None
    spread_bps: Decimal | None
    slippage_bps: Decimal | None
    all_in_bps: Decimal | None
    fee_evidence: str
    spread_evidence: str
    notes: str = ""

    @property
    def costs_known(self) -> bool:
        return self.all_in_bps is not None

    def clears(self, ceiling: CostCeiling) -> bool | None:
        """Whether this venue's all-in cost fits inside a capture scenario's budget.

        None when the cost is not knowable, which is the whole point of carrying the unknown
        through rather than defaulting it to zero.
        """
        if self.all_in_bps is None:
            return None
        return self.all_in_bps <= ceiling.maximum_all_in_friction_bps

    def public(self) -> dict[str, Any]:
        return {"venue": self.venue, "mode": self.mode,
                "fee_only_bps": None if self.fee_only_bps is None else str(self.fee_only_bps),
                "spread_bps": None if self.spread_bps is None else str(self.spread_bps),
                "slippage_bps": None if self.slippage_bps is None else str(self.slippage_bps),
                "all_in_bps": None if self.all_in_bps is None else str(self.all_in_bps),
                "fee_evidence": self.fee_evidence, "spread_evidence": self.spread_evidence,
                "costs_known": self.costs_known, "notes": self.notes}


@financial
def venue_economics(*, schedule: VenueFeeSchedule, mode: str,
                    spread_bps: Decimal | None, slippage_bps: Decimal | None
                    ) -> VenueEconomics:
    """Price one execution mode at one venue, or record why it cannot be priced.

    When the venue's rates are unknown every cost field is None and the evidence fields say so.
    That propagates into an INSUFFICIENT_COST_EVIDENCE verdict rather than into a favourable one,
    which is the property the spec's section 25 asks to be tested.
    """
    if mode not in EXECUTION_MODES:
        raise VenueError(f"unknown execution mode: {mode}")
    rates = schedule.rates_for(mode)
    if rates is None:
        return VenueEconomics(
            venue=schedule.venue, mode=mode, fee_only_bps=None, spread_bps=None,
            slippage_bps=None, all_in_bps=None, fee_evidence=ACCOUNT_RATE_UNKNOWN,
            spread_evidence="NOT_EVALUATED",
            notes=(f"{schedule.venue} fees are only visible after authenticating; the spec "
                   "forbids requesting credentials in this milestone"))
    buy, sell = rates
    fee_only = fee_only_round_trip_bps(buy_fee_rate=buy, sell_fee_rate=sell)
    if spread_bps is None or slippage_bps is None:
        return VenueEconomics(
            venue=schedule.venue, mode=mode, fee_only_bps=fee_only, spread_bps=spread_bps,
            slippage_bps=slippage_bps, all_in_bps=None,
            fee_evidence=schedule.basis,
            spread_evidence="MISSING" if spread_bps is None else "PRESENT",
            notes="fees are known but a required execution cost is not, so no all-in total exists")
    return VenueEconomics(
        venue=schedule.venue, mode=mode, fee_only_bps=fee_only, spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        all_in_bps=all_in_friction_bps(buy_fee_rate=buy, sell_fee_rate=sell,
                                       spread_bps=spread_bps, slippage_bps=slippage_bps),
        fee_evidence=schedule.basis,
        spread_evidence="OBSERVED_PUBLIC_TICKER" if spread_bps > ZERO else "ZERO_ASSUMED",
        notes=schedule.notes)


def structurally_untradeable(*, economics: VenueEconomics, ceiling: CostCeiling) -> bool:
    """Whether the venue fails on fees alone, before any execution modelling (spec section 10).

    True only when the fee floor is known and already exceeds the budget. An unknown fee floor
    returns False, because "we do not know" is not the same as "we know it fails" and must be
    reported as the missing evidence it is.
    """
    if economics.fee_only_bps is None:
        return False
    return economics.fee_only_bps > ceiling.maximum_all_in_friction_bps


def feasibility_verdict(*, economics: VenueEconomics, ladder: tuple[CostCeiling, ...]
                        ) -> str:
    """Classify whether a venue could plausibly collect this alpha (spec section 9).

    The order of the checks is the spec's section 10 early-fail and it matters. The fee floor is
    tested FIRST, before asking whether the spread and slippage are known, because a venue whose
    fees alone exceed the best possible capture cannot be rescued by any estimate of the other
    costs: spread and slippage are non-negative, so they can only make a failing venue worse.

    An earlier version of this function checked completeness first and returned
    INSUFFICIENT_COST_EVIDENCE for a venue whose known fee floor was already eight times the
    ceiling. That was a hedge rather than a conclusion, and it was wrong: "we have not measured
    the spread" is not a reason to withhold a verdict that fees alone have already settled. The
    remaining cases keep the distinction the spec asks for, since a venue that fails on the
    complete cost but not on fees is a different finding from one that fails structurally.
    """
    if not ladder:
        raise VenueError("a capture ladder is required")
    best_case = ladder[0]  # 100% capture: the most generous assumption available

    # Stage one: fees alone. A known fee floor above the ceiling settles the question.
    if economics.fee_only_bps is not None:
        if economics.fee_only_bps > best_case.maximum_all_in_friction_bps:
            return STRUCTURALLY_UNTRADEABLE_FOR_THIS_ALPHA
    elif not economics.costs_known:
        # Neither the fee floor nor the all-in total is knowable, so nothing can be concluded.
        return INSUFFICIENT_COST_EVIDENCE

    # Stage two: the complete cost, when it is knowable.
    if not economics.costs_known:
        # Fees fit the budget but a required execution cost is missing, so whether the venue
        # clears is genuinely undetermined rather than settled either way.
        return INSUFFICIENT_COST_EVIDENCE

    clears_best = economics.clears(best_case)
    if clears_best is None:
        return INSUFFICIENT_COST_EVIDENCE
    if clears_best:
        # Even a full capture fits, which for a realistic all-in cost means the venue is at least
        # arithmetically possible. Still not a permission: the economic guard decides.
        return POTENTIALLY_ECONOMIC
    return CLEARLY_NOT_ECONOMIC


@dataclass(frozen=True, slots=True)
class VenueAssessment:
    """A venue's complete evaluation: its facts, its costed modes, and its verdicts."""

    profile: VenueProfile
    schedule: VenueFeeSchedule
    economics: tuple[VenueEconomics, ...]
    verdicts: dict[str, str]
    minimum_compatibility: str
    reference_data_feasible: bool | None
    reference_data_notes: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def economics_for(self, mode: str) -> VenueEconomics | None:
        for item in self.economics:
            if item.mode == mode:
                return item
        return None

    def public(self) -> dict[str, Any]:
        return {
            "venue": self.profile.venue,
            "profile": self.profile.public(),
            "fee_schedule": self.schedule.public(),
            "economics": [item.public() for item in self.economics],
            "verdicts": self.verdicts,
            "minimum_order_compatibility": self.minimum_compatibility,
            "reference_data_feasible": self.reference_data_feasible,
            "reference_data_notes": self.reference_data_notes,
            "migration_effort": self.profile.migration_effort,
            **self.detail,
        }


def assess_venue(*, profile: VenueProfile, schedule: VenueFeeSchedule,
                 ladder: tuple[CostCeiling, ...],
                 spread_bps: Decimal | None = None,
                 slippage_bps: Decimal | None = None,
                 spread_evidence: str = "UNAVAILABLE"
                 ) -> VenueAssessment:
    """Cost every supported mode at one venue and classify each against the ladder.

    A mode the venue does not support is not costed at all. Pricing a maker leg a venue has no
    post-only order type for would be inventing a scenario, which is the failure this milestone is
    most exposed to because every maker scenario is cheaper than the taker one.
    """
    modes: list[str] = []
    if profile.supports_market_orders is not False:
        modes.append(TAKER_TAKER)
    if profile.supports_limit is not False and profile.supports_post_only is True:
        modes.append(MAKER_MAKER)
        modes.append(MAKER_TAKER)
    modes = [mode for mode in EXECUTION_MODES if mode in modes]

    economics = tuple(
        venue_economics(schedule=schedule, mode=mode, spread_bps=spread_bps,
                        slippage_bps=slippage_bps)
        for mode in modes)
    verdicts = {item.mode: feasibility_verdict(economics=item, ladder=ladder)
                for item in economics}
    return VenueAssessment(
        profile=profile, schedule=schedule, economics=economics, verdicts=verdicts,
        minimum_compatibility=profile.minimum_order_compatibility(),
        reference_data_feasible=_reference_data_feasible(profile=profile),
        reference_data_notes=_reference_data_notes(profile=profile),
        detail={"spread_evidence": spread_evidence})


def _reference_data_feasible(*, profile: VenueProfile) -> bool | None:
    """Whether this venue's public data could support future cross-venue research (section 14).

    Deliberately separate from whether the venue is suitable for execution. A venue may be
    unusable for trading and still be the most useful reference price available, and the spec's
    section 13 asks for exactly that possibility to be assessed independently.
    """
    signals = (profile.public_market_data_api, profile.order_book_api, profile.trade_tape_api)
    if any(value is None for value in signals):
        return None
    return bool(profile.public_market_data_api and profile.order_book_api)


def _reference_data_notes(*, profile: VenueProfile) -> str:
    if profile.public_market_data_api is None:
        return "public data availability could not be established from official sources"
    parts = []
    if profile.order_book_api:
        parts.append("order book reachable without authentication")
    if profile.trade_tape_api:
        parts.append("trade tape reachable without authentication")
    if profile.websocket_support:
        parts.append("websocket streaming available")
    return "; ".join(parts) or "no public market-data surface identified"


@financial
def required_friction_bps(*, movement_bps: Decimal, capture_fraction: Decimal = ONE
                          ) -> Decimal:
    """The headline answer to the spec's section 23 question.

    "What all-in round-trip friction would AutoFund require to monetize its validated alpha?"
    Expressed as a ceiling, so a smaller number is a harder requirement. The function exists
    mainly so the report cannot state the number in a way the model would not produce.
    """
    return cost_ceiling(movement_bps=movement_bps,
                        capture_fraction=capture_fraction).maximum_all_in_friction_bps


def retail_environment_floor_bps() -> Decimal:
    """The cheapest all-in retail friction established in this milestone, from verified sources.

    Binance publishes 0.100% maker and taker for a Regular User, which is the lowest account-
    accessible rate found, and it is a taker rate that requires no volume tier and no passive fill.
    Used as the comparison floor for the hard question in section 23: not the best venue found,
    but the best *published baseline rate* found, on an MXN pair.

    Uses the geometric convention so it is comparable with the project's established fee-only
    floors, which are computed the same way.

    Returned as a constant rather than recomputed so a caller has to cite it deliberately.
    """
    binance_regular = Decimal("0.0010")
    return fee_only_round_trip_bps(buy_fee_rate=binance_regular,
                                   sell_fee_rate=binance_regular)


@financial
def historical_friction_bps(*, taker_rate: Decimal = Decimal("0.0078"),
                            spread_bps: Decimal = Decimal("12"),
                            slippage_bps: Decimal = Decimal("5")) -> Decimal:
    """Reproduce the ~173 bps all-in friction the earlier milestones compared against.

    Provided so the recorded historical conclusion can be regenerated exactly rather than quoted
    from memory (spec section 7). Uses the doubled convention because that is the convention the
    historical figure was produced with: `2 * 0.0078 * 10000 + 12 + 5 = 173`.

    Kept as an explicit function rather than a constant so the difference between the two fee
    conventions is visible at the point of use, and so a test can assert the reconstruction.
    """
    return all_in_friction_bps(buy_fee_rate=taker_rate, sell_fee_rate=taker_rate,
                               spread_bps=spread_bps, slippage_bps=slippage_bps,
                               fee_convention=FEE_CONVENTION_DOUBLED)


def classify_thesis(*, assessed: tuple[VenueAssessment, ...],
                    movement_bps: Decimal,
                    microstructure_ready: bool = False,
                    notes: tuple[str, ...] = ()) -> dict[str, Any]:
    """Decide which product-thesis path the evidence supports (spec section 17).

    The ordering encodes the reasoning rather than a preference. If any venue is at least
    arithmetically possible, the thesis could be viable there. If none is, the question becomes
    whether the obstacle is the venue or the alpha itself: a signal whose captured movement is
    smaller than the *cheapest verified retail fee floor* cannot be rescued by moving venue, and
    that is a different finding from "this venue is too expensive".

    Microstructure is only allowed to be the dominant classification when the evidence is
    otherwise negative *and* more capture is genuinely the next step, so it cannot be used to
    defer a conclusion the cost arithmetic has already reached.
    """
    ladder = capture_ladder(movement_bps=movement_bps)
    best_ceiling = ladder[0].maximum_all_in_friction_bps

    possible = [item for item in assessed
                if any(verdict == POTENTIALLY_ECONOMIC
                       for verdict in item.verdicts.values())]

    floor = retail_environment_floor_bps()
    # The comparison that decides between the two negative paths: can the most generous capture
    # clear even the cheapest verified retail fee floor? If not, no venue change helps.
    alpha_clears_cheapest_floor = best_ceiling >= floor

    if possible:
        dominant = CURRENT_VENUE_VIABLE if any(
            item.profile.venue.lower() == "bitso" for item in possible
        ) else LOWER_COST_VENUE_REQUIRED
    elif alpha_clears_cheapest_floor:
        # The alpha could pay *some* environment, but nothing evaluated reaches it. That is a
        # venue-access problem, not an alpha problem.
        dominant = LOWER_COST_VENUE_REQUIRED
    else:
        dominant = NEW_ALPHA_SOURCE_REQUIRED

    coexisting: list[str] = []
    if dominant == NEW_ALPHA_SOURCE_REQUIRED and microstructure_ready is False:
        # Recorded as a coexisting finding, never as the dominant one, so a pending
        # measurement cannot be used to avoid the conclusion the costs already support.
        coexisting.append(MICROSTRUCTURE_EVIDENCE_PENDING)
    if dominant == NEW_ALPHA_SOURCE_REQUIRED and not possible:
        coexisting.append(ACTIVE_TRADING_THESIS_NOT_SUPPORTED)

    return {
        "dominant": dominant,
        "coexisting": coexisting,
        "cheapest_verified_retail_floor_bps": str(floor),
        "best_capture_ceiling_bps": str(best_ceiling),
        "alpha_clears_cheapest_verified_retail_floor": alpha_clears_cheapest_floor,
        "venues_potentially_economic": [item.profile.venue for item in possible],
        "shortfall_multiple": str(floor / best_ceiling) if best_ceiling > ZERO else "INFINITE",
        "notes": list(notes),
        "reason": _classify_reason(dominant=dominant, floor=floor, best_ceiling=best_ceiling,
                                   possible=possible),
    }


def _classify_reason(*, dominant: str, floor: Decimal, best_ceiling: Decimal,
                     possible: list[VenueAssessment]) -> str:
    if dominant == CURRENT_VENUE_VIABLE:
        return "the current venue can at least arithmetically clear the alpha at full capture"
    if dominant == LOWER_COST_VENUE_REQUIRED:
        if possible:
            return ("a non-Bitso venue could clear the alpha, so the obstacle is venue economics "
                    "rather than the signal")
        return ("the alpha could pay a cheaper fee structure than anything available at this "
                "account size, but no evaluated venue reaches it")
    if dominant == NEW_ALPHA_SOURCE_REQUIRED:
        return (f"even capturing 100 percent of the validated movement leaves "
                f"{best_ceiling:.2f} bps of budget against a cheapest verified retail fee floor "
                f"of {floor:.2f} bps, so no retail venue change can monetize this signal")
    if dominant == MICROSTRUCTURE_EVIDENCE_PENDING:
        return "the strongest unresolved hypothesis needs more forward capture"
    return "the evidence does not support autonomous active trading under these assumptions"


def freeze_validated_alpha(*, candidate: Any, validation: dict[str, Any],
                           retrieved_at: datetime | None = None) -> dict[str, Any]:
    """Freeze the 0.2.6 candidate as VALIDATED_INFORMATION_SIGNAL_V1 (spec section 1).

    The freeze records what was measured, by which code, against which data, and it explicitly
    forbids the thing the spec calls out: retuning the signal because a venue turned out to be
    expensive. The venue economics are computed *against* this frozen signal and may not feed
    back into it, which is why the record carries its own disclaimer field rather than relying on
    a reader to remember.

    The fingerprint is taken from the candidate manifest rather than recomputed from a copy, so
    the frozen identity is the same object the 0.2.6 experiment produced.
    """
    moment = retrieved_at or datetime.now(UTC)
    return {
        "label": VALIDATED_INFORMATION_SIGNAL_V1,
        "frozen_at": moment.isoformat(),
        "fingerprint": candidate.fingerprint,
        "candidate_id": candidate.candidate_id,
        "source_family": candidate.source_family,
        "feature_name": candidate.feature_name,
        "leader": candidate.leader,
        "follower": candidate.follower,
        "horizon_minutes": candidate.horizon_minutes,
        "bucket_count": candidate.bucket_count,
        "development_observations": candidate.development_observations,
        "development_effective_observations": str(candidate.development_effective_observations),
        "development_rank_relationship": str(candidate.development_rank_relationship),
        "development_monotone": candidate.development_monotone,
        "development_monotone_direction": candidate.development_monotone_direction,
        "development_stable_subwindows": candidate.development_stable_subwindows,
        "validation_observations": validation.get("observations"),
        "validation_rank_relationship": validation.get("rank_relationship"),
        "validation_monotone": validation.get("monotone"),
        "validation_monotone_direction": validation.get("monotone_direction"),
        "validation_stable_subwindows": validation.get("stable_subwindows"),
        "validation_passed": validation.get("passed"),
        "movement_bps": str(_movement_from_validation(validation)),
        "movement_definition": (
            "the largest bucket's conditional mean forward movement; an estimate of what the "
            "signal knows, NOT a realisable trade return"),
        "retuned_for_venue_economics": False,
        "may_not_be_retuned_for_venue_economics": True,
        "is_a_strategy": False,
        "trading_policy_defined": False,
    }


VALIDATED_INFORMATION_SIGNAL_V1 = "VALIDATED_INFORMATION_SIGNAL_V1"

# The frozen signal's identity, pinned as constants so anything that reports it reports the same
# value. These are the 0.2.6 candidate's fingerprint (reproduced from its manifest) and the
# validated extreme-bucket movement scale. They are asserted by the certification script before
# any venue cost is computed, so venue economics can never be applied to a different signal, and
# they are exposed here rather than only in the script so the research view can state them too.
FROZEN_ALPHA_FINGERPRINT = (
    "fae4faa85ef648acb5e831cb6bc3fe242d8a1723cc3c474262a886a4600ab3aa")
FROZEN_ALPHA_MOVEMENT_BPS = Decimal("2.513590292581896613688157726")


def _movement_from_validation(validation: dict[str, Any]) -> Decimal:
    """Read the measured movement scale out of a validation record.

    Taken as the largest absolute bucket mean rather than the spread between extremes, because
    only one side of the relationship is tradeable in a long-only spot system and the usable
    magnitude is therefore the mean of the bucket that moves, not the distance between two.
    """
    buckets = validation.get("content", {}).get("buckets") or []
    if not buckets:
        return ZERO
    means = [abs(Decimal(str(bucket["mean_forward_return_bps"]))) for bucket in buckets]
    return max(means)

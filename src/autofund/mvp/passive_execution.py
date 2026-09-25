"""Passive (post-only) execution research: modes, fills, cash flows and fee floors.

MVP 0.2.2 established that no current strategy is certifiable, and that the dominant
obstacle is execution friction: at a confirmed **taker** fee of `0.0078` plus the crossed
spread, a round trip costs roughly 173 bps, so any target small enough to be plausible is
also too small to pay for itself.

This module asks a narrower and more falsifiable question: *is the friction itself the
binding constraint, or is the absence of edge?* It answers it by re-pricing the **existing
frozen strategy decisions** — unchanged — under different execution styles, so that alpha
and execution cost can be separated instead of confounded.

Three premises govern everything here, and each is deliberately pessimistic:

**A maker rate is not an entitlement.** The account-confirmed schedule shows a maker rate
below the taker rate, but nothing in this module assumes a passive order fills. A limit
order that rests unfilled saves no money and captures no move. Fill is the thing to be
proven, and it is modelled conservatively.

**A fill needs evidence from after the order existed.** An order is placed at time T; only
evidence with a timestamp strictly after T can support a fill claim. A later candle's
high/low touching the limit price is *not* evidence, because a candle records a range and
not a sequence, so it cannot show that a resting order was in front of the trades that
printed there. Treating a touch as a fill is the single most common way a backtest
manufactures passive profit out of nothing.

**Unfilled is a cost, not a neutral.** An order that does not fill has reserved capital,
missed the move the strategy identified, and paid the opportunity cost of that capital. A
passive model that reports only the P&L of its filled subset is reporting a selection
effect, not an execution improvement. Therefore every unfilled order is counted, and the
price move it missed is recorded.

Nothing here authorises a live passive order. There is no cancellation capability, no
order placement, and no exchange mutation: cancellation is simulated locally.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ONE, ZERO, financial
from autofund.replay.data import Candle

BPS = Decimal("10000")

EXECUTION_VERSION = "autofund.passive-execution.v1"

# ---------------------------------------------------------------------------
# Liquidity roles and execution modes
# ---------------------------------------------------------------------------

MAKER = "MAKER"
TAKER = "TAKER"

# The four hypotheses. Ordered from the current behaviour to the most aggressive
# departure from it, which is also the order of increasing modelling uncertainty.
TAKER_TAKER = "TAKER_TAKER"
MAKER_TAKER = "MAKER_TAKER"
TAKER_MAKER = "TAKER_MAKER"
MAKER_MAKER = "MAKER_MAKER"

ALL_MODES = (TAKER_TAKER, MAKER_TAKER, TAKER_MAKER, MAKER_MAKER)

# ---------------------------------------------------------------------------
# Passive-fill evidence quality
# ---------------------------------------------------------------------------

# Ordered weakest to strongest. `CANDLE_ONLY_UNCERTAIN` is explicitly *not* evidence of
# execution: it records that the model could not determine whether a resting order would
# have filled, and every conclusion drawn under it must be labelled as an inference.
CANDLE_ONLY_UNCERTAIN = "CANDLE_ONLY_UNCERTAIN"
TOP_OF_BOOK_INFERRED = "TOP_OF_BOOK_INFERRED"
ORDER_BOOK_SUPPORTED = "ORDER_BOOK_SUPPORTED"
TRADE_TAPE_SUPPORTED = "TRADE_TAPE_SUPPORTED"

EVIDENCE_QUALITY_LADDER = (CANDLE_ONLY_UNCERTAIN, TOP_OF_BOOK_INFERRED,
                           ORDER_BOOK_SUPPORTED, TRADE_TAPE_SUPPORTED)

# Only these two may support a Production feasibility conclusion about maker fills.
CONFIRMING_MAKER_EVIDENCE = frozenset({ORDER_BOOK_SUPPORTED, TRADE_TAPE_SUPPORTED})

# ---------------------------------------------------------------------------
# Order outcomes
# ---------------------------------------------------------------------------

# Kept distinct because "cancelled" and "filled nothing" are different facts: an order
# cancelled after a partial fill left real inventory, and conflating the two would either
# invent or destroy a position.
PREPARED = "PREPARED"
SUBMITTED = "SUBMITTED"
OPEN = "OPEN"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
FILLED = "FILLED"
CANCEL_REQUESTED = "CANCEL_REQUESTED"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"
OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"

# Rejection reasons. Each is a distinct finding, not a generic failure.
POST_ONLY_WOULD_REJECT = "POST_ONLY_WOULD_REJECT"
INSUFFICIENT_PASSIVE_EVIDENCE = "INSUFFICIENT_PASSIVE_EVIDENCE"
NO_TOUCH_AFTER_PLACEMENT = "NO_TOUCH_AFTER_PLACEMENT"
TIMED_OUT_RESTING = "TIMED_OUT_RESTING"
CROSSED_ON_PLACEMENT = "CROSSED_ON_PLACEMENT"

INVALIDATION_TICKS = Decimal("1")


class ExecutionError(ValueError):
    """The execution model was asked for something it cannot defend."""


@dataclass(frozen=True, slots=True)
class ExecutionMode:
    """One research-only execution hypothesis.

    This is an economic hypothesis about how orders would be *submitted*. It is not a
    Production authorisation, and the exchange's capability allowlist is unaffected by
    its existence.
    """

    mode: str
    entry_liquidity: str
    exit_liquidity: str
    entry_order_type: str
    exit_order_type: str
    timeout_bars: int
    requires_post_only: bool
    description: str

    def __post_init__(self) -> None:
        if self.mode not in ALL_MODES:
            raise ExecutionError(f"unknown execution mode: {self.mode}")
        for role in (self.entry_liquidity, self.exit_liquidity):
            if role not in (MAKER, TAKER):
                raise ExecutionError(f"unknown liquidity role: {role}")
        if self.timeout_bars <= 0:
            raise ExecutionError("timeout_bars must be positive")

    @property
    def indicative(self) -> bool:
        """Whether this mode depends on an unproven maker fill."""
        return MAKER in (self.entry_liquidity, self.exit_liquidity)

    def public(self) -> dict[str, Any]:
        return {"version": EXECUTION_VERSION, "mode": self.mode,
                "entry_liquidity": self.entry_liquidity,
                "exit_liquidity": self.exit_liquidity,
                "entry_order_type": self.entry_order_type,
                "exit_order_type": self.exit_order_type,
                "timeout_bars": self.timeout_bars,
                "requires_post_only": self.requires_post_only,
                "research_only": True,
                "production_authorised": False,
                "description": self.description}


def execution_modes() -> dict[str, ExecutionMode]:
    """The four predeclared hypotheses, with their full semantics stated."""
    return {
        TAKER_TAKER: ExecutionMode(
            mode=TAKER_TAKER, entry_liquidity=TAKER, exit_liquidity=TAKER,
            entry_order_type="MARKET", exit_order_type="MARKET", timeout_bars=1,
            requires_post_only=False,
            description=("Current behaviour. Both legs cross the spread and pay the taker "
                         "rate. Baseline for every comparison.")),
        MAKER_TAKER: ExecutionMode(
            mode=MAKER_TAKER, entry_liquidity=MAKER, exit_liquidity=TAKER,
            entry_order_type="LIMIT_POST_ONLY", exit_order_type="MARKET", timeout_bars=5,
            requires_post_only=True,
            description=("Enter passively at the bid, exit aggressively into the bid. "
                         "Only the entry fee is reduced, so the saving is one maker-taker "
                         "differential and the entry may not fill.")),
        TAKER_MAKER: ExecutionMode(
            mode=TAKER_MAKER, entry_liquidity=TAKER, exit_liquidity=MAKER,
            entry_order_type="MARKET", exit_order_type="LIMIT_POST_ONLY", timeout_bars=5,
            requires_post_only=True,
            description=("Enter aggressively, exit passively at the ask. Exit is the leg "
                         "most exposed to adverse selection, because a resting sell is "
                         "filled preferentially when price is falling.")),
        MAKER_MAKER: ExecutionMode(
            mode=MAKER_MAKER, entry_liquidity=MAKER, exit_liquidity=MAKER,
            entry_order_type="LIMIT_POST_ONLY", exit_order_type="LIMIT_POST_ONLY",
            timeout_bars=5, requires_post_only=True,
            description=("Both legs passive. Maximum nominal fee saving and maximum "
                         "execution risk: either leg may not fill, and both are exposed "
                         "to adverse selection.")),
    }


# ---------------------------------------------------------------------------
# Fee floors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeeFloor:
    """The gross round-trip price movement required for fees alone to break even.

    Fee-currency semantics are the real account's: the BUY fee is charged in the BASE
    asset (so it reduces the quantity that can later be sold) and the SELL fee is charged
    in the QUOTE asset (so it reduces MXN proceeds). Because the two legs are charged in
    different currencies, the round trip is `1 / ((1 - buy_fee) * (1 - sell_fee)) - 1`,
    not the sum of the two rates. Summing them would overstate the cost of a small fee
    pair and understate the value of removing one.

    Spread is deliberately excluded. It is a separate cost that only one of the two legs
    pays, and folding it in here would let the same bps be charged twice once the caller
    adds spread to the comparison.
    """

    mode: str
    buy_fee_rate: Decimal
    sell_fee_rate: Decimal

    @property
    def fee_only_round_trip_bps(self) -> Decimal:
        denominator = (ONE - self.buy_fee_rate) * (ONE - self.sell_fee_rate)
        if denominator <= ZERO:
            return ZERO
        return (ONE / denominator - ONE) * BPS

    def telemetry(self) -> dict[str, Any]:
        return {"version": EXECUTION_VERSION, "mode": self.mode,
                "buy_fee_rate": str(self.buy_fee_rate),
                "sell_fee_rate": str(self.sell_fee_rate),
                "buy_fee_liquidity": "MAKER" if self.buy_fee_rate
                else "UNKNOWN",
                "fee_only_round_trip_bps": str(self.fee_only_round_trip_bps),
                "gross_movement_required_bps": str(self.fee_only_round_trip_bps),
                "spread_included": False,
                "fee_currency_semantics": "BUY_FEE_IN_BASE;SELL_FEE_IN_QUOTE"}


@financial
def fee_floors(*, maker_rate: Decimal, taker_rate: Decimal,
               modes: tuple[str, ...] = ALL_MODES) -> dict[str, FeeFloor]:
    """Fee-only round-trip floor for each execution mode, from account-confirmed rates."""
    if maker_rate > taker_rate:
        raise ExecutionError("maker rate above taker rate")
    resolved = execution_modes()
    floors: dict[str, FeeFloor] = {}
    for mode in modes:
        definition = resolved[mode]
        floors[mode] = FeeFloor(
            mode=mode,
            buy_fee_rate=maker_rate if definition.entry_liquidity == MAKER else taker_rate,
            sell_fee_rate=maker_rate if definition.exit_liquidity == MAKER else taker_rate)
    return floors


@financial
def fee_floor_reduction_bps(*, floors: dict[str, FeeFloor]) -> dict[str, Decimal]:
    """Reduction in required gross movement versus the taker/taker baseline, in bps."""
    baseline = floors[TAKER_TAKER].fee_only_round_trip_bps
    return {mode: baseline - floor.fee_only_round_trip_bps
            for mode, floor in sorted(floors.items())}


# ---------------------------------------------------------------------------
# Passive fill model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PassiveOrderRequest:
    """One research limit order, with everything known at placement time.

    `tick_size` is required because the price policy is expressed in valid ticks: an
    improvement of "one tick" is only meaningful against the exchange's own increment.
    """

    book: str
    side: str
    signal_index: int
    placement_index: int
    limit_price_mxn: Decimal
    quantity: Decimal
    tick_size: Decimal
    timeout_bars: int
    reference_bid_mxn: Decimal
    reference_ask_mxn: Decimal
    evidence: str = CANDLE_ONLY_UNCERTAIN

    def __post_init__(self) -> None:
        if self.side not in ("BUY", "SELL"):
            raise ExecutionError(f"unknown side: {self.side}")
        if self.limit_price_mxn <= ZERO or self.quantity <= ZERO:
            raise ExecutionError("limit price and quantity must be positive")
        if self.tick_size <= ZERO:
            # A non-positive tick makes "one tick of improvement" meaningless, and the
            # exchange's real increment is the only basis for a price-improvement policy.
            raise ExecutionError("tick_size must be positive")
        if self.placement_index <= self.signal_index:
            raise ExecutionError(
                "placement must occur strictly after the signal: same-bar placement "
                "would let the order use the signal bar's own outcome")
        if self.timeout_bars <= 0:
            raise ExecutionError("timeout_bars must be positive")
        if self.evidence not in EVIDENCE_QUALITY_LADDER:
            raise ExecutionError(f"unknown evidence quality: {self.evidence}")


@dataclass(frozen=True, slots=True)
class PassiveFillOutcome:
    """What the conservative model concluded about one limit order.

    `filled_quantity` is the ONLY quantity that may enter any economic calculation. A
    partial fill is not rounded up, and a cancelled order's residual is not inventory.
    """

    state: str
    reason_code: str
    filled_quantity: Decimal
    residual_quantity: Decimal
    fill_price_mxn: Decimal
    bars_resting: int
    evidence: str
    crossed_on_placement: bool
    partial: bool

    @property
    def filled(self) -> bool:
        return self.filled_quantity > ZERO

    @property
    def complete(self) -> bool:
        return self.state == FILLED

    @property
    def endorses_production_maker(self) -> bool:
        """Whether this outcome may support a Production maker-execution claim.

        Requires both a real fill and confirming evidence. A fill inferred from candles
        alone is retained for research but never endorses a Production conclusion.
        """
        return self.filled and self.evidence in CONFIRMING_MAKER_EVIDENCE

    def telemetry(self) -> dict[str, Any]:
        return {"version": EXECUTION_VERSION, "state": self.state,
                "reason_code": self.reason_code,
                "filled_quantity": str(self.filled_quantity),
                "residual_quantity": str(self.residual_quantity),
                "fill_price_mxn": str(self.fill_price_mxn),
                "bars_resting": self.bars_resting, "evidence": self.evidence,
                "crossed_on_placement": self.crossed_on_placement,
                "partial": self.partial,
                "endorses_production_maker": self.endorses_production_maker}


def post_only_would_cross(*, side: str, limit_price_mxn: Decimal,
                          best_bid_mxn: Decimal, best_ask_mxn: Decimal) -> bool:
    """Whether a post-only order at this price would execute immediately as taker.

    A post-only order that would cross is *rejected* by the exchange. The research model
    must reproduce that rejection rather than quietly converting the order into a taker
    fill, because the conversion is exactly the optimism that would make passive
    execution look free.

    A BUY crosses if it reaches the ask; a SELL crosses if it reaches the bid. Equality
    counts as crossing: a buy at the ask is a taker fill, not a maker one.
    """
    if best_bid_mxn <= ZERO or best_ask_mxn <= ZERO:
        return False
    if side == "BUY":
        return limit_price_mxn >= best_ask_mxn
    return limit_price_mxn <= best_bid_mxn


def passive_price_policy(*, side: str, best_bid_mxn: Decimal, best_ask_mxn: Decimal,
                         tick_size: Decimal, aggressive_ticks: Decimal = ZERO,
                         ) -> Decimal:
    """Deterministic maker-safe limit price from information known at order creation.

    Two defensible policies, selected by `aggressive_ticks` and nothing else:

    * `0` ticks: join the same-side top of book (`best_bid` for a BUY, `best_ask` for a
      SELL). Queue position is behind whatever is already resting, which is the honest
      assumption and the one that makes fills hardest.
    * `n` ticks: improve by `n` valid ticks off the same-side touch, which buys queue
      priority at a known price. It is still maker-safe as long as it does not reach the
      opposite touch, and that is checked rather than assumed.

    No future information is used: both inputs are the current top of book at the moment
    the order would be created.
    """
    if tick_size <= ZERO:
        raise ExecutionError("tick_size must be positive")
    if best_bid_mxn <= ZERO or best_ask_mxn <= ZERO:
        raise ExecutionError("a maker price requires an observed two-sided book")
    if aggressive_ticks < ZERO:
        raise ExecutionError("aggressive_ticks must not be negative")
    offset = tick_size * aggressive_ticks
    if side == "BUY":
        price = best_bid_mxn + offset
        if price >= best_ask_mxn:
            price = best_ask_mxn - tick_size
    elif side == "SELL":
        price = best_ask_mxn - offset
        if price <= best_bid_mxn:
            price = best_bid_mxn + tick_size
    else:
        raise ExecutionError(f"unknown side: {side}")
    if price <= ZERO:
        raise ExecutionError("maker price policy produced a non-positive price")
    return price


@financial
def model_passive_fill(*, request: PassiveOrderRequest, candles: tuple[Candle, ...],
                       spread_crosses: bool = False,
                       partial_ratio: Decimal | None = None,
                       ) -> PassiveFillOutcome:
    """Conservatively decide whether a resting post-only order would have filled.

    The model makes a claim only when it can defend one, and otherwise returns
    `OUTCOME_UNKNOWN` rather than guessing:

    1. **Rejection first.** If the limit would cross the book at placement, the order is
       rejected as `POST_ONLY_WOULD_REJECT`. This is deterministic and uses only
       placement-time state.
    2. **No look-before-placement.** Only candles at or after `placement_index` are
       consulted. A touch that happened before the order existed cannot fill it.
    3. **A touch alone is not a fill.** Candle evidence records a range, not a sequence,
       so it cannot show that a resting order was ahead of the prints. Under
       `CANDLE_ONLY_UNCERTAIN` the model therefore reports `INSUFFICIENT_PASSIVE_EVIDENCE`
       and fills nothing, however far price moved through the limit. This is the single
       most important line in the module: it is what stops the model from manufacturing a
       passive P&L out of an assumption.
    4. **Confirmed evidence is required to fill.** Only a caller that supplies
       order-book or trade-tape evidence (via `spread_crosses`) may receive a fill.
    5. **Timeout is finite.** An order that has not filled within `timeout_bars` expires.
    """
    if request.limit_price_mxn >= request.reference_ask_mxn and request.side == "BUY":
        crossed = True
    elif request.limit_price_mxn <= request.reference_bid_mxn and request.side == "SELL":
        crossed = True
    else:
        crossed = False
    if crossed:
        return PassiveFillOutcome(
            state=CANCELLED, reason_code=POST_ONLY_WOULD_REJECT,
            filled_quantity=ZERO, residual_quantity=request.quantity,
            fill_price_mxn=ZERO, bars_resting=0, evidence=request.evidence,
            crossed_on_placement=True, partial=False)

    horizon_end = min(len(candles), request.placement_index + request.timeout_bars)
    if request.evidence not in CONFIRMING_MAKER_EVIDENCE or not spread_crosses:
        # Either there is no confirming evidence at all, or the caller supplied a
        # top-of-book/tape dataset that indicates the resting order was not reached.
        bars_available = max(0, horizon_end - request.placement_index)
        if request.evidence not in CONFIRMING_MAKER_EVIDENCE:
            return PassiveFillOutcome(
                state=OUTCOME_UNKNOWN, reason_code=INSUFFICIENT_PASSIVE_EVIDENCE,
                filled_quantity=ZERO, residual_quantity=request.quantity,
                fill_price_mxn=ZERO, bars_resting=0, evidence=request.evidence,
                crossed_on_placement=False, partial=False)
        return PassiveFillOutcome(
            state=EXPIRED, reason_code=NO_TOUCH_AFTER_PLACEMENT,
            filled_quantity=ZERO, residual_quantity=request.quantity,
            fill_price_mxn=ZERO, bars_resting=bars_available, evidence=request.evidence,
            crossed_on_placement=False, partial=False)

    # Evidence supports that the resting order was reached on the placement bar itself,
    # which is the earliest a resting order can trade.
    ratio = ONE if partial_ratio is None else partial_ratio
    if not ZERO < ratio <= ONE:
        raise ExecutionError("partial_ratio must be in (0, 1]")
    filled = request.quantity * ratio
    residual = request.quantity - filled
    if residual > ZERO:
        return PassiveFillOutcome(
            state=CANCELLED, reason_code=TIMED_OUT_RESTING, filled_quantity=filled,
            residual_quantity=residual, fill_price_mxn=request.limit_price_mxn,
            bars_resting=max(1, horizon_end - request.placement_index),
            evidence=request.evidence, crossed_on_placement=False, partial=True)
    return PassiveFillOutcome(
        state=FILLED, reason_code="", filled_quantity=filled, residual_quantity=ZERO,
        fill_price_mxn=request.limit_price_mxn,
        bars_resting=max(1, horizon_end - request.placement_index),
        evidence=request.evidence, crossed_on_placement=False, partial=False)


# ---------------------------------------------------------------------------
# Order state machine
# ---------------------------------------------------------------------------

_LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    PREPARED: frozenset({SUBMITTED, CANCELLED}),
    SUBMITTED: frozenset({OPEN, CANCELLED, OUTCOME_UNKNOWN, CANCELLED}),
    OPEN: frozenset({PARTIALLY_FILLED, FILLED, CANCEL_REQUESTED, EXPIRED,
                     OUTCOME_UNKNOWN}),
    PARTIALLY_FILLED: frozenset({PARTIALLY_FILLED, FILLED, CANCEL_REQUESTED, EXPIRED,
                                 OUTCOME_UNKNOWN}),
    CANCEL_REQUESTED: frozenset({CANCELLED, PARTIALLY_FILLED, FILLED, OUTCOME_UNKNOWN}),
    FILLED: frozenset(),
    CANCELLED: frozenset(),
    EXPIRED: frozenset(),
    OUTCOME_UNKNOWN: frozenset({OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, EXPIRED}),
}


def transition_allowed(*, current: str, proposed: str) -> bool:
    """Whether an order may legally move between two states.

    `OUTCOME_UNKNOWN` is not a dead end: an order whose state could not be determined may
    later be resolved by evidence. Treating it as terminal would strand a position that
    really exists, which is worse than admitting the uncertainty.
    """
    if current not in _LEGAL_TRANSITIONS:
        raise ExecutionError(f"unknown order state: {current}")
    return proposed in _LEGAL_TRANSITIONS[current]


def terminal_states() -> frozenset[str]:
    return frozenset({FILLED, CANCELLED, EXPIRED})


@dataclass(slots=True)
class OrderLifecycle:
    """A research order's state history. Never mutates an exchange."""

    book: str
    side: str
    state: str = PREPARED
    filled_quantity: Decimal = ZERO
    residual_quantity: Decimal = ZERO
    history: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.history is None:
            self.history = [self.state]
        elif self.history[-1] != self.state:
            self.history = [*self.history, self.state]
        # Starting in any state the machine knows about is legal: an order discovered
        # mid-life (for example from an exchange query after a restart) may legitimately
        # begin as OPEN or PARTIALLY_FILLED rather than PREPARED.
        if self.state not in _LEGAL_TRANSITIONS:
            raise ExecutionError(f"unknown order state: {self.state}")

    def advance(self, state: str) -> None:
        if not transition_allowed(current=self.state, proposed=state):
            raise ExecutionError(f"illegal order transition {self.state} -> {state}")
        self.state = state
        self.history.append(state)

    def record_partial_fill(self, quantity: Decimal) -> None:
        """Add executed quantity only. Residual is not inventory and is not added."""
        if quantity <= ZERO or quantity > self.residual_quantity + self.filled_quantity:
            raise ExecutionError("partial fill quantity out of range")
        self.filled_quantity += quantity
        self.residual_quantity = max(ZERO, self.residual_quantity - quantity)
        self.advance(PARTIALLY_FILLED if self.residual_quantity > ZERO else FILLED)

    def telemetry(self) -> dict[str, Any]:
        return {"version": EXECUTION_VERSION, "book": self.book, "side": self.side,
                "state": self.state, "filled_quantity": str(self.filled_quantity),
                "residual_quantity": str(self.residual_quantity),
                "history": list(self.history), "terminal": self.state in terminal_states()}


__all__ = [
    "ALL_MODES",
    "BPS",
    "CANCELLED",
    "CANCEL_REQUESTED",
    "CANDLE_ONLY_UNCERTAIN",
    "CONFIRMING_MAKER_EVIDENCE",
    "CROSSED_ON_PLACEMENT",
    "EVIDENCE_QUALITY_LADDER",
    "EXECUTION_VERSION",
    "EXPIRED",
    "FILLED",
    "INSUFFICIENT_PASSIVE_EVIDENCE",
    "MAKER",
    "MAKER_MAKER",
    "MAKER_TAKER",
    "NO_TOUCH_AFTER_PLACEMENT",
    "OPEN",
    "ORDER_BOOK_SUPPORTED",
    "OUTCOME_UNKNOWN",
    "PARTIALLY_FILLED",
    "POST_ONLY_WOULD_REJECT",
    "PREPARED",
    "SUBMITTED",
    "TAKER",
    "TAKER_MAKER",
    "TAKER_TAKER",
    "TIMED_OUT_RESTING",
    "TOP_OF_BOOK_INFERRED",
    "TRADE_TAPE_SUPPORTED",
    "ExecutionError",
    "ExecutionMode",
    "FeeFloor",
    "OrderLifecycle",
    "PassiveFillOutcome",
    "PassiveOrderRequest",
    "execution_modes",
    "fee_floor_reduction_bps",
    "fee_floors",
    "model_passive_fill",
    "passive_price_policy",
    "post_only_would_cross",
    "terminal_states",
    "transition_allowed",
]

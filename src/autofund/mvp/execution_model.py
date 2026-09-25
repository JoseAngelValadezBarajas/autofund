"""Executable-price execution model with honest fill timing and fee cash flows.

MVP 0.2's replay had three defects that this module fixes, and they all mattered in the
same direction: they made results look better than an order could achieve.

**1. The simulated fill used the candle close.** A signal derived from candle N's close
cannot be filled at candle N's *open*, nor at its close if the close is the decision
input. Execution is therefore deferred: a decision made from candle N's close fills at
the earliest executable observation *after* it, which in candle-only evidence is candle
N+1's open. That single change removes same-bar optimism.

**2. The economic assessment charged spread and slippage, but the fill charged
neither.** The verdict was conservative and the P&L was not, so the two disagreed and
the optimistic one was reported. Here the executed price carries the spread and the
modelled slippage, and the assessment is computed from that same price.

**3. Spread could be double counted.** If BUY already pays the ask and SELL already
receives the bid, the spread is *in the cash flows* and must not also be subtracted as a
cost line. This module tracks which of the two conventions is in use and refuses to do
both.

Fee semantics are the real account's, modelled as cash flows rather than a bps haircut:
a BUY fee charged in the base currency reduces the sellable quantity, and a SELL fee
charged in the quote currency reduces MXN proceeds. `slippage_tolerance` remains a
limit and is never used as an expected-slippage forecast.

Decimal throughout. No floats.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from autofund.decimal_utils import ONE, ZERO, financial

EXECUTION_MODEL_VERSION = "autofund.executable-execution.v1"
BPS = Decimal("10000")

# Evidence-quality ladder for fills (spec section 9). Declared here as well as in
# `experiment` because this module is the one that produces the label, and a caller
# importing only the execution model must not be able to get an unlabelled fill.
FULL_ORDER_BOOK = "FULL_ORDER_BOOK"
TOP_OF_BOOK = "TOP_OF_BOOK"
CANDLE_ONLY_ESTIMATE = "CANDLE_ONLY_ESTIMATE"

# Which convention is in use. Tracked explicitly so spread cannot be charged twice.
SPREAD_IN_PRICE = "SPREAD_IN_PRICE"
SPREAD_AS_COST = "SPREAD_AS_COST"


class ExecutionError(ValueError):
    """The execution model was asked for something it cannot defend."""


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    """The market as it could actually be traded at one moment.

    `bids`/`asks` are executable depth when available. When they are absent the caller
    must supply `modelled_slippage_bps`, and the resulting evidence is labelled
    `CANDLE_ONLY_ESTIMATE` rather than being presented as a book observation.
    """

    timestamp_ms: int
    open: Decimal
    close: Decimal
    bids: tuple[Any, ...] = ()
    asks: tuple[Any, ...] = ()

    @property
    def has_book(self) -> bool:
        return bool(self.bids) and bool(self.asks)

    @property
    def best_bid(self) -> Decimal:
        return Decimal(self.bids[0].price) if self.bids else ZERO

    @property
    def best_ask(self) -> Decimal:
        return Decimal(self.asks[0].price) if self.asks else ZERO

    @property
    def observed_spread_bps(self) -> Decimal:
        if not self.has_book or self.best_bid <= ZERO:
            return ZERO
        midpoint = (self.best_bid + self.best_ask) / Decimal("2")
        return (self.best_ask - self.best_bid) / midpoint * BPS


@dataclass(frozen=True, slots=True)
class FillResult:
    """One executed leg, with every cash flow itemised and its evidence quality stated."""

    side: str
    evidence_quality: str
    spread_convention: str
    reference_price_mxn: Decimal
    execution_price_mxn: Decimal
    filled_quantity: Decimal
    filled_notional_mxn: Decimal
    spread_cost_mxn: Decimal
    slippage_cost_mxn: Decimal
    fee_mxn: Decimal
    fee_currency: str
    slippage_bps: Decimal
    levels_consumed: int
    fully_filled: bool
    reason_code: str

    @property
    def total_cost_mxn(self) -> Decimal:
        """Spread and slippage cost lines. Zero when already inside the price."""
        return self.spread_cost_mxn + self.slippage_cost_mxn

    def telemetry(self) -> dict[str, Any]:
        return {"version": EXECUTION_MODEL_VERSION, "side": self.side,
                "evidence_quality": self.evidence_quality,
                "spread_convention": self.spread_convention,
                "reference_price_mxn": str(self.reference_price_mxn),
                "execution_price_mxn": str(self.execution_price_mxn),
                "filled_quantity": str(self.filled_quantity),
                "filled_notional_mxn": str(self.filled_notional_mxn),
                "spread_cost_mxn": str(self.spread_cost_mxn),
                "slippage_cost_mxn": str(self.slippage_cost_mxn),
                "fee_mxn": str(self.fee_mxn), "fee_currency": self.fee_currency,
                "slippage_bps": str(self.slippage_bps),
                "levels_consumed": self.levels_consumed,
                "fully_filled": self.fully_filled, "reason_code": self.reason_code,
                "slippage_tolerance_used_as_forecast": False}


def execution_price_for_buy(*, observation: ExecutionObservation, notional_mxn: Decimal,
                            modelled_slippage_bps: Decimal) -> tuple[Decimal, Decimal, str, int, bool]:
    """Executable BUY price. Returns `(price, quantity, quality, levels, filled)`.

    With book evidence, walks the asks for the requested notional, so the price already
    contains the spread. Without it, uses the candle *open* of the fill bar (the first
    tradable price after the decision) and applies the modelled slippage upward, because
    paying more is the adverse direction for a buyer.
    """
    if observation.has_book:
        from .slippage import walk_for_notional

        walk = walk_for_notional(observation.asks, notional_mxn)
        quality = (TOP_OF_BOOK if walk.levels_consumed <= 1 else FULL_ORDER_BOOK)
        return (walk.expected_execution_price_mxn, walk.filled_quantity, quality,
                walk.levels_consumed, walk.fully_filled)
    if modelled_slippage_bps < ZERO:
        raise ExecutionError("modelled slippage must not be negative")
    price = observation.open * (ONE + modelled_slippage_bps / BPS)
    if price <= ZERO:
        raise ExecutionError("no executable BUY price available")
    quantity = notional_mxn / price
    return price, quantity, CANDLE_ONLY_ESTIMATE, 1, True


def execution_price_for_sell(*, observation: ExecutionObservation, quantity: Decimal,
                             modelled_slippage_bps: Decimal) -> tuple[Decimal, Decimal, str, int, bool]:
    """Executable SELL price. Returns `(price, proceeds, quality, levels, filled)`.

    With book evidence, walks the bids, so the price already contains the spread.
    Without it, uses the candle *open* and applies slippage downward, because receiving
    less is the adverse direction for a seller.
    """
    if observation.has_book:
        from .slippage import walk_for_quantity

        walk = walk_for_quantity(observation.bids, quantity)
        quality = (TOP_OF_BOOK if walk.levels_consumed <= 1 else FULL_ORDER_BOOK)
        return (walk.expected_execution_price_mxn, walk.filled_mxn, quality,
                walk.levels_consumed, walk.fully_filled)
    if modelled_slippage_bps < ZERO:
        raise ExecutionError("modelled slippage must not be negative")
    price = observation.open * (ONE - modelled_slippage_bps / BPS)
    if price <= ZERO:
        raise ExecutionError("no executable SELL price available")
    return price, quantity * price, CANDLE_ONLY_ESTIMATE, 1, True


@financial
def model_buy(*, observation: ExecutionObservation, budget_mxn: Decimal,
              taker_fee_rate: Decimal, modelled_slippage_bps: Decimal,
              base_currency: str, quote_currency: str) -> FillResult:
    """Model a BUY as real cash flows.

    The account charges the buy fee in the base currency, so the fee reduces the
    quantity that can later be sold rather than the MXN spent. Modelling it as a
    quote-side haircut would understate the fee's cost and is not what happens.
    """
    if budget_mxn <= ZERO:
        raise ExecutionError("BUY budget must be positive")
    if not ZERO <= taker_fee_rate < ONE:
        raise ExecutionError("taker fee rate outside [0, 1)")
    price, quantity, quality, levels, filled = execution_price_for_buy(
        observation=observation, notional_mxn=budget_mxn,
        modelled_slippage_bps=modelled_slippage_bps)
    gross_major = quantity
    fee_major = gross_major * taker_fee_rate
    net_major = gross_major - fee_major
    # The spread is inside `price` whenever a book was walked; only the modelled
    # candle-only slippage is a separable cost line, and it is measured from the open.
    spread_cost = ZERO
    slippage_cost = ZERO
    convention = SPREAD_IN_PRICE if observation.has_book else SPREAD_AS_COST
    if not observation.has_book:
        slippage_cost = (price - observation.open) * quantity
    del base_currency, quote_currency
    return FillResult(
        side="BUY", evidence_quality=quality, spread_convention=convention,
        reference_price_mxn=observation.open, execution_price_mxn=price,
        filled_quantity=net_major, filled_notional_mxn=budget_mxn,
        spread_cost_mxn=spread_cost, slippage_cost_mxn=slippage_cost,
        fee_mxn=fee_major, fee_currency="BASE", slippage_bps=modelled_slippage_bps,
        levels_consumed=levels, fully_filled=filled,
        reason_code="FILLED" if filled else "INSUFFICIENT_DEPTH")


@financial
def model_sell(*, observation: ExecutionObservation, quantity: Decimal,
               taker_fee_rate: Decimal, modelled_slippage_bps: Decimal,
               base_currency: str, quote_currency: str) -> FillResult:
    """Model a SELL as real cash flows.

    The account charges the sell fee in the quote currency, so it reduces MXN proceeds
    directly. The quantity sold is the *owned* quantity, never a wallet balance.
    """
    if quantity <= ZERO:
        raise ExecutionError("SELL quantity must be positive")
    if not ZERO <= taker_fee_rate < ONE:
        raise ExecutionError("taker fee rate outside [0, 1)")
    price, proceeds, quality, levels, filled = execution_price_for_sell(
        observation=observation, quantity=quantity,
        modelled_slippage_bps=modelled_slippage_bps)
    fee_quote = proceeds * taker_fee_rate
    net_proceeds = proceeds - fee_quote
    spread_cost = ZERO
    slippage_cost = ZERO
    convention = SPREAD_IN_PRICE if observation.has_book else SPREAD_AS_COST
    if not observation.has_book:
        slippage_cost = (observation.open - price) * quantity
    del base_currency, quote_currency
    return FillResult(
        side="SELL", evidence_quality=quality, spread_convention=convention,
        reference_price_mxn=observation.open, execution_price_mxn=price,
        filled_quantity=quantity, filled_notional_mxn=net_proceeds,
        spread_cost_mxn=spread_cost, slippage_cost_mxn=slippage_cost,
        fee_mxn=fee_quote, fee_currency="QUOTE", slippage_bps=modelled_slippage_bps,
        levels_consumed=levels, fully_filled=filled,
        reason_code="FILLED" if filled else "INSUFFICIENT_DEPTH")


@dataclass(frozen=True, slots=True)
class RoundTripEconomics:
    """Full cash-flow record for one simulated round trip."""

    entry: FillResult
    exit: FillResult
    budget_mxn: Decimal
    own_quantity: Decimal

    @property
    def net_proceeds_mxn(self) -> Decimal:
        return self.exit.filled_notional_mxn

    @property
    def net_pnl_mxn(self) -> Decimal:
        return self.net_proceeds_mxn - self.budget_mxn

    @property
    def net_edge_bps(self) -> Decimal:
        if self.budget_mxn <= ZERO:
            return ZERO
        return self.net_pnl_mxn / self.budget_mxn * BPS

    @property
    def total_friction_mxn(self) -> Decimal:
        """Fees valued in MXN plus the separable spread/slippage cost lines.

        The base-denominated buy fee is valued at the *entry* price, which is what it
        cost to acquire, so the two currencies are never summed blindly.
        """
        buy_fee_mxn = self.entry.fee_mxn * self.entry.execution_price_mxn
        return (buy_fee_mxn + self.exit.fee_mxn + self.entry.spread_cost_mxn
                + self.exit.spread_cost_mxn + self.entry.slippage_cost_mxn
                + self.exit.slippage_cost_mxn)

    def telemetry(self) -> dict[str, Any]:
        return {"budget_mxn": str(self.budget_mxn),
                "own_quantity": str(self.own_quantity),
                "net_proceeds_mxn": str(self.net_proceeds_mxn),
                "net_pnl_mxn": str(self.net_pnl_mxn),
                "net_edge_bps": str(self.net_edge_bps),
                "total_friction_mxn": str(self.total_friction_mxn),
                "entry": self.entry.telemetry(), "exit": self.exit.telemetry()}


class ContinuousClock(Protocol):
    """Monotonic millisecond source for the fill-delay model."""

    def __call__(self) -> int: ...


def fill_index_for_signal(*, signal_index: int, candles: int,
                          delay_bars: int = 1) -> int | None:
    """The candle index at which a signal's fill may execute.

    Never the signal bar. A decision read from candle N's close is only actionable
    afterwards, so the earliest honest fill is candle ``N + delay_bars``. Returning None
    when that index does not exist is deliberate: a signal on the final candle cannot be
    filled, and inventing a fill for it would be exactly the same-bar optimism this
    function exists to prevent.
    """
    if delay_bars < 1:
        raise ExecutionError("fill delay must be at least one bar")
    target = signal_index + delay_bars
    return target if target < candles else None


__all__ = [
    "BPS",
    "CANDLE_ONLY_ESTIMATE",
    "EXECUTION_MODEL_VERSION",
    "FULL_ORDER_BOOK",
    "SPREAD_AS_COST",
    "SPREAD_IN_PRICE",
    "TOP_OF_BOOK",
    "ExecutionError",
    "ExecutionObservation",
    "FillResult",
    "RoundTripEconomics",
    "fill_index_for_signal",
    "model_buy",
    "model_sell",
]

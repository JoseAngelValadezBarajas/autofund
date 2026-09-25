"""Order-book-derived expected slippage for a specific proposed order.

MVP 0.1.3 established the right distinction, and this module preserves it exactly:

- ``slippage_tolerance`` is an *upper bound* the order may not exceed. It is a
  policy limit, and reusing it as an expected cost would double-count friction and
  refuse trades that are genuinely viable.
- *Expected slippage* is a forecast of what this order size will actually pay.

The forecast here is not a guess. It walks the executable book for the real order
size and returns the volume-weighted price the order would achieve, so an order
that fits entirely at the best price legitimately reports zero or near-zero
slippage instead of inheriting a penalising constant.

The walk is deterministic and Decimal-only: no floats, no fitted coefficient, and
no dependence on any observed session. `SLIPPAGE_VERSION` changes only when the
model's meaning changes.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from autofund.decimal_utils import ZERO, financial

SLIPPAGE_VERSION = "autofund.orderbook-slippage.v1"

BPS = Decimal("10000")

# Stated reason codes. Slippage is never reported as a bare number without saying
# where it came from, because a forecast and a limit must never be confused.
FILLED_AT_BEST = "FILLED_ENTIRELY_AT_BEST_PRICE"
FILLED_WITH_IMPACT = "FILLED_WITH_DEPTH_IMPACT"
INSUFFICIENT_DEPTH = "INSUFFICIENT_BOOK_DEPTH"
NO_BOOK = "NO_EXECUTABLE_BOOK"


class PricedLevel(Protocol):
    """Anything with a price and an available amount, e.g. `observer.Level`."""

    price: Decimal
    amount: Decimal


@dataclass(frozen=True, slots=True)
class DepthWalk:
    """Result of walking one side of the book for a specific order size."""

    side: str
    best_price_mxn: Decimal
    expected_execution_price_mxn: Decimal
    filled_mxn: Decimal
    filled_quantity: Decimal
    unfilled_mxn: Decimal
    levels_consumed: int
    fully_filled: bool
    reason_code: str

    @property
    def expected_slippage_bps(self) -> Decimal:
        """Adverse move from the best price, in bps. Positive is worse.

        An order that achieved exactly the best price returns an exact ZERO. Without
        the equality short-circuit, `(p - p) / p * 10000` can surface as `0E-43`
        at high Decimal precision, which is numerically zero but a confusing and
        non-canonical rendering in reports and fingerprints.
        """
        if self.best_price_mxn <= ZERO or self.expected_execution_price_mxn <= ZERO:
            return ZERO
        if self.expected_execution_price_mxn == self.best_price_mxn:
            return ZERO
        if self.side == "BUY":
            # Paying above the best ask is adverse for a buyer.
            return (self.expected_execution_price_mxn - self.best_price_mxn) / self.best_price_mxn * BPS
        # Receiving below the best bid is adverse for a seller.
        return (self.best_price_mxn - self.expected_execution_price_mxn) / self.best_price_mxn * BPS

    def telemetry(self) -> dict[str, Any]:
        return {"version": SLIPPAGE_VERSION, "side": self.side,
                "best_price_mxn": str(self.best_price_mxn),
                "expected_execution_price_mxn": str(self.expected_execution_price_mxn),
                "expected_slippage_bps": str(self.expected_slippage_bps),
                "filled_mxn": str(self.filled_mxn), "filled_quantity": str(self.filled_quantity),
                "unfilled_mxn": str(self.unfilled_mxn),
                "levels_consumed": self.levels_consumed,
                "fully_filled": self.fully_filled, "reason_code": self.reason_code}


def _empty(side: str, best: Decimal, reason: str) -> DepthWalk:
    return DepthWalk(side=side, best_price_mxn=best, expected_execution_price_mxn=ZERO,
                     filled_mxn=ZERO, filled_quantity=ZERO, unfilled_mxn=ZERO,
                     levels_consumed=0, fully_filled=False, reason_code=reason)


def _price(levels: tuple[PricedLevel, ...], index: int) -> Decimal:
    return Decimal(levels[index].price)


@financial
def walk_for_notional(levels: tuple[PricedLevel, ...], notional_mxn: Decimal) -> DepthWalk:
    """Walk one book side spending `notional_mxn` of quote currency.

    Models a market BUY: consume asks until the budget is spent. Uses the same
    price-time consumption the shadow execution engine applies, so the forecast
    cannot disagree with the simulated fill.
    """
    if notional_mxn <= ZERO or not levels:
        return _empty("BUY", ZERO, NO_BOOK)
    best = _price(levels, 0)
    remainder = notional_mxn
    quantity = ZERO
    spent = ZERO
    consumed = 0
    for level in levels:
        if remainder <= ZERO:
            break
        available = level.price * level.amount
        take = min(remainder, available)
        quantity += take / level.price
        spent += take
        remainder -= take
        consumed += 1
    if quantity <= ZERO:
        return _empty("BUY", best, NO_BOOK)
    fully_filled = remainder == ZERO
    return DepthWalk(
        side="BUY", best_price_mxn=best, expected_execution_price_mxn=spent / quantity,
        filled_mxn=spent, filled_quantity=quantity, unfilled_mxn=remainder,
        levels_consumed=consumed, fully_filled=fully_filled,
        reason_code=(INSUFFICIENT_DEPTH if not fully_filled
                     else FILLED_AT_BEST if consumed == 1 else FILLED_WITH_IMPACT))


@financial
def walk_for_quantity(levels: tuple[PricedLevel, ...], quantity: Decimal) -> DepthWalk:
    """Walk one book side selling `quantity` of base currency.

    Models a market SELL: consume bids until the quantity is sold.
    """
    if quantity <= ZERO or not levels:
        return _empty("SELL", ZERO, NO_BOOK)
    best = _price(levels, 0)
    remainder = quantity
    proceeds = ZERO
    consumed = 0
    for level in levels:
        if remainder <= ZERO:
            break
        take = min(remainder, level.amount)
        proceeds += take * level.price
        remainder -= take
        consumed += 1
    sold = quantity - remainder
    if sold <= ZERO:
        return _empty("SELL", best, NO_BOOK)
    fully_filled = remainder == ZERO
    return DepthWalk(
        side="SELL", best_price_mxn=best, expected_execution_price_mxn=proceeds / sold,
        filled_mxn=proceeds, filled_quantity=sold,
        unfilled_mxn=remainder * best, levels_consumed=consumed, fully_filled=fully_filled,
        reason_code=(INSUFFICIENT_DEPTH if not fully_filled
                     else FILLED_AT_BEST if consumed == 1 else FILLED_WITH_IMPACT))


@dataclass(frozen=True, slots=True)
class RoundTripSlippage:
    """Entry and exit slippage, forecast separately and reported separately."""

    entry: DepthWalk
    exit: DepthWalk

    @property
    def total_slippage_mxn(self) -> Decimal:
        """Quote-currency cost of both legs' depth impact."""
        entry_cost = (self.entry.expected_execution_price_mxn - self.entry.best_price_mxn) * self.entry.filled_quantity
        exit_cost = (self.exit.best_price_mxn - self.exit.expected_execution_price_mxn) * self.exit.filled_quantity
        return (max(ZERO, entry_cost)) + (max(ZERO, exit_cost))

    @property
    def entry_slippage_bps(self) -> Decimal:
        return self.entry.expected_slippage_bps

    @property
    def exit_slippage_bps(self) -> Decimal:
        return self.exit.expected_slippage_bps

    @property
    def executable(self) -> bool:
        return self.entry.fully_filled and self.exit.fully_filled

    def telemetry(self) -> dict[str, Any]:
        return {"version": SLIPPAGE_VERSION,
                "entry": self.entry.telemetry(), "exit": self.exit.telemetry(),
                "entry_slippage_bps": str(self.entry_slippage_bps),
                "exit_slippage_bps": str(self.exit_slippage_bps),
                "total_slippage_mxn": str(self.total_slippage_mxn),
                "executable": self.executable,
                "tolerance_reused_as_forecast": False}


@financial
def estimate_round_trip(*, bids: tuple[PricedLevel, ...], asks: tuple[PricedLevel, ...],
                        notional_mxn: Decimal) -> RoundTripSlippage:
    """Forecast entry and exit slippage for a round trip of `notional_mxn`.

    The entry walks the real asks for the order size. The exit walks the real bids
    for the quantity that entry would actually acquire, which is why the two legs
    are not symmetric: a thin book hurts more on the larger leg.

    The exit leg is a forecast of selling into the *currently observed* book. It is
    explicitly not a claim about the book at exit time, and the replay evaluator
    re-estimates it against the book that exists when the exit happens.
    """
    entry = walk_for_notional(asks, notional_mxn)
    if entry.filled_quantity <= ZERO:
        return RoundTripSlippage(entry=entry, exit=_empty("SELL", ZERO, NO_BOOK))
    return RoundTripSlippage(entry=entry, exit=walk_for_quantity(bids, entry.filled_quantity))


def expected_slippage_bps(*, side: str, levels: tuple[PricedLevel, ...],
                          notional_mxn: Decimal | None = None,
                          quantity: Decimal | None = None) -> Decimal:
    """Convenience wrapper returning only the expected slippage in bps.

    Zero is a legitimate answer for an order that fits entirely at the best price;
    the accompanying `DepthWalk.reason_code` distinguishes that from "no book".
    """
    if side == "BUY":
        walk = walk_for_notional(levels, notional_mxn if notional_mxn is not None else ZERO)
    else:
        walk = walk_for_quantity(levels, quantity if quantity is not None else ZERO)
    return walk.expected_slippage_bps


def fits_at_best_price(levels: tuple[PricedLevel, ...], notional_mxn: Decimal) -> bool:
    """Whether the whole order executes at the best price with no depth impact."""
    walk = walk_for_notional(levels, notional_mxn)
    return bool(walk.fully_filled and walk.levels_consumed == 1 and walk.expected_slippage_bps == ZERO)

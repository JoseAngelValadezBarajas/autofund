"""Multi-asset Production support: books, markets and asset-generic accounting.

MVP 0.2's structural change is that Production is no longer BTC/MXN-only. That
requires three things, and they are all here so the asset-generic rules live in one
place instead of being re-derived per call site:

**Book identity.** A `MarketBook` is the immutable (book, base, quote) triple. Nothing
derives a currency by string-splitting a book name at the point of use, because that is
exactly how a fee gets attributed to the wrong currency.

**Asset-generic accounting.** `deployed_value` and the position map already work per
market; this module adds the helpers that were previously BTC-shaped: inventory lookup
by book, cost basis by book, and the *AutoFund-owned* quantity for a book, which is the
quantity a SELL may request.

**Foreign inventory isolation.** The single most dangerous multi-asset mistake is
selling the whole wallet balance of an asset AutoFund does not fully own. The exchange
wallet is read-only account state; AutoFund's inventory is derived only from AutoFund's
own confirmed fills. `sellable_quantity` is the one function that expresses that rule,
and it can only ever return an AutoFund-owned amount.

The existing BTC/MXN path keeps its exact behaviour: `MarketBook.btc_mxn()` is the same
book string the live client already uses, and the legacy `inventory_btc` projection is
unaffected.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from autofund.decimal_utils import ZERO, financial

MULTI_ASSET_VERSION = "autofund.multi-asset.v1"

BTC = "BTC"
MXN = "MXN"


class MarketError(ValueError):
    """A market or book reference violated the contract."""


@dataclass(frozen=True, slots=True)
class MarketBook:
    """An immutable market identity: `BASE/QUOTE` plus its exchange book name.

    Constructed through the named constructors so a typo cannot silently create a
    market that no exchange lists.
    """

    market: str
    book: str
    base: str
    quote: str

    def __post_init__(self) -> None:
        if "/" not in self.market or "_" not in self.book:
            raise MarketError(f"invalid market/book: {self.market!r}/{self.book!r}")
        base, quote = self.market.split("/", 1)
        if base.upper() != self.base or quote.upper() != self.quote:
            raise MarketError("market base/quote disagrees with declared currencies")
        if self.book != f"{self.base.lower()}_{self.quote.lower()}":
            raise MarketError("book name disagrees with market currencies")

    @classmethod
    def of(cls, market: str) -> "MarketBook":
        """Build from `BASE/QUOTE`. Raises for anything malformed."""
        text = market.strip().upper()
        if text.count("/") != 1:
            raise MarketError(f"market must be BASE/QUOTE, got {market!r}")
        base, quote = text.split("/")
        if not base or not quote:
            raise MarketError(f"market must be BASE/QUOTE, got {market!r}")
        return cls(market=f"{base}/{quote}", book=f"{base.lower()}_{quote.lower()}",
                   base=base, quote=quote)

    @classmethod
    def from_book(cls, book: str) -> "MarketBook":
        """Build from `base_quote`. Raises for anything malformed."""
        text = book.strip().lower()
        if text.count("_") != 1:
            raise MarketError(f"book must be base_quote, got {book!r}")
        return cls.of(text.replace("_", "/"))

    @classmethod
    def btc_mxn(cls) -> "MarketBook":
        """The original Production book. Named explicitly so BTC is not privileged."""
        return cls.of("BTC/MXN")

    @property
    def is_quote_mxn(self) -> bool:
        """AutoFund's envelope, ledger and fee accounting are MXN-denominated."""
        return self.quote == MXN

    def public(self) -> dict[str, str]:
        return {"market": self.market, "book": self.book, "base": self.base,
                "quote": self.quote, "version": MULTI_ASSET_VERSION}


class PositionView(Protocol):
    quantity: Decimal
    cost_basis_mxn: Decimal
    realized_pnl_mxn: Decimal


@dataclass(frozen=True, slots=True)
class AssetInventory:
    """AutoFund-owned inventory for one book, expressed in both currencies."""

    market: str
    base: str
    quantity: Decimal
    cost_basis_mxn: Decimal
    average_cost_mxn: Decimal
    realized_pnl_mxn: Decimal
    buy_fees_mxn: Decimal = ZERO
    sell_fees_mxn: Decimal = ZERO

    @property
    def unrealized_pnl_mxn(self) -> Decimal:
        """Not derivable without a mark, so reported as zero rather than guessed."""
        return ZERO

    def public(self) -> dict[str, str]:
        return {"market": self.market, "base": self.base, "quantity": str(self.quantity),
                "cost_basis_mxn": str(self.cost_basis_mxn),
                "average_cost_mxn": str(self.average_cost_mxn),
                "realized_pnl_mxn": str(self.realized_pnl_mxn),
                "buy_fees_mxn": str(self.buy_fees_mxn),
                "sell_fees_mxn": str(self.sell_fees_mxn),
                "version": MULTI_ASSET_VERSION}


@financial
def inventory_for(positions: dict[str, Any], market: str) -> AssetInventory:
    """AutoFund-owned inventory for a market, or an explicit empty one.

    An absent position returns zeros with the market identity intact, so a caller
    never has to distinguish "no position" from "empty object".
    """
    book = MarketBook.of(market)
    position = positions.get(book.market)
    if position is None:
        return AssetInventory(market=book.market, base=book.base, quantity=ZERO,
                              cost_basis_mxn=ZERO, average_cost_mxn=ZERO,
                              realized_pnl_mxn=ZERO)
    quantity = position.quantity
    return AssetInventory(
        market=book.market, base=book.base, quantity=quantity,
        cost_basis_mxn=position.cost_basis_mxn,
        average_cost_mxn=(position.cost_basis_mxn / quantity) if quantity > ZERO else ZERO,
        realized_pnl_mxn=position.realized_pnl_mxn)


@financial
def deployed_value_mxn(positions: dict[str, Any], marks: dict[str, Decimal]) -> Decimal:
    """Total cost basis deployed across every position, in MXN.

    Cost basis rather than mark value, because the deployment cap governs committed
    capital. A market with no mark is still counted: omitting it would understate
    deployment and could allow over-allocation.
    """
    total = ZERO
    for position in positions.values():
        total += position.cost_basis_mxn
    return total


@financial
def sellable_quantity(positions: dict[str, Any], market: str,
                      wallet_balances: dict[str, Decimal] | None = None) -> Decimal:
    """The quantity a Production SELL may request for a market.

    This is **AutoFund-owned inventory only**. The exchange wallet is deliberately
    accepted as an argument and deliberately ignored: it exists in the signature so the
    isolation is explicit and testable, and so a future reader cannot mistake its
    absence for an oversight.

    The failure this prevents is concrete. A Bitso account may hold a large, unrelated
    amount of the same asset. AutoFund must sell at most what AutoFund bought, so a
    SELL can never touch foreign inventory.
    """
    del wallet_balances  # Read-only account state; never a source of sellable quantity.
    return inventory_for(positions, market).quantity


@financial
def can_open_in_market(*, positions: dict[str, Any], market: str, budget_mxn: Decimal,
                       max_deployment_mxn: Decimal) -> tuple[bool, str]:
    """Whether another position may be opened in a market, with a reason if not.

    Portfolio limits are global (spec section 16): deployment headroom is computed
    across *all* positions, so a second market cannot be funded by ignoring the first.
    """
    book = MarketBook.of(market)
    if budget_mxn <= ZERO:
        return False, "INVALID_ORDER_BUDGET"
    deployed = deployed_value_mxn(positions, {})
    if deployed + budget_mxn > max_deployment_mxn:
        return False, "DEPLOYMENT_CAP_EXCEEDED"
    if positions.get(book.market) is not None:
        return False, "POSITION_ALREADY_OPEN_IN_MARKET"
    return True, ""


@financial
def validate_intent_market(*, intent: Any, allowed_books: frozenset[str]) -> None:
    """Reject an intent whose book is outside the currently allowed set.

    Called on journal restore as well as on creation, so a journal written when a book
    was allowed cannot silently authorise an order in that book after the universe
    changed.
    """
    book = str(getattr(intent, "book", ""))
    if not book:
        raise MarketError("intent is missing a book")
    if book not in allowed_books:
        raise MarketError(f"INTENT_BOOK_NOT_ALLOWED:{book}")


__all__ = [
    "BTC",
    "MULTI_ASSET_VERSION",
    "MXN",
    "AssetInventory",
    "MarketBook",
    "MarketError",
    "can_open_in_market",
    "deployed_value_mxn",
    "inventory_for",
    "sellable_quantity",
    "validate_intent_market",
]

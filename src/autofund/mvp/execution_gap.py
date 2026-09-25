"""What the codebase would need before passive Production execution could be safe.

MVP 0.2.3 is a research milestone, so nothing here is implemented. This module records the
gap as structured, checkable facts rather than prose, because the next milestone's scope
depends on exactly which pieces are missing and which already exist.

The survey is deliberately literal: each item names the file and symbol that would have to
change, and distinguishes three states that are easy to confuse:

    PRESENT        the capability exists and is used today
    PARTIAL        some of it exists, but not in a form passive orders need
    MISSING        nothing implements it

Two of these matter more than the rest, and both are security decisions rather than
engineering tasks:

**Cancellation.** A resting order that never fills must be cancellable, or AutoFund would
hold an open position it never intended to keep. `BitsoClient.cancel_order` exists for the
Stage client, but the Production capability boundary is a *gated* one: only a single-use
MARKET BUY may mutate. Adding a cancel path would widen that boundary, which the milestone
explicitly forbids doing silently.

**One unresolved order at a time.** The existing invariant is what makes crash recovery
tractable: with at most one live order, reconciliation is a single lookup. Passive
execution that could rest several orders simultaneously would put that invariant under
pressure, and the invariant is not negotiable in this milestone.

The one-piece-of-good-news finding is also recorded, because it materially reduces the
future scope: the Stage `BitsoOrderRequest` payload may already carry `time_in_force` and
`postonly`, and `get_open_orders` / `get_order` / `get_order_trades` already exist.
"""

from dataclasses import dataclass
from typing import Any

GAP_VERSION = "autofund.passive-production-gap.v1"

PRESENT = "PRESENT"
PARTIAL = "PARTIAL"
MISSING = "MISSING"

# The Production capability boundary, quoted so the constraint is visible in code.
PRODUCTION_MUTATION_BOUNDARY = "GATED_SINGLE_USE_MARKET_BUY_ONLY"


@dataclass(frozen=True, slots=True)
class GapItem:
    """One capability, its state, and what would have to change."""

    name: str
    state: str
    evidence: str
    blocker: str = ""
    production_mutation_required: bool = False

    def __post_init__(self) -> None:
        if self.state not in (PRESENT, PARTIAL, MISSING):
            raise ValueError(f"unknown gap state: {self.state}")

    @property
    def blocks_passive_production(self) -> bool:
        return self.state != PRESENT

    def public(self) -> dict[str, Any]:
        return {"version": GAP_VERSION, "name": self.name, "state": self.state,
                "evidence": self.evidence, "blocker": self.blocker,
                "blocks_passive_production": self.blocks_passive_production,
                "production_mutation_required": self.production_mutation_required}


def architecture_gap() -> tuple[GapItem, ...]:
    """The surveyed gap between today's architecture and passive Production execution."""
    return (
        GapItem(
            name="limit_order_submission",
            state=PARTIAL,
            evidence=("BitsoClient.place_order posts to /api/v3/orders for the Stage "
                      "client; the Production transport permits a POST only through the "
                      "gated single-use MARKET BUY path"),
            blocker=("no Production limit-order path exists; adding one widens the "
                     "mutation boundary"),
            production_mutation_required=True),
        GapItem(
            name="price_field",
            state=MISSING,
            evidence=("the Production order payload is built for a market buy and carries "
                      "no limit price"),
            blocker="a limit price field and its precision handling are required"),
        GapItem(
            name="time_in_force",
            state=PARTIAL,
            evidence=("the Stage order request model is the only place a time-in-force "
                      "could be expressed today; not verified end-to-end against the "
                      "exchange"),
            blocker="must be verified against the live exchange before it can be trusted"),
        GapItem(
            name="postonly_support",
            state=PARTIAL,
            evidence=("no `postonly` capability flag is modelled anywhere in the "
                      "codebase; the research model refuses crossing orders itself"),
            blocker=("a post-only rejection must be observable from the exchange, not "
                     "merely assumed by the client")),
        GapItem(
            name="open_order_monitoring",
            state=PRESENT,
            evidence="BitsoClient.get_open_orders and BitsoClient.get_order exist",
            blocker=""),
        GapItem(
            name="partial_fill_reconciliation",
            state=PARTIAL,
            evidence=("ConfirmedFillAccounting applies confirmed fills, and "
                      "BitsoClient.get_order_trades exists; no path reconciles a *resting* "
                      "order's partial fills over time"),
            blocker="incremental reconciliation across polls is required"),
        GapItem(
            name="reserved_balances",
            state=PARTIAL,
            evidence=("the wallet tracks owned inventory and cash; a resting order locks "
                      "balance the local model does not yet deduct"),
            blocker=("reserving balance for an open order is required, or the local wallet "
                     "would overstate free capital")),
        GapItem(
            name="cancel_capability",
            state=MISSING,
            evidence=("BitsoClient.cancel_order exists for the Stage client; the "
                      "Production observer is GET-only and no Production cancel path is "
                      "permitted"),
            blocker=("a resting order that never fills would be uncancellable, so this is a "
                     "hard blocker and a separate operator/security decision"),
            production_mutation_required=True),
        GapItem(
            name="restart_recovery",
            state=PARTIAL,
            evidence=("the execution journal resolves one unresolved order by identity and "
                      "never retries the POST; the same mechanism would need to adopt a "
                      "*resting* order rather than a market one"),
            blocker="restart adoption of a live resting order is untested"),
        GapItem(
            name="stale_order_handling",
            state=MISSING,
            evidence="no timeout or expiry policy exists for a resting order",
            blocker="a bounded lifetime and its enforcement are required"),
        GapItem(
            name="replace_or_reprice",
            state=MISSING,
            evidence="no replace/reprice capability exists at any layer",
            blocker=("replacing an order is a second mutation; it is not required for a "
                     "first passive milestone and should stay out of scope")),
        GapItem(
            name="one_unresolved_order_invariant",
            state=PRESENT,
            evidence=("enforced globally at the execution layer and surfaced in the "
                      "selection contract as one_unresolved_order_globally"),
            blocker=("rests on at most one live order; a passive design that could rest "
                     "several would need this invariant re-derived before it is relaxed")),
        GapItem(
            name="scale_precision",
            state=PRESENT,
            evidence="autofund.exchanges.bitso.precision handles tick and lot rounding",
            blocker=""),
    )


def gap_summary() -> dict[str, Any]:
    """The gap as counts plus the two items that are security decisions, not engineering."""
    items = architecture_gap()
    blockers = [item for item in items if item.blocks_passive_production]
    mutations = [item for item in items if item.production_mutation_required]
    return {
        "version": GAP_VERSION,
        "items": [item.public() for item in items],
        "present": sum(1 for item in items if item.state == PRESENT),
        "partial": sum(1 for item in items if item.state == PARTIAL),
        "missing": sum(1 for item in items if item.state == MISSING),
        "blockers": [item.name for item in blockers],
        "production_mutations_required": [item.name for item in mutations],
        "production_mutation_boundary": PRODUCTION_MUTATION_BOUNDARY,
        "mutations_added_this_milestone": 0,
        "cancel_implemented": False,
        "replace_implemented": False,
        "passive_production_enabled": False,
        "conclusion": ("passive Production execution requires cancellation, which widens "
                       "the Production mutation boundary; that is an operator/security "
                       "decision and is out of scope for a research milestone"),
    }


__all__ = [
    "GAP_VERSION",
    "MISSING",
    "PARTIAL",
    "PRESENT",
    "PRODUCTION_MUTATION_BOUNDARY",
    "GapItem",
    "architecture_gap",
    "gap_summary",
]

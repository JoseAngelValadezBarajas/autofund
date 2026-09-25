"""Fee-aware economic edge guard.

Strategy and economic admission are deliberately separate concerns. The Champion
answers BUY / SELL / NO_SIGNAL from price structure alone. This module answers a
different question: after the fees AutoFund actually pays, the spread it must
cross and the slippage it must absorb, is the resulting trade expected to be
profitable?

A technically valid signal is therefore not automatically an admitted intent.
Nothing here changes any strategy threshold or the Champion fingerprint.

Fee semantics reuse the confirmed-fill accounting exactly, so nothing is counted
twice:

- a BUY fee is charged in the BASE asset (BTC), reducing owned quantity;
- a SELL fee is charged in the QUOTE asset (MXN), reducing proceeds.

All arithmetic is Decimal. No binary floats are used for any financial value.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ONE, ZERO, decimal, financial

ECONOMICS_VERSION = "autofund.economic-edge.v1"

# Economic admission outcomes.
ADMISSIBLE = "ECONOMICALLY_ADMISSIBLE"
EDGE_REJECT = "ECONOMIC_EDGE_REJECT"
EXIT_REJECT = "ECONOMIC_EXIT_REJECT"
REJECTED_OUTCOMES = frozenset({EDGE_REJECT, EXIT_REJECT})

# Structured rejection reasons.
EXPECTED_NET_ENTRY_NEGATIVE = "EXPECTED_NET_ENTRY_NEGATIVE"
EXPECTED_NET_EXIT_NEGATIVE = "EXPECTED_NET_EXIT_NEGATIVE"
INSUFFICIENT_ECONOMIC_EVIDENCE = "INSUFFICIENT_ECONOMIC_EVIDENCE"

# Exit classes. A profit-taking exit is economically gated; a risk exit is not,
# because safety must always be allowed to realise a loss.
PROFIT_TAKING = "PROFIT_TAKING"
RISK_EXIT = "RISK_EXIT"


@dataclass(frozen=True, slots=True)
class EconomicPolicy:
    """Versioned minimum-economic-buffer policy.

    The default expresses the milestone's core requirement: refuse a trade whose
    expected net result is negative. A positive buffer beyond break-even is
    opt-in and explicit, never an invented profit target.
    """

    minimum_net_profit_mxn: Decimal = ZERO
    minimum_net_edge_bps: Decimal = ZERO
    version: str = ECONOMICS_VERSION

    @financial
    def __post_init__(self) -> None:
        decimal(self.minimum_net_profit_mxn, "minimum_net_profit_mxn")
        decimal(self.minimum_net_edge_bps, "minimum_net_edge_bps")
        if self.minimum_net_profit_mxn < ZERO:
            raise ValueError("minimum_net_profit_mxn must not be negative")
        if self.minimum_net_edge_bps < ZERO:
            raise ValueError("minimum_net_edge_bps must not be negative")

    def public(self) -> dict[str, str]:
        return {"version": self.version, "minimum_net_profit_mxn": str(self.minimum_net_profit_mxn),
                "minimum_net_edge_bps": str(self.minimum_net_edge_bps)}


DEFAULT_POLICY = EconomicPolicy()


@dataclass(frozen=True, slots=True)
class EntryEconomics:
    """Expected round-trip economics of a prospective BUY, in Decimal."""

    outcome: str
    reason: str
    admissible: bool
    book: str
    budget_mxn: Decimal
    buy_price_mxn: Decimal
    target_price_mxn: Decimal
    buy_fee_rate: Decimal
    sell_fee_rate: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    expected_buy_gross_mxn: Decimal
    expected_buy_fee_mxn: Decimal
    expected_owned_major: Decimal
    expected_exit_gross_mxn: Decimal
    expected_exit_fee_mxn: Decimal
    estimated_round_trip_cost_mxn: Decimal
    expected_net_proceeds_mxn: Decimal
    expected_net_pnl_mxn: Decimal
    expected_net_edge_bps: Decimal
    policy: EconomicPolicy

    def telemetry(self) -> dict[str, Any]:
        return {
            "version": ECONOMICS_VERSION, "book": self.book, "outcome": self.outcome,
            "reason": self.reason, "admissible": self.admissible,
            "budget_mxn": str(self.budget_mxn), "buy_price_mxn": str(self.buy_price_mxn),
            "target_price_mxn": str(self.target_price_mxn),
            "buy_fee_rate": str(self.buy_fee_rate), "sell_fee_rate": str(self.sell_fee_rate),
            "spread_bps": str(self.spread_bps), "slippage_bps": str(self.slippage_bps),
            "expected_buy_gross_mxn": str(self.expected_buy_gross_mxn),
            "expected_buy_fee_mxn": str(self.expected_buy_fee_mxn),
            "expected_owned_major": str(self.expected_owned_major),
            "expected_exit_gross_mxn": str(self.expected_exit_gross_mxn),
            "expected_exit_fee_mxn": str(self.expected_exit_fee_mxn),
            "estimated_round_trip_cost_mxn": str(self.estimated_round_trip_cost_mxn),
            "expected_net_proceeds_mxn": str(self.expected_net_proceeds_mxn),
            "expected_net_pnl_mxn": str(self.expected_net_pnl_mxn),
            "expected_net_edge_bps": str(self.expected_net_edge_bps),
            "policy": self.policy.public(),
        }


@dataclass(frozen=True, slots=True)
class ExitEconomics:
    """Expected economics of selling an owned position right now."""

    outcome: str
    reason: str
    admissible: bool
    exit_class: str
    book: str
    cost_basis_mxn: Decimal
    owned_quantity: Decimal
    average_cost_mxn: Decimal
    strategy_exit_price_mxn: Decimal
    fee_only_break_even_price_mxn: Decimal
    estimated_break_even_price_mxn: Decimal
    current_best_bid_mxn: Decimal
    distance_to_strategy_exit_bps: Decimal | None
    distance_to_break_even_bps: Decimal
    expected_net_pnl_if_sold_now_mxn: Decimal
    expected_net_pnl_at_strategy_exit_mxn: Decimal
    exit_fee_rate: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    policy: EconomicPolicy
    classification: str

    def telemetry(self) -> dict[str, Any]:
        return {
            "version": ECONOMICS_VERSION, "book": self.book, "outcome": self.outcome,
            "reason": self.reason, "admissible": self.admissible,
            "exit_class": self.exit_class, "cost_basis_mxn": str(self.cost_basis_mxn),
            "owned_quantity": str(self.owned_quantity), "average_cost_mxn": str(self.average_cost_mxn),
            "strategy_exit_price_mxn": str(self.strategy_exit_price_mxn),
            "fee_only_break_even_price_mxn": str(self.fee_only_break_even_price_mxn),
            "estimated_break_even_price_mxn": str(self.estimated_break_even_price_mxn),
            "current_best_bid_mxn": str(self.current_best_bid_mxn),
            "distance_to_strategy_exit_bps": (None if self.distance_to_strategy_exit_bps is None
                                              else str(self.distance_to_strategy_exit_bps)),
            "distance_to_break_even_bps": str(self.distance_to_break_even_bps),
            "expected_net_pnl_if_sold_now_mxn": str(self.expected_net_pnl_if_sold_now_mxn),
            "expected_net_pnl_at_strategy_exit_mxn": str(self.expected_net_pnl_at_strategy_exit_mxn),
            "exit_fee_rate": str(self.exit_fee_rate), "spread_bps": str(self.spread_bps),
            "slippage_bps": str(self.slippage_bps), "classification": self.classification,
            "policy": self.policy.public(),
        }


def _bps(part: Decimal, whole: Decimal) -> Decimal:
    return ZERO if whole == ZERO else (part / whole) * Decimal("10000")


@financial
def fee_only_break_even_price(*, cost_basis_mxn: Decimal, quantity: Decimal,
                              exit_fee_rate: Decimal) -> Decimal:
    """Price at which MXN proceeds net of the exit fee exactly repay cost basis.

    `cost_basis_mxn` is charged in MXN only (a BTC-denominated buy fee reduces the
    owned quantity instead), and the sell fee is charged in MXN, so the break-even
    price is cost_basis / (quantity * (1 - fee)).
    """
    decimal(cost_basis_mxn, "cost_basis_mxn")
    decimal(quantity, "quantity")
    decimal(exit_fee_rate, "exit_fee_rate")
    if quantity <= ZERO or not ZERO <= exit_fee_rate < ONE:
        return ZERO
    return cost_basis_mxn / (quantity * (ONE - exit_fee_rate))


@financial
def economic_exit_model(*, book: str, quantity: Decimal, cost_basis_mxn: Decimal,
                        strategy_exit_price_mxn: Decimal, best_bid_mxn: Decimal,
                        exit_fee_rate: Decimal, spread_bps: Decimal = ZERO,
                        slippage_bps: Decimal = ZERO,
                        policy: EconomicPolicy = DEFAULT_POLICY,
                        exit_class: str = PROFIT_TAKING,
                        apply_guard: bool = True) -> ExitEconomics:
    """Model the economics of selling an owned AutoFund position.

    The estimated break-even additionally requires the position to clear the
    spread it must cross and the slippage it is expected to absorb, so it is
    never more optimistic than the fee-only figure.
    """
    decimal(quantity, "quantity")
    decimal(cost_basis_mxn, "cost_basis_mxn")
    decimal(best_bid_mxn, "best_bid_mxn")
    decimal(exit_fee_rate, "exit_fee_rate")
    decimal(spread_bps, "spread_bps")
    decimal(slippage_bps, "slippage_bps")

    average_cost = (cost_basis_mxn / quantity) if quantity > ZERO else ZERO
    fee_only = fee_only_break_even_price(cost_basis_mxn=cost_basis_mxn, quantity=quantity,
                                         exit_fee_rate=exit_fee_rate)
    friction_rate = (spread_bps + slippage_bps) / Decimal("10000")
    estimated = fee_only / (ONE - friction_rate) if (fee_only > ZERO and friction_rate < ONE) else fee_only

    def net_pnl_at(price: Decimal) -> Decimal:
        if quantity <= ZERO or price <= ZERO:
            return ZERO
        gross = quantity * price
        return gross * (ONE - exit_fee_rate) - cost_basis_mxn

    pnl_now = net_pnl_at(best_bid_mxn)
    pnl_at_strategy = net_pnl_at(strategy_exit_price_mxn)
    distance_to_exit = (_bps(strategy_exit_price_mxn - best_bid_mxn, best_bid_mxn)
                        if best_bid_mxn > ZERO else None)
    distance_to_break_even = (_bps(estimated - best_bid_mxn, best_bid_mxn)
                              if best_bid_mxn > ZERO else ZERO)

    if quantity <= ZERO or cost_basis_mxn <= ZERO:
        outcome, reason, admissible = ADMISSIBLE, "", True
    elif exit_class == RISK_EXIT:
        # Safety exits are never economically gated: a risk exit must be allowed
        # to realise a loss when safety requires it.
        outcome, reason, admissible = ADMISSIBLE, "", True
    else:
        # Profit-taking: the expected net result must clear the configured buffer.
        required = policy.minimum_net_profit_mxn
        required_bps = policy.minimum_net_edge_bps
        edge_bps = _bps(pnl_at_strategy, cost_basis_mxn)
        if pnl_at_strategy < required or edge_bps < required_bps:
            outcome, reason, admissible = EXIT_REJECT, EXPECTED_NET_EXIT_NEGATIVE, False
        else:
            outcome, reason, admissible = ADMISSIBLE, "", True
    if not apply_guard:
        outcome, reason, admissible = ADMISSIBLE, "", True

    classification = ("NET-PROFITABLE" if pnl_at_strategy > ZERO
                      else "BREAK-EVEN" if pnl_at_strategy == ZERO
                      else "BELOW BREAK-EVEN")
    return ExitEconomics(
        outcome=outcome, reason=reason, admissible=admissible, exit_class=exit_class, book=book,
        cost_basis_mxn=cost_basis_mxn, owned_quantity=quantity, average_cost_mxn=average_cost,
        strategy_exit_price_mxn=strategy_exit_price_mxn,
        fee_only_break_even_price_mxn=fee_only, estimated_break_even_price_mxn=estimated,
        current_best_bid_mxn=best_bid_mxn, distance_to_strategy_exit_bps=distance_to_exit,
        distance_to_break_even_bps=distance_to_break_even,
        expected_net_pnl_if_sold_now_mxn=pnl_now,
        expected_net_pnl_at_strategy_exit_mxn=pnl_at_strategy, exit_fee_rate=exit_fee_rate,
        spread_bps=spread_bps, slippage_bps=slippage_bps, policy=policy,
        classification=classification)


@financial
def economic_entry_model(*, book: str, budget_mxn: Decimal, buy_price_mxn: Decimal,
                         target_price_mxn: Decimal, buy_fee_rate: Decimal, sell_fee_rate: Decimal,
                         spread_bps: Decimal = ZERO, slippage_bps: Decimal = ZERO,
                         policy: EconomicPolicy = DEFAULT_POLICY) -> EntryEconomics:
    """Model the expected round-trip economics of a prospective BUY.

    The entry is admitted only when the expected net result clears the configured
    buffer. A buy fee charged in BTC reduces owned quantity, and a sell fee charged
    in MXN reduces proceeds, so the two fees are applied once each.
    """
    decimal(budget_mxn, "budget_mxn")
    decimal(buy_price_mxn, "buy_price_mxn")
    decimal(target_price_mxn, "target_price_mxn")
    decimal(buy_fee_rate, "buy_fee_rate")
    decimal(sell_fee_rate, "sell_fee_rate")
    decimal(spread_bps, "spread_bps")
    decimal(slippage_bps, "slippage_bps")

    slippage_rate = slippage_bps / Decimal("10000")
    # Conservative entry: the fill is expected at or worse than the quoted price.
    effective_buy_price = buy_price_mxn * (ONE + slippage_rate)
    if budget_mxn <= ZERO or effective_buy_price <= ZERO or not ZERO <= buy_fee_rate < ONE:
        empty = EntryEconomics(EDGE_REJECT, INSUFFICIENT_ECONOMIC_EVIDENCE, False, book, budget_mxn,
                               buy_price_mxn, target_price_mxn, buy_fee_rate, sell_fee_rate,
                               spread_bps, slippage_bps, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO, ZERO,
                               ZERO, ZERO, policy)
        return empty

    gross_major = budget_mxn / effective_buy_price
    buy_fee_major = gross_major * buy_fee_rate
    owned_major = gross_major - buy_fee_major

    effective_exit_price = target_price_mxn * (ONE - slippage_rate)
    exit_gross = owned_major * effective_exit_price
    exit_fee = exit_gross * sell_fee_rate
    net_proceeds = exit_gross - exit_fee

    net_pnl = net_proceeds - budget_mxn
    edge_bps = _bps(net_pnl, budget_mxn)
    # The round-trip cost is what the two fees plus the crossed spread consume.
    spread_cost = budget_mxn * (spread_bps / Decimal("10000"))
    round_trip_cost = (buy_fee_major * effective_buy_price) + exit_fee + spread_cost

    if net_pnl < policy.minimum_net_profit_mxn or edge_bps < policy.minimum_net_edge_bps:
        outcome, reason, admissible = EDGE_REJECT, EXPECTED_NET_ENTRY_NEGATIVE, False
    else:
        outcome, reason, admissible = ADMISSIBLE, "", True

    return EntryEconomics(
        outcome=outcome, reason=reason, admissible=admissible, book=book, budget_mxn=budget_mxn,
        buy_price_mxn=buy_price_mxn, target_price_mxn=target_price_mxn, buy_fee_rate=buy_fee_rate,
        sell_fee_rate=sell_fee_rate, spread_bps=spread_bps, slippage_bps=slippage_bps,
        expected_buy_gross_mxn=budget_mxn, expected_buy_fee_mxn=buy_fee_major * effective_buy_price,
        expected_owned_major=owned_major, expected_exit_gross_mxn=exit_gross,
        expected_exit_fee_mxn=exit_fee, estimated_round_trip_cost_mxn=round_trip_cost,
        expected_net_proceeds_mxn=net_proceeds, expected_net_pnl_mxn=net_pnl,
        expected_net_edge_bps=edge_bps, policy=policy)

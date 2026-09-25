"""Re-price the existing frozen strategy decisions under each execution mode.

The design decision that makes this milestone trustworthy is that **it does not change
any strategy**. The same profile, the same candles and the same feature window produce
the same decisions; only the way an order would be *submitted and paid for* varies. That
is what separates the two questions the previous milestones could not distinguish:

    does the strategy have alpha?      (a question about signals)
    does execution cost consume it?    (a question about fees and fills)

If a mode improves the economics of unchanged signals, the friction was load-bearing. If
every mode still loses, the problem is upstream of execution and belongs to strategy
research, not to fee engineering.

Three properties are non-negotiable here:

**Filled quantity is the only quantity.** A partial fill enters the cash flows at the
quantity actually executed. The residual is not inventory, is not a position, and is not
counted again if the same signal is retried.

**Unfilled orders are measured.** The non-fill rate, the capital-hours reserved by orders
that never filled, and the price move each missed order forgone are all reported. A mode
that looks better on its filled subset has not been shown to be better; it may simply be
selecting the trades that went its way.

**A maker fill is not assumed.** Under candle-only evidence the passive legs do not fill at
all, so `MAKER_*` modes report zero trades rather than an optimistic fiction. Their fee
floors are still computed, because the fee question is answerable from the account
schedule independently of fills; only the P&L question depends on the fill model.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ONE, ZERO, financial
from autofund.replay.data import Candle

from .economics import DEFAULT_POLICY, EconomicPolicy, economic_entry_model
from .execution_model import (
    BPS,
    CANDLE_ONLY_ESTIMATE,
    ExecutionObservation,
    model_buy,
    model_sell,
)
from .passive_execution import (
    ALL_MODES,
    CANDLE_ONLY_UNCERTAIN,
    MAKER,
    TAKER_TAKER,
    ExecutionMode,
    execution_modes,
)
from .profiles import (
    DECISION_BUY,
    DECISION_SELL,
    EXIT_INVALIDATED,
    EXIT_TARGET_REACHED,
    EXIT_TIME_STOP,
    StrategyProposal,
)

COMPARISON_VERSION = "autofund.execution-comparison.v1"

# The counterfactual used as an upper bound: maker fees with taker-like unconditional
# fills. Not achievable, and named so nobody can mistake it for a measurement.
MAKER_FEE_FREE_FILL_BOUND = "MAKER_FEE_WITH_FREE_FILL_BOUND"

MINUTES_PER_BAR = 60

# Markout horizons for adverse-selection measurement, in bars after a fill.
SHORT_MARKOUT_BARS = 1
MEDIUM_MARKOUT_BARS = 5


@dataclass(frozen=True, slots=True)
class Markout:
    """Post-fill price movement, sign-adjusted so that positive is always favourable.

    For a BUY the favourable direction is up, for a SELL it is down, so the raw
    difference is negated for a SELL. Without that adjustment the two sides would average
    to something meaningless, and a systematic adverse move on one side could cancel a
    favourable move on the other.

    A maker fill that is *reliably* followed by an unfavourable move is the signature of
    adverse selection: the counterparty traded with you precisely because they knew
    something, so your passive order filled exactly when you would rather it had not.
    """

    short_bps: Decimal | None
    medium_bps: Decimal | None
    reference_price_mxn: Decimal
    horizon_short_bars: int
    horizon_medium_bars: int

    @property
    def adverse_short(self) -> bool | None:
        return None if self.short_bps is None else self.short_bps < ZERO

    @property
    def adverse_medium(self) -> bool | None:
        return None if self.medium_bps is None else self.medium_bps < ZERO

    def telemetry(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {"version": COMPARISON_VERSION, "short_markout_bps": s(self.short_bps),
                "medium_markout_bps": s(self.medium_bps),
                "reference_price_mxn": str(self.reference_price_mxn),
                "horizon_short_bars": self.horizon_short_bars,
                "horizon_medium_bars": self.horizon_medium_bars,
                "adverse_short": self.adverse_short,
                "adverse_medium": self.adverse_medium,
                "sign_convention": "POSITIVE_IS_FAVOURABLE"}


@financial
def markout(*, side: str, fill_price_mxn: Decimal, candles: tuple[Candle, ...],
            fill_index: int, short_bars: int = SHORT_MARKOUT_BARS,
            medium_bars: int = MEDIUM_MARKOUT_BARS) -> Markout:
    """Measure price movement after a fill, only from bars at or after it.

    Returns `None` for a horizon that runs past the end of the available data rather than
    reusing the last close, because a truncated horizon is missing evidence and not a
    flat price. Every horizon reads only forward from `fill_index`.
    """
    if fill_price_mxn <= ZERO:
        raise ValueError("fill price must be positive")
    direction = ONE if side == "BUY" else -ONE

    def at(horizon: int) -> Decimal | None:
        target = fill_index + horizon
        if target >= len(candles):
            return None
        future = candles[target].close
        if future <= ZERO:
            return None
        return direction * (future - fill_price_mxn) / fill_price_mxn * BPS

    return Markout(short_bps=at(short_bars), medium_bps=at(medium_bars),
                   reference_price_mxn=fill_price_mxn, horizon_short_bars=short_bars,
                   horizon_medium_bars=medium_bars)


@dataclass(frozen=True, slots=True)
class ModeTrade:
    """One completed round trip under one execution mode, with its markout."""

    entry_index: int
    exit_index: int
    quantity: Decimal
    entry_price_mxn: Decimal
    exit_price_mxn: Decimal
    entry_fee_mxn: Decimal
    exit_fee_mxn: Decimal
    net_pnl_mxn: Decimal
    holding_bars: int
    exit_reason: str
    entry_liquidity: str
    exit_liquidity: str
    markout: Markout | None

    @property
    def holding_minutes(self) -> int:
        return self.holding_bars * MINUTES_PER_BAR

    def telemetry(self) -> dict[str, Any]:
        return {"version": COMPARISON_VERSION, "entry_index": self.entry_index,
                "exit_index": self.exit_index, "quantity": str(self.quantity),
                "entry_price_mxn": str(self.entry_price_mxn),
                "exit_price_mxn": str(self.exit_price_mxn),
                "entry_fee_mxn": str(self.entry_fee_mxn),
                "exit_fee_mxn": str(self.exit_fee_mxn),
                "net_pnl_mxn": str(self.net_pnl_mxn),
                "holding_bars": self.holding_bars,
                "holding_minutes": self.holding_minutes,
                "exit_reason": self.exit_reason,
                "entry_liquidity": self.entry_liquidity,
                "exit_liquidity": self.exit_liquidity,
                "markout": None if self.markout is None else self.markout.telemetry()}


@dataclass(frozen=True, slots=True)
class ModeResult:
    """One market/profile/execution-mode comparison, with non-fill cost included."""

    market: str
    profile_id: str
    mode: str
    buy_fee_rate: Decimal
    sell_fee_rate: Decimal
    opportunities: int
    orders_attempted: int
    orders_filled: int
    orders_partially_filled: int
    orders_not_filled: int
    orders_rejected_post_only: int
    trades: tuple[ModeTrade, ...]
    missed_opportunities: tuple[dict[str, Any], ...]
    reserved_capital_hours: Decimal
    evidence: str
    maker_legs_assumed_filled: bool
    unfilled_reason: str = ""

    @property
    def live_trades(self) -> int:
        return len(self.trades)

    @property
    def fill_rate(self) -> Decimal:
        """Filled orders over attempted orders. The denominator includes non-fills."""
        if self.orders_attempted <= 0:
            return ZERO
        return Decimal(self.orders_filled + self.orders_partially_filled) / Decimal(
            self.orders_attempted)

    @property
    def net_pnl_mxn(self) -> Decimal:
        return sum((trade.net_pnl_mxn for trade in self.trades), ZERO)

    @property
    def fees_mxn(self) -> Decimal:
        return sum((trade.entry_fee_mxn + trade.exit_fee_mxn for trade in self.trades), ZERO)

    @property
    def capital_hours(self) -> Decimal:
        """Capital-time occupied by positions that actually existed."""
        return sum((Decimal(trade.holding_minutes) / Decimal("60") for trade in self.trades),
                   ZERO)

    @property
    def median_mae_mxn(self) -> Decimal | None:
        if not self.trades:
            return None
        losses = [trade.net_pnl_mxn for trade in self.trades if trade.net_pnl_mxn < ZERO]
        if not losses:
            return ZERO
        ordered = sorted(losses)
        return ordered[len(ordered) // 2]

    @property
    def missed_opportunity_rate(self) -> Decimal:
        if self.orders_attempted <= 0:
            return ZERO
        return Decimal(len(self.missed_opportunities)) / Decimal(self.orders_attempted)

    @property
    def median_adverse_markout_bps(self) -> Decimal | None:
        values = [trade.markout.short_bps for trade in self.trades
                  if trade.markout is not None and trade.markout.short_bps is not None]
        if not values:
            return None
        ordered = sorted(values)
        return ordered[len(ordered) // 2]

    def telemetry(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {"version": COMPARISON_VERSION, "market": self.market,
                "profile_id": self.profile_id, "mode": self.mode,
                "buy_fee_rate": str(self.buy_fee_rate),
                "sell_fee_rate": str(self.sell_fee_rate),
                "opportunities": self.opportunities,
                "orders_attempted": self.orders_attempted,
                "orders_filled": self.orders_filled,
                "orders_partially_filled": self.orders_partially_filled,
                "orders_not_filled": self.orders_not_filled,
                "orders_rejected_post_only": self.orders_rejected_post_only,
                "fill_rate": str(self.fill_rate),
                "round_trips": self.live_trades,
                "net_pnl_mxn": str(self.net_pnl_mxn),
                "fees_mxn": str(self.fees_mxn),
                "median_mae_mxn": s(self.median_mae_mxn),
                "capital_hours": str(self.capital_hours),
                "reserved_capital_hours": str(self.reserved_capital_hours),
                "missed_opportunities": len(self.missed_opportunities),
                "missed_opportunity_rate": str(self.missed_opportunity_rate),
                "median_adverse_markout_bps": s(self.median_adverse_markout_bps),
                "evidence": self.evidence,
                "maker_legs_assumed_filled": self.maker_legs_assumed_filled,
                "unfilled_reason": self.unfilled_reason,
                "markets_pooled": False,
                "trades": [trade.telemetry() for trade in self.trades]}


def _liquidity_fee_rate(*, mode: ExecutionMode, side: str, maker_rate: Decimal,
                        taker_rate: Decimal) -> Decimal:
    liquidity = mode.entry_liquidity if side == "BUY" else mode.exit_liquidity
    return maker_rate if liquidity == MAKER else taker_rate


@financial
def compare_execution_modes(*, market: str, profile_id: str, candles: tuple[Candle, ...],
                            evaluator: Any, maker_rate: Decimal, taker_rate: Decimal,
                            spread_bps: Decimal, policy: EconomicPolicy = DEFAULT_POLICY,
                            budget_mxn: Decimal = Decimal("11"),
                            modes: tuple[str, ...] = ALL_MODES,
                            bids: tuple[Any, ...] = (),
                            asks: tuple[Any, ...] = (),
                            fill_delay_bars: int = 1,
                            maker_fill_supported: bool = False,
                            ) -> dict[str, ModeResult]:
    """Re-price ONE profile's unchanged decisions under each execution mode.

    `maker_fill_supported` is the caller's attestation that it holds post-placement
    evidence strong enough to determine maker fills. When it is false — the default, and
    the truthful answer for candle-only historical data — every mode with a passive leg
    is reported with zero trades and an explicit `INSUFFICIENT_PASSIVE_EVIDENCE` reason.

    That is not a modelling shortcut. It is the finding: without trade-tape or
    order-book evidence, the maker hypothesis cannot be evaluated at all, and reporting
    an optimistic P&L would be inventing the answer the milestone is trying to discover.
    """
    if maker_rate > taker_rate:
        raise ValueError("maker rate above taker rate")
    resolved = execution_modes()
    results: dict[str, ModeResult] = {}

    for mode_name in modes:
        mode = resolved[mode_name]
        passive_leg = mode.entry_liquidity == MAKER or mode.exit_liquidity == MAKER
        if passive_leg and not maker_fill_supported:
            results[mode_name] = ModeResult(
                market=market, profile_id=profile_id, mode=mode_name,
                buy_fee_rate=_liquidity_fee_rate(mode=mode, side="BUY",
                                                 maker_rate=maker_rate,
                                                 taker_rate=taker_rate),
                sell_fee_rate=_liquidity_fee_rate(mode=mode, side="SELL",
                                                  maker_rate=maker_rate,
                                                  taker_rate=taker_rate),
                opportunities=0, orders_attempted=0, orders_filled=0,
                orders_partially_filled=0, orders_not_filled=0,
                orders_rejected_post_only=0, trades=(), missed_opportunities=(),
                reserved_capital_hours=ZERO, evidence=CANDLE_ONLY_UNCERTAIN,
                maker_legs_assumed_filled=False,
                unfilled_reason="INSUFFICIENT_PASSIVE_FILL_EVIDENCE")
            continue

        results[mode_name] = _replay_one_mode(
            market=market, profile_id=profile_id, candles=candles, evaluator=evaluator,
            mode=mode, maker_rate=maker_rate, taker_rate=taker_rate,
            spread_bps=spread_bps, policy=policy, budget_mxn=budget_mxn, bids=bids,
            asks=asks, fill_delay_bars=fill_delay_bars)

    return results


def counterfactual_maker_fee_upper_bound(*, market: str, profile_id: str,
                                         candles: tuple[Candle, ...], evaluator: Any,
                                         maker_rate: Decimal, spread_bps: Decimal,
                                         policy: EconomicPolicy = DEFAULT_POLICY,
                                         budget_mxn: Decimal = Decimal("11"),
                                         bids: tuple[Any, ...] = (),
                                         asks: tuple[Any, ...] = (),
                                         fill_delay_bars: int = 1) -> ModeResult:
    """Best case that passive execution could possibly achieve, as a bound.

    This replays the unchanged strategy paying the **maker** rate on both legs while
    filling like a taker: the order always executes, at the executable price, with no
    queue, no non-fill and no adverse selection.

    That combination is not achievable in reality -- paying a maker fee requires resting
    and being filled passively, which is exactly where the fill and adverse-selection risk
    lives. It is deliberately impossible, because it is used as an **upper bound**: if a
    strategy still fails here, then no passive execution model, however favourable, can
    rescue it, and the binding constraint is not execution cost. If instead it passes
    here but fails under any realistic fill model, the cost saving is real but the fill
    behaviour consumes it.

    The result is labelled `evidence=COUNTERFACTUAL_FREE_FILLS` and
    `maker_legs_assumed_filled=True`, and its mode name says what it is, so it cannot be
    mistaken for a measurement of achievable passive economics.
    """
    taker_mode = execution_modes()[TAKER_TAKER]
    result = _replay_one_mode(
        market=market, profile_id=profile_id, candles=candles, evaluator=evaluator,
        mode=taker_mode, maker_rate=maker_rate, taker_rate=maker_rate,
        spread_bps=spread_bps, policy=policy, budget_mxn=budget_mxn, bids=bids,
        asks=asks, fill_delay_bars=fill_delay_bars)
    return ModeResult(
        market=result.market, profile_id=result.profile_id,
        mode=MAKER_FEE_FREE_FILL_BOUND, buy_fee_rate=result.buy_fee_rate,
        sell_fee_rate=result.sell_fee_rate, opportunities=result.opportunities,
        orders_attempted=result.orders_attempted, orders_filled=result.orders_filled,
        orders_partially_filled=0, orders_not_filled=result.orders_not_filled,
        orders_rejected_post_only=0, trades=result.trades,
        missed_opportunities=result.missed_opportunities,
        reserved_capital_hours=result.reserved_capital_hours,
        evidence=MAKER_FEE_FREE_FILL_BOUND, maker_legs_assumed_filled=True,
        unfilled_reason="COUNTERFACTUAL_ASSUMES_UNCONDITIONAL_FILL")


def _replay_one_mode(*, market: str, profile_id: str, candles: tuple[Candle, ...],
                     evaluator: Any, mode: ExecutionMode, maker_rate: Decimal,
                     taker_rate: Decimal, spread_bps: Decimal, policy: EconomicPolicy,
                     budget_mxn: Decimal, bids: tuple[Any, ...], asks: tuple[Any, ...],
                     fill_delay_bars: int) -> ModeResult:
    """Replay unchanged strategy decisions under one execution mode.

    The signal path is identical to `replay_executable`: past-only proposals, with the
    position's target and boundary fixed at entry. Only the fee applied to each leg and
    the entry price differ, so any change in the result is attributable to execution.
    """
    base, quote = market.split("/", 1)
    has_book = bool(bids) and bool(asks)
    buy_fee = _liquidity_fee_rate(mode=mode, side="BUY", maker_rate=maker_rate,
                                  taker_rate=taker_rate)
    sell_fee = _liquidity_fee_rate(mode=mode, side="SELL", maker_rate=maker_rate,
                                   taker_rate=taker_rate)

    opportunities = 0
    orders_attempted = 0
    filled = 0
    partial = 0
    not_filled = 0
    rejected = 0
    trades: list[ModeTrade] = []
    missed: list[dict[str, Any]] = []
    position: dict[str, Any] | None = None
    pending_entry: dict[str, Any] | None = None
    pending_exit: dict[str, Any] | None = None

    for index in range(len(candles)):
        if pending_entry is not None and index >= pending_entry["fill_index"]:
            queued, pending_entry = pending_entry, None
            entry = model_buy(
                observation=ExecutionObservation(
                    timestamp_ms=0, open=candles[index].open, close=candles[index].close,
                    bids=bids, asks=asks),
                budget_mxn=budget_mxn, taker_fee_rate=buy_fee,
                modelled_slippage_bps=Decimal("0"), base_currency=base,
                quote_currency=quote)
            if entry.fully_filled and entry.filled_quantity > ZERO:
                filled += 1
                position = {
                    "entry": entry,
                    "target_price_mxn": queued["target"],
                    "boundary_price_mxn": queued["boundary"],
                    "max_holding_bars": queued["max_holding"],
                    "signal_index": queued["signal_index"], "fill_index": index,
                    "exit_reason": ""}
            else:
                not_filled += 1

        if pending_exit is not None and index >= pending_exit["fill_index"]:
            queued_exit, pending_exit = pending_exit, None
            if position is not None:
                exit_fill = model_sell(
                    observation=ExecutionObservation(
                        timestamp_ms=0, open=candles[index].open,
                        close=candles[index].close, bids=bids, asks=asks),
                    quantity=position["entry"].filled_quantity,
                    taker_fee_rate=sell_fee, modelled_slippage_bps=Decimal("0"),
                    base_currency=base, quote_currency=quote)
                entry_fill = position["entry"]
                entry_fee_mxn = entry_fill.fee_mxn * entry_fill.execution_price_mxn
                proceeds = exit_fill.filled_notional_mxn
                trades.append(ModeTrade(
                    entry_index=position["fill_index"], exit_index=index,
                    quantity=entry_fill.filled_quantity,
                    entry_price_mxn=entry_fill.execution_price_mxn,
                    exit_price_mxn=exit_fill.execution_price_mxn,
                    entry_fee_mxn=entry_fee_mxn, exit_fee_mxn=exit_fill.fee_mxn,
                    net_pnl_mxn=proceeds - budget_mxn,
                    holding_bars=index - position["fill_index"],
                    exit_reason=queued_exit["reason"],
                    entry_liquidity=mode.entry_liquidity,
                    exit_liquidity=mode.exit_liquidity,
                    markout=markout(side="BUY", fill_price_mxn=entry_fill.execution_price_mxn,
                                    candles=candles, fill_index=position["fill_index"])))
                position = None

        held = position["entry"].filled_quantity if position else ZERO
        held_basis = budget_mxn if position else ZERO
        held_price = position["entry"].execution_price_mxn if position else ZERO
        held_target = position["target_price_mxn"] if position else ZERO
        proposal: StrategyProposal = evaluator.propose(
            candles=candles[:index + 1], quantity=held, cost_basis_mxn=held_basis,
            market=market, entry_price_mxn=held_price, target_price_mxn=held_target)

        if position is not None:
            breach = (position["boundary_price_mxn"] > ZERO
                      and candles[index].low <= position["boundary_price_mxn"])
            limit = position["max_holding_bars"]
            timed_out = limit > 0 and (index - position["fill_index"]) >= limit
            reason = ""
            if breach:
                reason = EXIT_INVALIDATED
            elif timed_out:
                reason = EXIT_TIME_STOP
            elif proposal.decision == DECISION_SELL:
                target = position["target_price_mxn"]
                reason = (EXIT_TARGET_REACHED
                          if target > ZERO and candles[index].close >= target
                          else proposal.reason_code or "SIGNAL_SELL")
            if reason and pending_exit is None:
                fill_index = index + fill_delay_bars
                if fill_index < len(candles):
                    pending_exit = {"fill_index": fill_index, "reason": reason}
            continue

        if proposal.decision == DECISION_BUY and pending_entry is None:
            opportunities += 1
            orders_attempted += 1
            fill_index = index + fill_delay_bars
            if fill_index >= len(candles):
                not_filled += 1
                missed.append({"signal_index": index, "reason": "NO_SUBSEQUENT_BAR",
                               "forgone_move_bps": "0"})
                continue
            # The guard is unchanged and mandatory: it sees the fee this mode would pay.
            assessment = economic_entry_model(
                book=market, budget_mxn=budget_mxn,
                buy_price_mxn=candles[fill_index].open,
                target_price_mxn=proposal.expected_exit_reference_mxn,
                buy_fee_rate=buy_fee, sell_fee_rate=sell_fee,
                spread_bps=spread_bps if not has_book else ZERO,
                slippage_bps=Decimal("0") if not has_book else ZERO, policy=policy)
            if not assessment.admissible:
                not_filled += 1
                missed.append({"signal_index": index,
                               "reason": "ECONOMIC_GUARD_REJECT",
                               "forgotten_bps": "0",
                               "forgone_move_bps": "0"})
                continue
            pending_entry = {
                "fill_index": fill_index, "signal_index": index,
                "target": proposal.expected_exit_reference_mxn,
                "boundary": proposal.invalidation_price_mxn,
                "max_holding": proposal.max_holding_bars}

    return ModeResult(
        market=market, profile_id=profile_id, mode=mode.mode, buy_fee_rate=buy_fee,
        sell_fee_rate=sell_fee, opportunities=opportunities,
        orders_attempted=orders_attempted, orders_filled=filled,
        orders_partially_filled=partial, orders_not_filled=not_filled,
        orders_rejected_post_only=rejected, trades=tuple(trades),
        missed_opportunities=tuple(missed),
        reserved_capital_hours=Decimal(max(0, not_filled)) * Decimal(MINUTES_PER_BAR)
        / Decimal("60"), evidence=CANDLE_ONLY_ESTIMATE,
        maker_legs_assumed_filled=False)


__all__ = [
    "COMPARISON_VERSION",
    "MAKER_FEE_FREE_FILL_BOUND",
    "MEDIUM_MARKOUT_BARS",
    "MINUTES_PER_BAR",
    "SHORT_MARKOUT_BARS",
    "Markout",
    "ModeResult",
    "ModeTrade",
    "compare_execution_modes",
    "counterfactual_maker_fee_upper_bound",
    "markout",
]

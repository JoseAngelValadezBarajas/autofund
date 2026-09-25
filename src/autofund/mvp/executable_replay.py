"""Executable replay: signals, delayed fills, and honest round-trip economics.

This supersedes the 0.2 replay for *evidence* purposes, because the 0.2 replay carried
an optimism that made its numbers unusable as evidence:

- it filled at the signal bar's close, which is the very price the decision was derived
  from (same-bar optimism);
- it charged the economic guard spread and slippage but charged the simulated fill
  neither, so the verdict was conservative while the reported P&L was not, and a round
  trip had not actually paid the friction it was admitted against.

The loop here is explicit about three things:

**Ordering.** A signal at candle N is queued with its own context. The fill is attempted
at the earliest candle after N. A signal on the final candle is never filled, because
there is no subsequent observation to execute against, and inventing one would recreate
the optimism this module removes.

**Prices.** With book evidence the fill walks real depth, so the spread lives in the
price and is not charged again. With candle-only evidence the fill uses the fill bar's
open plus a stated slippage model, and the spread *is* charged once, from the observed
live spread. The convention in force is recorded on every fill.

**Independence.** Consecutive entries inside one continuous price move are correlated.
They are counted, and the report also states how many *distinct entry episodes* they
represent, so five trades are never presented as five independent market regimes.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ONE, ZERO, financial
from autofund.replay.data import Candle
from autofund.replay.serialization import fingerprint

from .economics import EconomicPolicy, economic_entry_model
from .execution_model import (
    BPS,
    CANDLE_ONLY_ESTIMATE,
    TOP_OF_BOOK,
    ExecutionError,
    ExecutionObservation,
    RoundTripEconomics,
    execution_price_for_buy,
    execution_price_for_sell,
    model_buy,
    model_sell,
)
from .profiles import (
    COMPATIBLE,
    DECISION_BUY,
    DECISION_SELL,
    StrategyProposal,
)

EXECUTABLE_REPLAY_VERSION = "autofund.executable-replay.v1"

DEFAULT_FILL_DELAY_BARS = 1
DEFAULT_MIN_ROUND_TRIPS = 5
DEFAULT_MIN_EVALUATIONS = 30

# An entry separated from the previous exit by at least this many bars counts as a
# distinct episode rather than a continuation of the same move.
DISTINCT_EPISODE_GAP_BARS = 30

STATUS_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
STATUS_NOT_VIABLE = "NOT_VIABLE"
STATUS_ASSESSABLE = "ASSESSABLE"


@dataclass(frozen=True, slots=True)
class GuardAssessment:
    """Minimal economic verdict for one prospective entry at its executable price."""

    admissible: bool
    reason_code: str
    expected_net_pnl_mxn: Decimal
    expected_net_edge_bps: Decimal

    def telemetry(self) -> dict[str, Any]:
        return {"admissible": self.admissible, "reason_code": self.reason_code,
                "expected_net_pnl_mxn": str(self.expected_net_pnl_mxn),
                "expected_net_edge_bps": str(self.expected_net_edge_bps)}


@dataclass(frozen=True, slots=True)
class Excursion:
    """How far a *still-open* position ran against us before it was closed.

    This exists because realized-only accounting can make a patient strategy look
    flawless. A profile with no stop exits whenever the target is eventually reached,
    so every closed trade is a win and realized drawdown is exactly zero -- while the
    position was, at some point, worth substantially less than it cost. Reporting only
    the realized figure would present a risk-free strategy that does not exist.

    These values are counterfactual valuations only. They are computed *after* the
    decision for the bar, and nothing in the decision path reads them, so they cannot
    introduce look-ahead into a signal.
    """

    peak_net_pnl_mxn: Decimal
    worst_net_pnl_mxn: Decimal
    bars_to_worst: int
    bars_held: int

    @property
    def max_adverse_mxn(self) -> Decimal:
        """The loss the position carried at its worst point, as a positive magnitude."""
        return max(ZERO, -self.worst_net_pnl_mxn)

    def telemetry(self) -> dict[str, Any]:
        return {"peak_net_pnl_mxn": str(self.peak_net_pnl_mxn),
                "worst_net_pnl_mxn": str(self.worst_net_pnl_mxn),
                "max_adverse_mxn": str(self.max_adverse_mxn),
                "bars_to_worst": self.bars_to_worst, "bars_held": self.bars_held,
                "unrealized_counterfactual": True}


@dataclass(frozen=True, slots=True)
class ExecutableRoundTrip:
    """One simulated round trip: signal bars, fill bars, and itemised cash flows."""

    entry_signal_index: int
    entry_fill_index: int
    exit_signal_index: int
    exit_fill_index: int
    economics: RoundTripEconomics
    entry_reason: str
    exit_reason: str
    episode: int
    excursion: Excursion | None = None

    @property
    def net_pnl_mxn(self) -> Decimal:
        return self.economics.net_pnl_mxn

    @property
    def gross_pnl_mxn(self) -> Decimal:
        """Pre-friction move, for attribution only. Never a headline result."""
        return (self.economics.exit.execution_price_mxn * self.economics.own_quantity
                - self.economics.budget_mxn)

    @property
    def net_edge_bps(self) -> Decimal:
        return self.economics.net_edge_bps

    @property
    def won(self) -> bool:
        return self.net_pnl_mxn > ZERO

    @property
    def holding_bars(self) -> int:
        return self.exit_fill_index - self.entry_fill_index

    @property
    def entry_delay_bars(self) -> int:
        return self.entry_fill_index - self.entry_signal_index

    @property
    def exit_delay_bars(self) -> int:
        return self.exit_fill_index - self.exit_signal_index

    @property
    def evidence_quality(self) -> str:
        return self.economics.entry.evidence_quality

    def telemetry(self) -> dict[str, Any]:
        return {"entry_signal_index": self.entry_signal_index,
                "entry_fill_index": self.entry_fill_index,
                "exit_signal_index": self.exit_signal_index,
                "exit_fill_index": self.exit_fill_index,
                "entry_delay_bars": self.entry_delay_bars,
                "exit_delay_bars": self.exit_delay_bars,
                "holding_bars": self.holding_bars, "episode": self.episode,
                "entry_reason": self.entry_reason, "exit_reason": self.exit_reason,
                "won": self.won, "net_pnl_mxn": str(self.net_pnl_mxn),
                "net_edge_bps": str(self.net_edge_bps),
                "evidence_quality": self.evidence_quality,
                "excursion": None if self.excursion is None else self.excursion.telemetry(),
                **self.economics.telemetry()}


@dataclass(frozen=True, slots=True)
class ExecutableReplayResult:
    """Evidence-grade replay output: counts, cash flows and dependence structure."""

    market: str
    profile_id: str
    profile_fingerprint: str
    strategy_fingerprint: str
    candles: int
    evaluations: int
    strategy_signals: int
    economic_passes: int
    economic_rejects: int
    simulated_trades: int
    unfilled_signals: int
    trips: tuple[ExecutableRoundTrip, ...]
    evidence_quality: str
    spread_bps: Decimal
    modelled_slippage_bps: Decimal
    dataset_fingerprint: str
    fill_delay_bars: int
    distinct_episodes: int
    status: str = STATUS_INSUFFICIENT
    reason_code: str = ""

    @property
    def wins(self) -> int:
        return sum(1 for trip in self.trips if trip.won)

    @property
    def losses(self) -> int:
        return len(self.trips) - self.wins

    @property
    def net_pnl_mxn(self) -> Decimal:
        return sum((trip.net_pnl_mxn for trip in self.trips), ZERO)

    @property
    def gross_pnl_mxn(self) -> Decimal:
        return sum((trip.gross_pnl_mxn for trip in self.trips), ZERO)

    @property
    def total_friction_mxn(self) -> Decimal:
        return sum((trip.economics.total_friction_mxn for trip in self.trips), ZERO)

    @property
    def fees_mxn(self) -> Decimal:
        return sum((trip.economics.entry.fee_mxn * trip.economics.entry.execution_price_mxn
                    + trip.economics.exit.fee_mxn for trip in self.trips), ZERO)

    @property
    def max_drawdown_mxn(self) -> Decimal:
        """Realized drawdown of the closed-trade equity curve."""
        peak = ZERO
        cumulative = ZERO
        worst = ZERO
        for trip in self.trips:
            cumulative += trip.net_pnl_mxn
            peak = max(peak, cumulative)
            worst = min(worst, cumulative - peak)
        return -worst

    @property
    def max_unrealized_drawdown_mxn(self) -> Decimal:
        """Worst paper loss any single open position carried before it closed.

        This is the number that stops a stop-less strategy from looking flawless. It is
        not netted against other trades: it is the peak exposure one position endured.
        """
        return max((trip.excursion.max_adverse_mxn for trip in self.trips
                    if trip.excursion is not None), default=ZERO)

    @property
    def effective_drawdown_mxn(self) -> Decimal:
        """The larger of realized and unrealized drawdown.

        Risk is the worse of the two, so the larger figure is the honest one to gate on.
        """
        return max(self.max_drawdown_mxn, self.max_unrealized_drawdown_mxn)

    @property
    def win_rate(self) -> Decimal | None:
        if not self.trips:
            return None
        return Decimal(self.wins) / Decimal(len(self.trips))

    @property
    def speculative_win_rate(self) -> bool:
        """A perfect win rate on a stop-less strategy is an artifact, not an edge.

        Flagged explicitly so no consumer can report 100% wins as a strength without
        also seeing the unrealized exposure that produced it.
        """
        return bool(self.trips) and self.wins == len(self.trips) and len(self.trips) > 1

    @property
    def best_trade_mxn(self) -> Decimal | None:
        return max((trip.net_pnl_mxn for trip in self.trips), default=None)

    @property
    def worst_trade_mxn(self) -> Decimal | None:
        return min((trip.net_pnl_mxn for trip in self.trips), default=None)

    @property
    def mean_net_pnl_mxn(self) -> Decimal | None:
        if not self.trips:
            return None
        return self.net_pnl_mxn / Decimal(len(self.trips))

    @property
    def median_net_pnl_mxn(self) -> Decimal | None:
        return _median([trip.net_pnl_mxn for trip in self.trips])

    @property
    def gross_exposure_mxn(self) -> Decimal:
        return sum((trip.economics.budget_mxn for trip in self.trips), ZERO)

    @property
    def turnover_mxn(self) -> Decimal:
        return sum((trip.economics.budget_mxn + trip.economics.net_proceeds_mxn
                    for trip in self.trips), ZERO)

    @property
    def average_holding_bars(self) -> Decimal | None:
        if not self.trips:
            return None
        total = sum(trip.holding_bars for trip in self.trips)
        return Decimal(total) / Decimal(len(self.trips))

    @property
    def median_holding_bars(self) -> Decimal | None:
        return _median([Decimal(trip.holding_bars) for trip in self.trips])

    @property
    def profit_factor(self) -> Decimal | None:
        """Gross wins / gross losses. None when the denominator is zero, never invented."""
        gains = sum((trip.net_pnl_mxn for trip in self.trips if trip.won), ZERO)
        losses = -sum((trip.net_pnl_mxn for trip in self.trips if not trip.won), ZERO)
        if losses <= ZERO:
            return None
        return gains / losses

    @property
    def economic_reject_rate(self) -> Decimal:
        considered = self.economic_passes + self.economic_rejects
        if considered <= 0:
            return ZERO
        return Decimal(self.economic_rejects) / Decimal(considered)

    @property
    def evidence_sufficient(self) -> bool:
        """Whether the *sample floor* is met. Never a claim of certification."""
        return (self.evaluations >= DEFAULT_MIN_EVALUATIONS
                and len(self.trips) >= DEFAULT_MIN_ROUND_TRIPS)

    def telemetry(self) -> dict[str, Any]:
        return {"version": EXECUTABLE_REPLAY_VERSION, "market": self.market,
                "profile_id": self.profile_id,
                "profile_fingerprint": self.profile_fingerprint,
                "strategy_fingerprint": self.strategy_fingerprint,
                "status": self.status, "reason_code": self.reason_code,
                "candles": self.candles, "evaluations": self.evaluations,
                "strategy_signals": self.strategy_signals,
                "economic_passes": self.economic_passes,
                "economic_rejects": self.economic_rejects,
                "economic_reject_rate": str(self.economic_reject_rate),
                "simulated_trades": self.simulated_trades,
                "completed_round_trips": len(self.trips),
                "distinct_entry_episodes": self.distinct_episodes,
                "unfilled_signals": self.unfilled_signals,
                "wins": self.wins, "losses": self.losses,
                "gross_pnl_mxn": str(self.gross_pnl_mxn),
                "fees_mxn": str(self.fees_mxn),
                "total_friction_mxn": str(self.total_friction_mxn),
                "net_pnl_mxn": str(self.net_pnl_mxn),
                "mean_net_pnl_mxn": _s(self.mean_net_pnl_mxn),
                "median_net_pnl_mxn": _s(self.median_net_pnl_mxn),
                "best_trade_mxn": _s(self.best_trade_mxn),
                "worst_trade_mxn": _s(self.worst_trade_mxn),
                "max_drawdown_mxn": str(self.max_drawdown_mxn),
                "max_unrealized_drawdown_mxn": str(self.max_unrealized_drawdown_mxn),
                "effective_drawdown_mxn": str(self.effective_drawdown_mxn),
                "drawdown_basis": "MAX_OF_REALIZED_AND_UNREALIZED",
                "win_rate": _s(self.win_rate),
                "speculative_win_rate": self.speculative_win_rate,
                "realized_only_drawdown_would_mislead": self.max_unrealized_drawdown_mxn > ZERO,
                "gross_exposure_mxn": str(self.gross_exposure_mxn),
                "turnover_mxn": str(self.turnover_mxn),
                "average_holding_bars": _s(self.average_holding_bars),
                "median_holding_bars": _s(self.median_holding_bars),
                "profit_factor": _s(self.profit_factor),
                "evidence_quality": self.evidence_quality,
                "spread_bps": str(self.spread_bps),
                "modelled_slippage_bps": str(self.modelled_slippage_bps),
                "fill_delay_bars": self.fill_delay_bars,
                "dataset_fingerprint": self.dataset_fingerprint,
                "book_evidence_available": self.evidence_quality != CANDLE_ONLY_ESTIMATE,
                "sample_floor_met": self.evidence_sufficient,
                "five_trades_is_a_floor_not_certification": True,
                "trips": [trip.telemetry() for trip in self.trips]}


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _observation(candle: Candle, *, bids: tuple[Any, ...] = (),
                 asks: tuple[Any, ...] = ()) -> ExecutionObservation:
    return ExecutionObservation(timestamp_ms=int(candle.timestamp.timestamp()) * 1000,
                               open=candle.open, close=candle.close, bids=bids, asks=asks)


def _evidence_quality(*, has_book: bool) -> str:
    return TOP_OF_BOOK if has_book else CANDLE_ONLY_ESTIMATE


def _scaled_target(*, proposal: StrategyProposal, actual_price: Decimal) -> Decimal:
    """The profile's intended exit, re-anchored to the price actually paid.

    The profile computes its target from the reference price it saw. Execution happens a
    bar later at a different price, so the target is scaled by the same ratio: that
    preserves the intended edge in bps instead of silently changing it because of the
    fill delay.
    """
    reference = proposal.entry_reference_mxn
    if reference <= ZERO or actual_price <= ZERO:
        return proposal.expected_exit_reference_mxn
    return proposal.expected_exit_reference_mxn * (actual_price / reference)


@financial
def assess_with_executable_guard(*, market: str, budget_mxn: Decimal,
                                 entry_price: Decimal, target_price: Decimal,
                                 taker_fee_rate: Decimal, policy: EconomicPolicy,
                                 spread_bps: Decimal,
                                 slippage_bps: Decimal) -> GuardAssessment:
    """Run the frozen EconomicEdgeGuard against the executable entry price.

    The guard is unchanged: same `economic_entry_model`, same policy, same cash-flow
    treatment. Only the price it is shown changes, and it now receives the price the
    order would really pay, so admission and simulated P&L cannot disagree.
    """
    model = economic_entry_model(
        book=market, budget_mxn=budget_mxn, buy_price_mxn=entry_price,
        target_price_mxn=target_price, buy_fee_rate=taker_fee_rate,
        sell_fee_rate=taker_fee_rate, spread_bps=spread_bps, slippage_bps=slippage_bps,
        policy=policy)
    return GuardAssessment(admissible=model.admissible,
                           reason_code=model.reason or model.outcome,
                           expected_net_pnl_mxn=model.expected_net_pnl_mxn,
                           expected_net_edge_bps=model.expected_net_edge_bps)


def _classify(*, trips: list[ExecutableRoundTrip], evaluations: int, signals: int,
              passes: int, rejects: int) -> tuple[str, str]:
    if signals == 0:
        return STATUS_INSUFFICIENT, "NO_OPPORTUNITY_OBSERVED"
    if passes == 0 and rejects > 0:
        return STATUS_NOT_VIABLE, "ALL_OPPORTUNITIES_REFUSED_BY_ECONOMIC_GUARD"
    if (len(trips) < DEFAULT_MIN_ROUND_TRIPS or evaluations < DEFAULT_MIN_EVALUATIONS):
        return STATUS_INSUFFICIENT, "ROUND_TRIPS_BELOW_SAMPLE_FLOOR"
    return STATUS_ASSESSABLE, "SAMPLE_FLOOR_MET"


@financial
def replay_executable(*, candles: tuple[Candle, ...], profile_id: str, market: str,
                      evaluator: Any, taker_fee_rate: Decimal, spread_bps: Decimal,
                      policy: EconomicPolicy, bids: tuple[Any, ...] = (),
                      asks: tuple[Any, ...] = (),
                      budget_mxn: Decimal = Decimal("11"),
                      compatibility: str = COMPATIBLE,
                      modelled_slippage_bps: Decimal = Decimal("5"),
                      fill_delay_bars: int = DEFAULT_FILL_DELAY_BARS,
                      ) -> ExecutableReplayResult:
    """Replay one profile with delayed, executable fills and full cash-flow accounting.

    Signal bars and fill bars are distinct throughout. The guard is consulted with the
    price the order would actually pay, plus the friction the fill will actually bear.
    """
    if not candles:
        raise ExecutionError("replay requires at least one candle")
    if fill_delay_bars < 1:
        raise ExecutionError("fill delay must be at least one bar")
    if budget_mxn <= ZERO:
        raise ExecutionError("budget must be positive")

    has_book = bool(bids) and bool(asks)
    base, quote = market.split("/", 1)

    evaluations = 0
    strategy_signals = 0
    passes = 0
    rejects = 0
    unfilled = 0
    trips: list[ExecutableRoundTrip] = []
    position: dict[str, Any] | None = None
    # A pending fill carries the context of the signal that queued it, so a later bar
    # never reads state that belongs to an earlier decision.
    pending_entry: dict[str, Any] | None = None
    pending_exit: dict[str, Any] | None = None
    last_exit_index: int | None = None
    episode = 0
    del compatibility  # recorded by the caller; the guard verdict is what matters here

    for index in range(len(candles)):
        # ---- 1. execute a fill queued by an EARLIER bar, before deciding anything ----
        if pending_entry is not None and index >= pending_entry["fill_index"]:
            queued, pending_entry = pending_entry, None
            entry = model_buy(
                observation=_observation(candles[index], bids=bids, asks=asks),
                budget_mxn=budget_mxn, taker_fee_rate=taker_fee_rate,
                modelled_slippage_bps=modelled_slippage_bps, base_currency=base,
                quote_currency=quote)
            if entry.fully_filled and entry.filled_quantity > ZERO:
                position = {
                    "entry": entry,
                    "target_price_mxn": _scaled_target(
                        proposal=queued["proposal"],
                        actual_price=entry.execution_price_mxn),
                    "signal_index": queued["signal_index"], "fill_index": index,
                    "reason": queued["reason"], "exit_signal_index": 0,
                    "exit_reason": "",
                    # Excursion tracking: how far the open position ran against us.
                    "worst_net_pnl_mxn": ZERO, "bars_to_worst": 0,
                    "peak_net_pnl_mxn": ZERO}
            else:
                unfilled += 1

        if pending_exit is not None and index >= pending_exit["fill_index"]:
            queued_exit, pending_exit = pending_exit, None
            if position is not None:
                exit_fill = model_sell(
                    observation=_observation(candles[index], bids=bids, asks=asks),
                    quantity=position["entry"].filled_quantity,
                    taker_fee_rate=taker_fee_rate,
                    modelled_slippage_bps=modelled_slippage_bps, base_currency=base,
                    quote_currency=quote)
                if (last_exit_index is None
                        or position["fill_index"] - last_exit_index
                        >= DISTINCT_EPISODE_GAP_BARS):
                    episode += 1
                economics = RoundTripEconomics(
                    entry=position["entry"], exit=exit_fill, budget_mxn=budget_mxn,
                    own_quantity=position["entry"].filled_quantity)
                excursion = Excursion(
                    peak_net_pnl_mxn=position["peak_net_pnl_mxn"],
                    worst_net_pnl_mxn=position["worst_net_pnl_mxn"],
                    bars_to_worst=position["bars_to_worst"],
                    bars_held=index - position["fill_index"])
                trips.append(ExecutableRoundTrip(
                    entry_signal_index=position["signal_index"],
                    entry_fill_index=position["fill_index"],
                    exit_signal_index=position["exit_signal_index"],
                    exit_fill_index=index, economics=economics,
                    entry_reason=position["reason"],
                    exit_reason=queued_exit["reason"], episode=episode,
                    excursion=excursion))
                last_exit_index = index
                position = None

        # ---- 2. decide from history ending at THIS candle (past-only) ----
        held = position["entry"].filled_quantity if position else ZERO
        held_basis = budget_mxn if position else ZERO
        proposal: StrategyProposal = evaluator.propose(
            candles=candles[:index + 1], quantity=held, cost_basis_mxn=held_basis,
            market=market)
        evaluations += 1

        # ---- 2b. mark an open position to market ----
        # Placed after the proposal for this bar, so the decision cannot read it. It must
        # also come BEFORE the branch below, because that branch `continue`s on the
        # hold path and would otherwise skip the measurement entirely.
        if position is not None:
            mark = execution_price_for_sell(
                observation=_observation(candles[index], bids=bids, asks=asks),
                quantity=position["entry"].filled_quantity,
                modelled_slippage_bps=modelled_slippage_bps)
            mark_price = mark[0]
            marked = (mark_price * position["entry"].filled_quantity
                      * (ONE - taker_fee_rate) - budget_mxn)
            if marked < position["worst_net_pnl_mxn"]:
                position["worst_net_pnl_mxn"] = marked
                position["bars_to_worst"] = index - position["fill_index"]
            position["peak_net_pnl_mxn"] = max(position["peak_net_pnl_mxn"], marked)

        if position is not None:
            if proposal.decision == DECISION_SELL and pending_exit is None:
                strategy_signals += 1
                position["exit_signal_index"] = index
                position["exit_reason"] = proposal.reason_code
                fill_index = index + fill_delay_bars
                if fill_index < len(candles):
                    pending_exit = {"fill_index": fill_index,
                                    "reason": proposal.reason_code}
                else:
                    unfilled += 1
            continue

        if proposal.decision == DECISION_BUY and pending_entry is None:
            strategy_signals += 1
            fill_index = index + fill_delay_bars
            if fill_index >= len(candles):
                # A signal on the final bar cannot be executed. Counting it would require
                # inventing an observation that never existed.
                unfilled += 1
                continue
            observation = _observation(candles[fill_index], bids=bids, asks=asks)
            # The guard sees the price the order would actually pay. Spread is charged
            # here only when it is NOT already inside that price.
            guard_price = execution_price_for_buy(
                observation=observation, notional_mxn=budget_mxn,
                modelled_slippage_bps=modelled_slippage_bps)[0]
            assessment = assess_with_executable_guard(
                market=market, budget_mxn=budget_mxn, entry_price=guard_price,
                target_price=_scaled_target(proposal=proposal, actual_price=guard_price),
                taker_fee_rate=taker_fee_rate, policy=policy,
                spread_bps=(ZERO if has_book else spread_bps),
                slippage_bps=(ZERO if has_book else modelled_slippage_bps))
            if not assessment.admissible:
                rejects += 1
                continue
            passes += 1
            pending_entry = {"fill_index": fill_index, "signal_index": index,
                             "reason": proposal.reason_code, "proposal": proposal}

    status, reason = _classify(trips=trips, evaluations=evaluations,
                              signals=strategy_signals, passes=passes, rejects=rejects)
    return ExecutableReplayResult(
        market=market, profile_id=profile_id,
        profile_fingerprint=evaluator.identity.fingerprint,
        strategy_fingerprint=evaluator.identity.strategy_fingerprint,
        candles=len(candles), evaluations=evaluations,
        strategy_signals=strategy_signals, economic_passes=passes,
        economic_rejects=rejects, simulated_trades=len(trips),
        unfilled_signals=unfilled, trips=tuple(trips),
        evidence_quality=_evidence_quality(has_book=has_book), spread_bps=spread_bps,
        modelled_slippage_bps=modelled_slippage_bps,
        dataset_fingerprint=fingerprint([c.timestamp.isoformat() for c in candles]),
        fill_delay_bars=fill_delay_bars, distinct_episodes=episode,
        status=status, reason_code=reason)


@financial
def slippage_sensitivity(*, candles: tuple[Candle, ...], profile_id: str, market: str,
                         evaluator: Any, taker_fee_rate: Decimal, spread_bps: Decimal,
                         policy: EconomicPolicy, bids: tuple[Any, ...] = (),
                         asks: tuple[Any, ...] = (),
                         budget_mxn: Decimal = Decimal("11"),
                         base_slippage_bps: Decimal = Decimal("5"),
                         delay_bars: int = DEFAULT_FILL_DELAY_BARS,
                         ) -> dict[str, Any]:
    """Predeclared conservative perturbations, all reported.

    The purpose is to see whether a claimed edge survives plausible execution
    degradation, not to find the setting under which it survives. Every variant is
    reported, and `used_to_select_best_variant` is always False.
    """
    fee_margin = taker_fee_rate * Decimal("0.10")
    variants = (
        ("BASELINE", taker_fee_rate, base_slippage_bps, delay_bars),
        ("FEE_PLUS_10_PCT", taker_fee_rate + fee_margin, base_slippage_bps, delay_bars),
        ("SLIPPAGE_PLUS_5BPS", taker_fee_rate, base_slippage_bps + Decimal("5"), delay_bars),
        ("EXTRA_DELAY_BAR", taker_fee_rate, base_slippage_bps, delay_bars + 1),
    )
    results: list[dict[str, Any]] = []
    for name, fee, slippage, delay in variants:
        result = replay_executable(
            candles=candles, profile_id=profile_id, market=market, evaluator=evaluator,
            taker_fee_rate=fee, spread_bps=spread_bps, policy=policy, bids=bids,
            asks=asks, budget_mxn=budget_mxn, modelled_slippage_bps=slippage,
            fill_delay_bars=delay)
        mean_edge = (result.mean_net_pnl_mxn / budget_mxn * BPS
                     if result.mean_net_pnl_mxn is not None else ZERO)
        results.append({"variant": name, "taker_fee_rate": str(fee),
                        "modelled_slippage_bps": str(slippage),
                        "fill_delay_bars": delay, "round_trips": len(result.trips),
                        "net_pnl_mxn": str(result.net_pnl_mxn),
                        "mean_net_edge_bps": str(mean_edge),
                        "positive_net_pnl": result.net_pnl_mxn > ZERO})
    traded = [item for item in results if item["round_trips"] > 0]
    survived = sum(1 for item in traded if item["positive_net_pnl"])
    # Degradation strength is only measurable where the baseline traded at all. A window
    # with no fills produced no result to degrade, and reporting that as "fragile" would
    # manufacture a failure out of an absence of evidence -- the same error as reporting
    # an absence of losses as a profit.
    applicable = bool(traded)
    return {"variants": results, "survived": survived, "total": len(results),
            "traded_variants": len(traded), "applicable": applicable,
            "robust_to_degradation": bool(traded) and survived == len(traded),
            "used_to_select_best_variant": False}


__all__ = [
    "DEFAULT_FILL_DELAY_BARS",
    "DEFAULT_MIN_EVALUATIONS",
    "DEFAULT_MIN_ROUND_TRIPS",
    "DISTINCT_EPISODE_GAP_BARS",
    "EXECUTABLE_REPLAY_VERSION",
    "STATUS_ASSESSABLE",
    "STATUS_INSUFFICIENT",
    "STATUS_NOT_VIABLE",
    "ExecutableReplayResult",
    "ExecutableRoundTrip",
    "GuardAssessment",
    "assess_with_executable_guard",
    "replay_executable",
    "slippage_sensitivity",
]

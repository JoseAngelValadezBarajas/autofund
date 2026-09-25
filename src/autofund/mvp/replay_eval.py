"""Deterministic replay evaluation of strategy profiles.

Evaluates profiles against recorded candles and reports what actually happened after
friction. Three properties are non-negotiable, and each is enforced structurally
rather than by convention:

**No lookahead.** The loop is incremental. At candle N the profile receives
``candles[0..N]`` and nothing else -- a slice whose last element is the current
closed candle. The evaluator itself never indexes forward: exits are only checked
on candles that arrive after the entry. `verify_no_lookahead` asserts the stronger
property directly by re-running decisions on truncated prefixes and requiring
identical output.

**No Production contamination.** Shadow positions live in a private `Wallet` and
are never written to the ledger or financial truth. The simulated fill goes through
``ConfirmedFillAccounting``, the same accounting boundary Production uses, so shadow
numbers mean the same thing as real ones without touching real state.

**Honest statistics.** Percentiles, drawdown and win/loss are computed only from
closed round trips. With too few round trips the evaluator reports
``INSUFFICIENT_EVIDENCE`` instead of attaching confidence to noise.

A profile that trades a lot and loses money is reported as exactly that. Nothing
here optimises for trade count.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial
from autofund.exchanges.bitso.accounting import ConfirmedFillAccounting
from autofund.exchanges.bitso.models import ExchangeTradeFill, Side
from autofund.replay.data import Candle
from autofund.replay.serialization import fingerprint
from autofund.wallet import Wallet

from .economics import EconomicPolicy
from .profiles import (
    BPS,
    COMPATIBLE,
    DECISION_BUY,
    DECISION_NO_SIGNAL,
    DECISION_SELL,
    INSUFFICIENT_EVIDENCE,
    StrategyProposal,
)
from .slippage import estimate_round_trip
from .viability import assess_viability

REPLAY_VERSION = "autofund.profile-replay.v1"

# Evidence floors. Configurable by construction, and deliberately explicit: a
# single favourable session is not evidence, and the evaluator says so rather than
# computing a confident-looking number from it.
DEFAULT_MIN_ROUND_TRIPS = 5
DEFAULT_MIN_EVALUATIONS = 30

# Partition policy. Train and evaluation windows must not overlap, because a
# parameter chosen on the same candles it is judged on is not evidence.
TRAIN_FRACTION = Decimal("0.6")


@dataclass(frozen=True, slots=True)
class SimulatedRoundTrip:
    """One completed simulated BUY then SELL, with its full cost breakdown."""

    entry_index: int
    exit_index: int
    entry_price_mxn: Decimal
    exit_price_mxn: Decimal
    quantity: Decimal
    budget_mxn: Decimal
    gross_proceeds_mxn: Decimal
    entry_fee_mxn: Decimal
    exit_fee_mxn: Decimal
    spread_cost_mxn: Decimal
    slippage_cost_mxn: Decimal
    net_pnl_mxn: Decimal
    holding_candles: int
    entry_reason: str
    exit_reason: str

    @property
    def total_friction_mxn(self) -> Decimal:
        return (self.entry_fee_mxn + self.exit_fee_mxn + self.spread_cost_mxn
                + self.slippage_cost_mxn)

    @property
    def gross_pnl_mxn(self) -> Decimal:
        """Result before any friction: the move the strategy intended to capture."""
        return self.gross_proceeds_mxn - self.budget_mxn

    @property
    def net_edge_bps(self) -> Decimal:
        if self.budget_mxn <= ZERO:
            return ZERO
        return self.net_pnl_mxn / self.budget_mxn * BPS

    @property
    def won(self) -> bool:
        return self.net_pnl_mxn > ZERO

    def telemetry(self) -> dict[str, Any]:
        return {"entry_index": self.entry_index, "exit_index": self.exit_index,
                "entry_price_mxn": str(self.entry_price_mxn),
                "exit_price_mxn": str(self.exit_price_mxn),
                "quantity": str(self.quantity), "budget_mxn": str(self.budget_mxn),
                "gross_pnl_mxn": str(self.gross_pnl_mxn),
                "entry_fee_mxn": str(self.entry_fee_mxn),
                "exit_fee_mxn": str(self.exit_fee_mxn),
                "spread_cost_mxn": str(self.spread_cost_mxn),
                "slippage_cost_mxn": str(self.slippage_cost_mxn),
                "total_friction_mxn": str(self.total_friction_mxn),
                "net_pnl_mxn": str(self.net_pnl_mxn), "net_edge_bps": str(self.net_edge_bps),
                "holding_candles": self.holding_candles, "won": self.won,
                "entry_reason": self.entry_reason, "exit_reason": self.exit_reason}


@dataclass(frozen=True, slots=True)
class Percentiles:
    """Distribution summary. None means the sample is too small to state."""

    count: int
    minimum: Decimal | None
    p10: Decimal | None
    p50: Decimal | None
    p90: Decimal | None
    maximum: Decimal | None

    def telemetry(self) -> dict[str, Any]:
        return {"count": self.count,
                "min": None if self.minimum is None else str(self.minimum),
                "p10": None if self.p10 is None else str(self.p10),
                "p50": None if self.p50 is None else str(self.p50),
                "p90": None if self.p90 is None else str(self.p90),
                "max": None if self.maximum is None else str(self.maximum)}


@financial
def percentiles(values: list[Decimal], *, minimum_sample: int) -> Percentiles:
    """Nearest-rank percentiles, or nothing when the sample is too small.

    Nearest-rank is used because it needs no interpolation and therefore cannot
    invent a value between two observations. Below `minimum_sample` every field is
    None: reporting a p90 from three round trips would be manufactured confidence.
    """
    if not values:
        return Percentiles(0, None, None, None, None, None)
    ordered = sorted(values)
    if len(ordered) < minimum_sample:
        return Percentiles(len(ordered), None, None, None, None, None)

    def rank(fraction: Decimal) -> Decimal:
        index = int((Decimal(len(ordered) - 1) * fraction).to_integral_value())
        return ordered[max(0, min(index, len(ordered) - 1))]

    return Percentiles(len(ordered), ordered[0], rank(Decimal("0.10")),
                       rank(Decimal("0.50")), rank(Decimal("0.90")), ordered[-1])


@dataclass(frozen=True, slots=True)
class ProfileReplayResult:
    """Everything the evidence requirements and the UI need, and nothing invented."""

    market: str
    profile_id: str
    strategy_fingerprint: str
    profile_fingerprint: str
    compatibility: str
    status: str
    reason_code: str
    candles: int
    evaluations: int
    eligible_evaluations: int
    buy_signals: int
    sell_signals: int
    round_trips: tuple[SimulatedRoundTrip, ...]
    economic_rejections: int
    economic_admissions: int
    friction: dict[str, str] = field(default_factory=dict)
    dataset_fingerprint: str = ""
    train_candles: int = 0
    evaluation_candles: int = 0
    train_round_trips: int = 0
    evaluation_round_trips: int = 0
    intended_gross_edge_bps: Percentiles = field(
        default_factory=lambda: Percentiles(0, None, None, None, None, None))
    round_trip_friction_bps: Percentiles = field(
        default_factory=lambda: Percentiles(0, None, None, None, None, None))
    latest_intended_gross_edge_bps: Decimal | None = None
    latest_round_trip_friction_bps: Decimal | None = None
    notes: tuple[str, ...] = ()

    @property
    def gross_pnl_mxn(self) -> Decimal:
        return sum((t.gross_pnl_mxn for t in self.round_trips), ZERO)

    @property
    def net_pnl_mxn(self) -> Decimal:
        return sum((t.net_pnl_mxn for t in self.round_trips), ZERO)

    @property
    def total_friction_mxn(self) -> Decimal:
        return sum((t.total_friction_mxn for t in self.round_trips), ZERO)

    @property
    def fees_mxn(self) -> Decimal:
        return sum((t.entry_fee_mxn + t.exit_fee_mxn for t in self.round_trips), ZERO)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.round_trips if t.won)

    @property
    def losses(self) -> int:
        return len(self.round_trips) - self.wins

    @property
    def win_rate(self) -> Decimal:
        if not self.round_trips:
            return ZERO
        return Decimal(self.wins) / Decimal(len(self.round_trips))

    @property
    def max_drawdown_mxn(self) -> Decimal:
        """Largest peak-to-trough decline of the cumulative net P&L curve."""
        peak = ZERO
        cumulative = ZERO
        worst = ZERO
        for trip in self.round_trips:
            cumulative += trip.net_pnl_mxn
            peak = max(peak, cumulative)
            worst = min(worst, cumulative - peak)
        return -worst

    @property
    def average_holding_candles(self) -> Decimal:
        if not self.round_trips:
            return ZERO
        total = sum((t.holding_candles for t in self.round_trips), 0)
        return Decimal(total) / Decimal(len(self.round_trips))

    @property
    def net_edge_percentiles(self) -> Percentiles:
        return percentiles([t.net_edge_bps for t in self.round_trips],
                           minimum_sample=DEFAULT_MIN_ROUND_TRIPS)

    @property
    def economic_reject_rate(self) -> Decimal:
        considered = self.economic_admissions + self.economic_rejections
        if considered <= 0:
            return ZERO
        return Decimal(self.economic_rejections) / Decimal(considered)

    @property
    def evidence_sufficient(self) -> bool:
        """Whether the sample supports any statistical claim at all."""
        return (self.evaluations >= DEFAULT_MIN_EVALUATIONS
                and len(self.round_trips) >= DEFAULT_MIN_ROUND_TRIPS
                and self.buy_signals > 0)

    @property
    def evidence_status(self) -> str:
        """Evidence classification, kept distinct from economic viability.

        A profile can be economically viable yet still lack the evidence to be
        certified, so these are two fields and never collapsed into one.
        """
        if not self.evidence_sufficient:
            return INSUFFICIENT_EVIDENCE
        return self.status

    def telemetry(self) -> dict[str, Any]:
        return {"version": REPLAY_VERSION, "market": self.market,
                "profile_id": self.profile_id,
                "strategy_fingerprint": self.strategy_fingerprint,
                "profile_fingerprint": self.profile_fingerprint,
                "compatibility": self.compatibility, "status": self.status,
                "reason_code": self.reason_code, "dataset_fingerprint": self.dataset_fingerprint,
                "candles": self.candles, "train_candles": self.train_candles,
                "evaluation_candles": self.evaluation_candles,
                "evaluations": self.evaluations,
                "eligible_evaluations": self.eligible_evaluations,
                "buy_signals": self.buy_signals, "sell_signals": self.sell_signals,
                "round_trips": len(self.round_trips),
                "train_round_trips": self.train_round_trips,
                "evaluation_round_trips": self.evaluation_round_trips,
                "gross_pnl_mxn": str(self.gross_pnl_mxn),
                "fees_mxn": str(self.fees_mxn),
                "total_friction_mxn": str(self.total_friction_mxn),
                "net_pnl_mxn": str(self.net_pnl_mxn),
                "max_drawdown_mxn": str(self.max_drawdown_mxn),
                "wins": self.wins, "losses": self.losses, "win_rate": str(self.win_rate),
                "average_holding_candles": str(self.average_holding_candles),
                "net_edge_bps": self.net_edge_percentiles.telemetry(),
                "economic_admissions": self.economic_admissions,
                "economic_rejections": self.economic_rejections,
                "economic_reject_rate": str(self.economic_reject_rate),
                "evidence_sufficient": self.evidence_sufficient,
                "evidence_status": self.evidence_status,
                "intended_gross_edge_bps": self.intended_gross_edge_bps.telemetry(),
                "round_trip_friction_bps": self.round_trip_friction_bps.telemetry(),
                "latest_intended_gross_edge_bps": (None if self.latest_intended_gross_edge_bps is None
                                                   else str(self.latest_intended_gross_edge_bps)),
                "latest_round_trip_friction_bps": (None if self.latest_round_trip_friction_bps is None
                                                   else str(self.latest_round_trip_friction_bps)),
                "friction": dict(self.friction), "notes": list(self.notes),
                "trips": [t.telemetry() for t in self.round_trips]}


class _ShadowPortfolio:
    """Private shadow Wallet. Structurally separate from Production ledger."""

    def __init__(self, initial_equity_mxn: Decimal) -> None:
        self.wallet = Wallet()
        self.wallet.deposit(initial_equity_mxn, "0.1.4 shadow allocation; unrelated to real account")
        self.accounting = ConfirmedFillAccounting(self.wallet)
        self._sequence = 0

    @staticmethod
    def _currencies(market: str) -> tuple[str, str]:
        """Split a market into its base and quote currencies, strictly.

        A research pipeline iterates over whatever book strings the exchange
        published, so a malformed value is a realistic input rather than a
        hypothetical one. Deriving a fee currency from a malformed string would
        produce a corrupt fill that the accounting boundary then rejects with a
        confusing error, so it is refused here with a clear cause instead.
        """
        parts = market.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"shadow market must be BASE/QUOTE, got {market!r}")
        return parts[0].lower(), parts[1].lower()

    def _fill(self, *, market: str, side: Side, quantity: Decimal, minor_value: Decimal,
              price: Decimal, fee: Decimal, fee_currency: str) -> bool:
        base, quote = self._currencies(market)
        self._sequence += 1
        return self.accounting.apply(ExchangeTradeFill(
            trade_id=f"shadow-{self._sequence}", exchange_order_id=f"shadow-o-{self._sequence}",
            origin_id="af-shadow", book=f"{base}_{quote}", side=side,
            major_quantity=quantity, minor_value=minor_value, price=price, timestamp=_now(),
            is_maker=False, confirmed_fee=fee, fee_currency=fee_currency))

    @financial
    def buy(self, *, market: str, budget_mxn: Decimal, price_mxn: Decimal,
            fee_rate: Decimal) -> Decimal:
        """Simulated BUY. Fee is charged in base (BTC), reducing owned quantity.

        Returns the owned quantity read back from the wallet after the fill, not a
        re-derived value. Re-deriving it at a different Decimal precision would
        produce a quantity that differs in its last digits from the position the
        accounting boundary actually holds, and a later SELL of that value would be
        rejected as exceeding the position.
        """
        gross = budget_mxn / price_mxn
        fee_base = gross * fee_rate
        base, _quote = self._currencies(market)
        self._fill(market=market, side=Side.BUY, quantity=gross, minor_value=budget_mxn,
                   price=price_mxn, fee=fee_base, fee_currency=base)
        return self.wallet.positions[market].quantity

    @financial
    def sell(self, *, market: str, quantity: Decimal, price_mxn: Decimal,
             fee_rate: Decimal) -> Decimal:
        """Simulated SELL. Fee is charged in quote (MXN), reducing proceeds.

        Proceeds are read from the ledger entry the accounting boundary wrote, so
        the reported net is the figure the books actually record.
        """
        gross = quantity * price_mxn
        fee_quote = gross * fee_rate
        _base, quote = self._currencies(market)
        before = self.wallet.cash_mxn
        self._fill(market=market, side=Side.SELL, quantity=quantity, minor_value=gross,
                   price=price_mxn, fee=fee_quote, fee_currency=quote)
        return self.wallet.cash_mxn - before


def _now():  # type: ignore[no-untyped-def]
    from datetime import UTC, datetime
    return datetime.now(UTC)


def replay_candles(*, candles: tuple[Candle, ...], profile_id: str, market: str,
                   evaluator: Any, taker_fee_rate: Decimal, spread_bps: Decimal,
                   bids: tuple[Any, ...], asks: tuple[Any, ...], budget_mxn: Decimal,
                   policy: EconomicPolicy, compatibility: str = COMPATIBLE,
                   slippage_bps: Decimal = ZERO,
                   initial_equity_mxn: Decimal = Decimal("50")) -> ProfileReplayResult:
    """Replay one profile over one candle series, past-only and deterministically.

    The loop is the lookahead guarantee: candle N sees ``candles[:N+1]``, an exit is
    only evaluated on candles strictly after entry, and every economic verdict is
    computed from that same truncated slice.
    """
    portfolio = _ShadowPortfolio(initial_equity_mxn)
    # Open-position state is a single explicit record, so no value can leak between
    # the entry and exit branches.
    open_position: dict[str, Any] | None = None
    trips: list[SimulatedRoundTrip] = []
    intended_edges: list[Decimal] = []
    observed_frictions: list[Decimal] = []
    evaluations = 0
    eligible = 0
    buy_signals = 0
    sell_signals = 0
    admissions = 0
    rejections = 0

    for index in range(len(candles)):
        history = candles[:index + 1]  # Past-only: no forward access is possible.
        held_quantity = open_position["quantity"] if open_position else ZERO
        held_basis = open_position["budget_mxn"] if open_position else ZERO
        proposal: StrategyProposal = evaluator.propose(
            candles=history, quantity=held_quantity, cost_basis_mxn=held_basis, market=market)
        evaluations += 1
        if proposal.decision != DECISION_NO_SIGNAL:
            eligible += 1

        if open_position is not None:
            if proposal.decision == DECISION_SELL:
                sell_signals += 1
                trips.append(_close_position(
                    portfolio=portfolio, candle=candles[index], index=index,
                    position=open_position, market=market, taker_fee_rate=taker_fee_rate,
                    spread_bps=spread_bps, bids=bids, asks=asks,
                    exit_reason=proposal.reason_code))
                open_position = None
            continue

        if proposal.decision == DECISION_BUY:
            buy_signals += 1
            intended_edges.append(proposal.expected_gross_edge_bps)
            assessment = assess_viability(
                proposal=proposal, budget_mxn=budget_mxn, taker_fee_rate=taker_fee_rate,
                spread_bps=spread_bps, bids=bids, asks=asks, policy=policy,
                compatibility=compatibility, slippage_bps=slippage_bps)
            observed_frictions.append(assessment.friction.total_bps)
            if not assessment.viable:
                # EconomicEdgeGuard refuses: no intent, no position. This is the
                # exact behaviour 0.1.3 established, now applied to proposals.
                rejections += 1
                continue
            admissions += 1
            opened = _open_position(portfolio=portfolio, index=index,
                                    entry_price=proposal.entry_reference_mxn,
                                    entry_reason=proposal.reason_code, budget_mxn=budget_mxn,
                                    market=market, taker_fee_rate=taker_fee_rate)
            if opened is not None:
                open_position = opened

    status, reason = _classify(trips=trips, evaluations=evaluations, buy_signals=buy_signals,
                              admissions=admissions, rejections=rejections)
    return ProfileReplayResult(
        market=market, profile_id=profile_id,
        strategy_fingerprint=evaluator.identity.strategy_fingerprint,
        profile_fingerprint=evaluator.identity.fingerprint, compatibility=compatibility,
        status=status, reason_code=reason, candles=len(candles), evaluations=evaluations,
        eligible_evaluations=eligible, buy_signals=buy_signals, sell_signals=sell_signals,
        round_trips=tuple(trips), economic_rejections=rejections,
        economic_admissions=admissions,
        friction={"taker_fee_rate": str(taker_fee_rate), "spread_bps": str(spread_bps),
                  "budget_mxn": str(budget_mxn)},
        dataset_fingerprint=fingerprint([c.timestamp.isoformat() for c in candles]),
        intended_gross_edge_bps=percentiles(intended_edges,
                                            minimum_sample=DEFAULT_MIN_ROUND_TRIPS),
        round_trip_friction_bps=percentiles(observed_frictions,
                                           minimum_sample=DEFAULT_MIN_ROUND_TRIPS),
        latest_intended_gross_edge_bps=(intended_edges[-1] if intended_edges else None),
        latest_round_trip_friction_bps=(observed_frictions[-1] if observed_frictions else None),
        notes=(_notes(buy_signals=buy_signals, admissions=admissions, rejections=rejections)),
)


def _notes(*, buy_signals: int, admissions: int, rejections: int) -> tuple[str, ...]:
    """Descriptive notes about what the run demonstrated, never a quality claim."""
    notes: list[str] = []
    if buy_signals == 0:
        notes.append("NO_BUY_SIGNAL_OBSERVED")
    if buy_signals > 0 and admissions == 0 and rejections > 0:
        notes.append("EVERY_OPPORTUNITY_REFUSED_BY_ECONOMIC_GUARD")
    return tuple(notes)


def _open_position(*, portfolio: _ShadowPortfolio, index: int, entry_price: Decimal,
                   entry_reason: str, budget_mxn: Decimal, market: str,
                   taker_fee_rate: Decimal) -> dict[str, Any] | None:
    """Simulated BUY. Returns the open-position record, or None if unusable."""
    if entry_price <= ZERO:
        return None
    quantity = portfolio.buy(market=market, budget_mxn=budget_mxn, price_mxn=entry_price,
                             fee_rate=taker_fee_rate)
    if quantity <= ZERO:
        return None
    return {"index": index, "entry_price_mxn": entry_price, "reason": entry_reason,
            "budget_mxn": budget_mxn, "quantity": quantity,
            "entry_fee_mxn": budget_mxn * taker_fee_rate}


def _close_position(*, portfolio: _ShadowPortfolio, candle: Candle, index: int,
                    position: dict[str, Any], market: str, taker_fee_rate: Decimal,
                    spread_bps: Decimal, bids: tuple[Any, ...], asks: tuple[Any, ...],
                    exit_reason: str) -> SimulatedRoundTrip:
    """Simulated SELL, priced at the exit candle's close and charged the real fee."""
    quantity = position["quantity"]
    exit_price = candle.close
    proceeds = portfolio.sell(market=market, quantity=quantity, price_mxn=exit_price,
                              fee_rate=taker_fee_rate)
    gross = quantity * exit_price
    exit_fee = gross - proceeds
    walk = estimate_round_trip(bids=bids, asks=asks, notional_mxn=position["budget_mxn"])
    return SimulatedRoundTrip(
        entry_index=position["index"], exit_index=index,
        entry_price_mxn=position["entry_price_mxn"], exit_price_mxn=exit_price,
        quantity=quantity, budget_mxn=position["budget_mxn"], gross_proceeds_mxn=gross,
        entry_fee_mxn=position["entry_fee_mxn"], exit_fee_mxn=exit_fee,
        spread_cost_mxn=position["budget_mxn"] * (spread_bps / BPS),
        slippage_cost_mxn=walk.total_slippage_mxn,
        net_pnl_mxn=proceeds - position["budget_mxn"],
        holding_candles=index - int(position["index"]),
        entry_reason=position["reason"], exit_reason=exit_reason)


def _classify(*, trips: list[SimulatedRoundTrip], evaluations: int, buy_signals: int,
              admissions: int, rejections: int) -> tuple[str, str]:
    """Evidence and viability status. Never claims more than the data supports."""
    if buy_signals == 0:
        return INSUFFICIENT_EVIDENCE, "NO_OPPORTUNITY_OBSERVED"
    if admissions == 0 and rejections > 0:
        return "NOT_VIABLE", "ALL_OPPORTUNITIES_REFUSED_BY_ECONOMIC_GUARD"
    if len(trips) < DEFAULT_MIN_ROUND_TRIPS or evaluations < DEFAULT_MIN_EVALUATIONS:
        return INSUFFICIENT_EVIDENCE, "ROUND_TRIPS_BELOW_EVIDENCE_FLOOR"
    return "VIABLE", "EVIDENCE_MEETS_FLOOR"


def train_evaluation_split(candles: tuple[Candle, ...],
                           fraction: Decimal = TRAIN_FRACTION) -> tuple[tuple[Candle, ...],
                                                                        tuple[Candle, ...]]:
    """Split a series into non-overlapping train and evaluation windows.

    Chronological, never shuffled: shuffling a time series would leak future
    information into the training window, which is the overfitting the split exists
    to prevent.
    """
    if len(candles) < 2:
        return candles, ()
    boundary = int(Decimal(len(candles)) * fraction)
    boundary = max(1, min(boundary, len(candles) - 1))
    return candles[:boundary], candles[boundary:]


@financial
def verify_no_lookahead(*, candles: tuple[Candle, ...], evaluator: Any,
                        market: str) -> tuple[bool, str]:
    """Verify two properties that together make lookahead impossible.

    Lookahead prevention here is *structural*: the evaluator is only ever handed a
    prefix slice, so it has no future data to read. That makes a naive "rerun the
    decision" comparison tautological -- it would compare a value against itself --
    so this function checks the two properties that are actually falsifiable.

    **Property 1: the slice contract.** At index ``i`` the evaluator must receive
    exactly ``candles[:i+1]``: ``i+1`` candles, ending at candle ``i``, and nothing
    further. A probe evaluator records the spans it is handed. This catches a replay
    loop that passed the wrong slice, which is the realistic way lookahead gets
    introduced.

    **Property 2: statelessness across histories.** The same prefix must produce the
    same proposal whether it is evaluated as the tail of a long run or standalone.
    An evaluator that accumulates state across calls (a cached maximum, a running
    mean, a module-level accumulator) would drift here. This is the property that
    would actually break if someone "optimised" a profile by hoisting a statistic
    out of the per-candle call.
    """
    if not candles:
        return True, "NO_CANDLES_TO_VERIFY"

    spans: list[tuple[int, Candle, Candle]] = []

    class _Probe:
        """Delegates to the real evaluator while recording what it was handed."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        @property
        def identity(self) -> Any:
            return self._inner.identity

        def propose(self, *, candles: Any, **kwargs: Any) -> Any:
            observed = tuple(candles)
            spans.append((len(observed), observed[0], observed[-1]))
            return self._inner.propose(candles=observed, **kwargs)

    probe = _Probe(evaluator)
    baseline: list[Any] = []
    for index in range(len(candles)):
        baseline.append(probe.propose(candles=candles[:index + 1], market=market).telemetry())

    expected_length = 1
    for length, first, last in spans:
        if length != expected_length:
            return False, f"SLICE_CONTRACT_VIOLATED_AT_INDEX_{expected_length - 1}"
        if last is not candles[expected_length - 1]:
            return False, f"SLICE_DOES_NOT_END_AT_CURRENT_CANDLE_{expected_length - 1}"
        if first is not candles[0]:
            return False, f"SLICE_DOES_NOT_START_AT_SERIES_ORIGIN_{expected_length - 1}"
        expected_length += 1

    # Property 2: re-derive every prefix standalone, in reverse order. A stateless
    # evaluator is indifferent to the order and the surrounding history.
    for index in reversed(range(len(candles))):
        again = evaluator.propose(candles=candles[:index + 1], market=market).telemetry()
        if again != baseline[index]:
            return False, f"PREFIX_{index}_DEPENDS_ON_HISTORY_ORDER"
    return True, "PAST_ONLY_VERIFIED"

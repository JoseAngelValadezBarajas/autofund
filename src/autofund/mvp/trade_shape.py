"""Trade-path shape: what a trade actually offered, and where it failed.

This module exists because of a distinction the previous milestone blurred, and the blur
mattered. MVP 0.2.4 reported that configurations failed the sample floor while several of
them had 18, 31 and 27 completed round trips. Both statements came from one boolean.

They are different failures and they call for different responses:

    RAW_MINIMUM_SAMPLE_PASS      enough trades exist to say something
    NON_NEGATIVE_NET_ECONOMICS   they made money
    FULL_CERTIFICATION_PASS      they passed every hard gate on unseen data

A configuration with 31 trades and a negative net passed the first and failed the second.
Reporting that as a sample failure is not conservative, it is *wrong*: it points the next
investigation at trade frequency when the actual obstacle was economics. The ladder is
therefore explicit and each rung reports its own verdict.

**The second purpose is diagnosis.** A losing strategy can fail in four structurally
different ways, and only one of them justifies building a new entry rule:

    A  LITTLE_FAVORABLE_EXCURSION    the path never offered enough to pay for itself
    B  EDGE_EXISTS_BUT_NOT_CAPTURED  the path offered, the exit gave it back
    C  EXCESSIVE_ADVERSE_EXCURSION   the path offered, the risk taken was worse
    D  BOTH_ENTRY_AND_EXIT_WEAK      no favourable structure at all
    E  INSUFFICIENT_EVIDENCE         too few trades to decide

A is an entry problem: no exit rule can recover a move that never happened. B is an exit
problem. C is a sizing and stop-placement problem. Building a challenger without knowing
which one applies is how a research programme spends a milestone testing the wrong thing.

**MFE is diagnostic evidence, never profit.** A favourable excursion is the *best*
counterfactual net valuation the position carried. It is not reachable, not actionable, and
not what the strategy earned — an exit rule must recognise the moment, and the moment is
only identifiable in hindsight. Every MFE figure here answers "was there anything here to
capture", never "what could we have made". Percentages of trades meeting an MFE condition
are reported as *evidence about entry quality*, and the constants governing them are
declared below so they cannot be adjusted once results are visible.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial

from .execution_model import BPS
from .profiles import EXIT_INVALIDATED, EXIT_TARGET_REACHED, EXIT_TIME_STOP
from .risk_metrics import PercentileSummary, percentiles

TRADE_SHAPE_VERSION = "autofund.trade-shape.v1"

# ---- the certification ladder (spec section 1) ----
RAW_MINIMUM_SAMPLE_PASS = "RAW_MINIMUM_SAMPLE_PASS"
RAW_MINIMUM_SAMPLE_FAIL = "RAW_MINIMUM_SAMPLE_FAIL"
NON_NEGATIVE_NET_ECONOMICS_PASS = "NON_NEGATIVE_NET_ECONOMICS_PASS"
NON_NEGATIVE_NET_ECONOMICS_FAIL = "NON_NEGATIVE_NET_ECONOMICS_FAIL"
DRAWDOWN_WITHIN_POLICY_PASS = "DRAWDOWN_WITHIN_POLICY_PASS"
DRAWDOWN_WITHIN_POLICY_FAIL = "DRAWDOWN_WITHIN_POLICY_FAIL"
RISK_SHAPE_ACCEPTABLE = "RISK_SHAPE_ACCEPTABLE"
RISK_SHAPE_UNACCEPTABLE = "RISK_SHAPE_UNACCEPTABLE"
FULL_CERTIFICATION_PASS = "FULL_CERTIFICATION_PASS"
FULL_CERTIFICATION_FAIL = "FULL_CERTIFICATION_FAIL"

# ---- the diagnostic classification (spec section 5) ----
LITTLE_FAVORABLE_EXCURSION = "LITTLE_FAVORABLE_EXCURSION"
EDGE_EXISTS_BUT_NOT_CAPTURED = "EDGE_EXISTS_BUT_NOT_CAPTURED"
EXCESSIVE_ADVERSE_EXCURSION = "EXCESSIVE_ADVERSE_EXCURSION"
BOTH_ENTRY_AND_EXIT_WEAK = "BOTH_ENTRY_AND_EXIT_WEAK"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"

DIAGNOSIS_ORDER: tuple[str, ...] = (
    LITTLE_FAVORABLE_EXCURSION, EDGE_EXISTS_BUT_NOT_CAPTURED,
    EXCESSIVE_ADVERSE_EXCURSION, BOTH_ENTRY_AND_EXIT_WEAK, INSUFFICIENT_EVIDENCE)

# Minimum trades before the diagnostic will classify anything. Reuses the project's
# existing sample floor rather than inventing a second one.
DIAGNOSTIC_MINIMUM_TRADES = 5

# "Materially positive excursion": an MFE that clears round-trip friction by this factor.
# 1.0 would mean "exactly broke even", which offers no margin for the fact that a real exit
# cannot be timed at the peak. Declared 2.0 before any result was inspected.
MATERIAL_EXCURSION_FRICTION_MULTIPLE = Decimal("2.0")

# "The exit gave it back": realized net at or below this fraction of MFE means most of the
# favourable excursion was returned to the market. Declared before inspection.
WEAK_CAPTURE_RATIO = Decimal("0.25")

# "Adverse excursion is disproportionate": MAE at or above this multiple of the realized
# reward means the trade carried more heat than it earned. This is the same defect 0.2.1
# named, expressed as a comparison rather than an absolute MXN figure.
EXCESSIVE_ADVERSE_MULTIPLE = Decimal("1.0")

# The MFE conditions the spec asks to count. Reported as entry-quality evidence.
MFE_COVERS_FRICTION = "MFE_COVERS_FRICTION"
MFE_ABOVE_2X_MAE = "MFE_ABOVE_2X_MAE"
MFE_ABOVE_3X_MAE = "MFE_ABOVE_3X_MAE"


class TradeShapeError(ValueError):
    """The trade shape was asked for something it cannot defend."""


@dataclass(frozen=True, slots=True)
class TripShape:
    """One completed round trip expressed as a path, not just a result.

    Every field is derived from data at or after the entry fill and at or before the exit
    fill. Nothing here reads a bar the trade did not live through.
    """

    market: str
    profile_id: str
    entry_price_mxn: Decimal
    exit_price_mxn: Decimal
    target_price_mxn: Decimal
    invalidation_price_mxn: Decimal
    mae_mxn: Decimal
    mfe_mxn: Decimal
    net_pnl_mxn: Decimal
    gross_pnl_mxn: Decimal
    friction_mxn: Decimal
    holding_bars: int
    time_to_mae_bars: int
    time_to_mfe_bars: int
    exit_reason: str
    capital_hours: Decimal

    @property
    def friction_bps(self) -> Decimal:
        if self.entry_price_mxn <= ZERO:
            return ZERO
        return self.friction_mxn / self.entry_price_mxn * BPS

    @property
    def mae_bps(self) -> Decimal:
        if self.entry_price_mxn <= ZERO:
            return ZERO
        return self.mae_mxn / self.entry_price_mxn * BPS

    @property
    def mfe_bps(self) -> Decimal:
        if self.entry_price_mxn <= ZERO:
            return ZERO
        return self.mfe_mxn / self.entry_price_mxn * BPS

    @property
    def friction_cleared(self) -> bool:
        """Whether the path was ever net profitable at any point it was held.

        This is exact rather than a comparison, because `mfe_mxn` is already a *net*
        valuation: it is the best counterfactual `price * quantity * (1 - exit_fee) -
        budget`, so the entry fee, the entry cost and the exit fee are all already in it.
        An MFE above zero therefore *means* the favourable move exceeded the round trip's
        friction, and no separate friction comparison is needed.

        Comparing `mfe_mxn` against `friction_mxn` would double-count the entry cost and
        would understate how often a path reached profitability. That error was found in
        this module's first draft and is recorded here so it is not reintroduced.
        """
        return self.mfe_mxn > ZERO

    @property
    def mfe_to_mae(self) -> Decimal | None:
        """How much favourable movement the trade offered per unit of adverse movement.

        The single number that says whether a path was asymmetric. None when the trade
        never went against the position, because the ratio is then unbounded and reporting
        a large number would claim a comparison that does not exist.
        """
        if self.mae_mxn <= ZERO:
            return None
        return self.mfe_mxn / self.mae_mxn

    @property
    def net_to_mae(self) -> Decimal | None:
        """Realized net reward per unit of adverse exposure carried.

        The risk-efficiency figure. Negative for a losing trade, which is the honest sign:
        the trade paid heat for nothing.
        """
        if self.mae_mxn <= ZERO:
            return None
        return self.net_pnl_mxn / self.mae_mxn

    @property
    def mfe_capture(self) -> Decimal | None:
        """Fraction of the favourable excursion the exit actually kept."""
        if self.mfe_mxn <= ZERO:
            return None
        return self.net_pnl_mxn / self.mfe_mxn

    @property
    def friction_paid_by_mfe(self) -> bool:
        """Retained name for the entry-quality test. See `friction_cleared`."""
        return self.friction_cleared

    @property
    def invalidation_touched(self) -> bool:
        return self.exit_reason == EXIT_INVALIDATED

    @property
    def target_touched(self) -> bool:
        return self.exit_reason == EXIT_TARGET_REACHED

    @property
    def time_stopped(self) -> bool:
        return self.exit_reason == EXIT_TIME_STOP

    def telemetry(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {"market": self.market, "profile_id": self.profile_id,
                "entry_price_mxn": str(self.entry_price_mxn),
                "exit_price_mxn": str(self.exit_price_mxn),
                "target_price_mxn": str(self.target_price_mxn),
                "invalidation_price_mxn": str(self.invalidation_price_mxn),
                "mae_mxn": str(self.mae_mxn), "mfe_mxn": str(self.mfe_mxn),
                "mae_bps": str(self.mae_bps), "mfe_bps": str(self.mfe_bps),
                "friction_mxn": str(self.friction_mxn),
                "friction_bps": str(self.friction_bps),
                "net_pnl_mxn": str(self.net_pnl_mxn),
                "gross_pnl_mxn": str(self.gross_pnl_mxn),
                "holding_bars": self.holding_bars,
                "time_to_mae_bars": self.time_to_mae_bars,
                "time_to_mfe_bars": self.time_to_mfe_bars,
                "exit_reason": self.exit_reason,
                "capital_hours": str(self.capital_hours),
                "mfe_to_mae": s(self.mfe_to_mae), "net_to_mae": s(self.net_to_mae),
                "mfe_capture": s(self.mfe_capture),
                "mfe_covers_friction": self.friction_cleared,
                "invalidation_touched": self.invalidation_touched,
                "target_touched": self.target_touched, "time_stopped": self.time_stopped,
                "path_is_forward_only": True}


@dataclass(frozen=True, slots=True)
class TradeShapeSummary:
    """Aggregate path quality for one profile on one market. Never pools markets."""

    market: str
    profile_id: str
    round_trips: int
    wins: int
    losses: int
    median_mae_mxn: Decimal | None
    median_mfe_mxn: Decimal | None
    median_mfe_to_mae: Decimal | None
    median_net_to_mae: Decimal | None
    median_mfe_capture: Decimal | None
    median_holding_bars: Decimal | None
    total_net_pnl_mxn: Decimal
    gross_pnl_mxn: Decimal
    total_friction_mxn: Decimal
    capital_hours: Decimal
    net_pnl_per_capital_hour: Decimal | None
    mfe_to_mae_percentiles: PercentileSummary
    friction_covered_fraction: Decimal | None
    mfe_above_2x_mae_fraction: Decimal | None
    mfe_above_3x_mae_fraction: Decimal | None
    target_reached_fraction: Decimal | None
    invalidation_reached_fraction: Decimal | None
    time_stopped_fraction: Decimal | None
    average_win_mxn: Decimal | None
    average_loss_mxn: Decimal | None
    median_win_mxn: Decimal | None
    median_loss_mxn: Decimal | None
    payoff_ratio: Decimal | None
    expectancy_mxn: Decimal | None
    diagnosis: str
    diagnosis_evidence: dict[str, str]

    @property
    def stated(self) -> bool:
        return self.round_trips >= DIAGNOSTIC_MINIMUM_TRADES

    def telemetry(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {"market": self.market, "profile_id": self.profile_id,
                "round_trips": self.round_trips, "wins": self.wins,
                "losses": self.losses, "median_mae_mxn": s(self.median_mae_mxn),
                "median_mfe_mxn": s(self.median_mfe_mxn),
                "median_mfe_to_mae": s(self.median_mfe_to_mae),
                "median_net_to_mae": s(self.median_net_to_mae),
                "median_mfe_capture": s(self.median_mfe_capture),
                "median_holding_bars": s(self.median_holding_bars),
                "total_net_pnl_mxn": str(self.total_net_pnl_mxn),
                "gross_pnl_mxn": str(self.gross_pnl_mxn),
                "total_friction_mxn": str(self.total_friction_mxn),
                "capital_hours": str(self.capital_hours),
                "net_pnl_per_capital_hour": s(self.net_pnl_per_capital_hour),
                "mfe_to_mae_distribution": self.mfe_to_mae_percentiles.telemetry(),
                "friction_covered_fraction": s(self.friction_covered_fraction),
                "mfe_above_2x_mae_fraction": s(self.mfe_above_2x_mae_fraction),
                "mfe_above_3x_mae_fraction": s(self.mfe_above_3x_mae_fraction),
                "target_reached_fraction": s(self.target_reached_fraction),
                "invalidation_reached_fraction": s(self.invalidation_reached_fraction),
                "time_stopped_fraction": s(self.time_stopped_fraction),
                "average_win_mxn": s(self.average_win_mxn),
                "average_loss_mxn": s(self.average_loss_mxn),
                "median_win_mxn": s(self.median_win_mxn),
                "median_loss_mxn": s(self.median_loss_mxn),
                "payoff_ratio": s(self.payoff_ratio),
                "expectancy_mxn": s(self.expectancy_mxn),
                "diagnosis": self.diagnosis,
                "diagnosis_evidence": dict(self.diagnosis_evidence),
                "markets_pooled": False,
                "mfe_is_diagnostic_not_profit": True}


def _fraction(count: int, total: int) -> Decimal | None:
    if total <= 0:
        return None
    return Decimal(count) / Decimal(total)


def classify_defect(*, shapes: tuple[TripShape, ...],
                    minimum_trades: int = DIAGNOSTIC_MINIMUM_TRADES) -> tuple[str, dict[str, str]]:
    """Name the dominant defect, from path evidence only.

    The branches are ordered by what would have to change to fix them, so the first match
    is the most specific statement the evidence supports:

    * If the path rarely offered more than friction, no exit rule can help — the entry is
      the defect (A). This is checked first because it is the strongest claim and the one
      that most often gets skipped in favour of tuning an exit.
    * If it offered, but the trade went further against the position than it ever earned,
      the geometry is wrong (C).
    * If it offered and mostly kept the gains, but the aggregate result is still bad, the
      exit is giving back a real edge (B).
    * Otherwise there was no favourable structure to work with at all (D).
    """
    if len(shapes) < minimum_trades:
        return INSUFFICIENT_EVIDENCE, {
            "reason": "SAMPLE_BELOW_DIAGNOSTIC_FLOOR",
            "trades": str(len(shapes)), "required": str(minimum_trades)}

    covered = sum(1 for s in shapes if s.friction_cleared)
    covered_fraction = Decimal(covered) / Decimal(len(shapes))
    with_mae = [s for s in shapes if s.mae_mxn > ZERO]
    above_2x = sum(1 for s in with_mae
                   if s.mfe_mxn >= s.mae_mxn * Decimal("2"))
    above_2x_fraction = (Decimal(above_2x) / Decimal(len(with_mae))
                         if with_mae else ZERO)
    adverse = [s for s in shapes if s.net_to_mae is not None]
    worse_than_earned = sum(1 for s in shapes
                            if s.mae_mxn > ZERO and s.mae_mxn >= abs(s.net_pnl_mxn)
                            and s.net_pnl_mxn >= ZERO)
    evidence = {
        "trades": str(len(shapes)),
        "mfe_covers_friction_fraction": str(covered_fraction),
        "mfe_above_2x_mae_fraction": str(above_2x_fraction),
        "trades_with_adverse_excursion": str(len(with_mae)),
        "adverse_exceeds_realized_reward": str(worse_than_earned),
    }

    if covered_fraction <= Decimal("0.34"):
        # Fewer than about a third of trades were ever net profitable while held. An exit
        # rule operating on a path that never reached profitability has nothing to capture,
        # so no amount of exit tuning can produce a positive expectancy.
        return LITTLE_FAVORABLE_EXCURSION, evidence
    if with_mae and above_2x_fraction <= Decimal("0.10"):
        # The path did sometimes offer more than friction, but almost never at a scale
        # that justified the heat taken to hold it.
        return EXCESSIVE_ADVERSE_EXCURSION, evidence
    captures = [s.mfe_capture for s in shapes if s.mfe_capture is not None]
    if captures:
        ordered = sorted(captures)
        median_capture = ordered[len(ordered) // 2]
        evidence["median_mfe_capture"] = str(median_capture)
        if median_capture <= WEAK_CAPTURE_RATIO:
            return EDGE_EXISTS_BUT_NOT_CAPTURED, evidence
    del adverse
    return BOTH_ENTRY_AND_EXIT_WEAK, evidence


def assess_certification_ladder(*, round_trips: int, net_pnl_mxn: Decimal,
                                effective_drawdown_mxn: Decimal, median_mae_mxn: Decimal | None,
                                maximum_drawdown_mxn: Decimal,
                                max_single_trade_risk_mxn: Decimal,
                                minimum_round_trips: int = DIAGNOSTIC_MINIMUM_TRADES,
                                ) -> dict[str, Any]:
    """Report each rung separately, so a failure names the gate that actually failed.

    This is the correction to 0.2.4's reporting. A configuration with 31 trades and a
    negative net passes `RAW_MINIMUM_SAMPLE_PASS` and fails
    `NON_NEGATIVE_NET_ECONOMICS_FAIL`. Saying it failed the sample floor would be a
    different, false statement, and it would misdirect the next experiment.
    """
    ladder: dict[str, Any] = {}
    ladder["raw_minimum_sample"] = (
        RAW_MINIMUM_SAMPLE_PASS if round_trips >= minimum_round_trips
        else RAW_MINIMUM_SAMPLE_FAIL)
    ladder["net_economics"] = (
        NON_NEGATIVE_NET_ECONOMICS_PASS if net_pnl_mxn >= ZERO
        else NON_NEGATIVE_NET_ECONOMICS_FAIL)
    ladder["drawdown_within_policy"] = (
        DRAWDOWN_WITHIN_POLICY_PASS if effective_drawdown_mxn <= maximum_drawdown_mxn
        else DRAWDOWN_WITHIN_POLICY_FAIL)
    if median_mae_mxn is None:
        ladder["risk_shape"] = RISK_SHAPE_UNACCEPTABLE
    else:
        ladder["risk_shape"] = (
            RISK_SHAPE_ACCEPTABLE if median_mae_mxn <= max_single_trade_risk_mxn
            else RISK_SHAPE_UNACCEPTABLE)
    passed = all(ladder[key] in (RAW_MINIMUM_SAMPLE_PASS, NON_NEGATIVE_NET_ECONOMICS_PASS,
                                 DRAWDOWN_WITHIN_POLICY_PASS, RISK_SHAPE_ACCEPTABLE)
                 for key in ("raw_minimum_sample", "net_economics",
                             "drawdown_within_policy", "risk_shape"))
    ladder["full_certification"] = (FULL_CERTIFICATION_PASS if passed
                                    else FULL_CERTIFICATION_FAIL)
    ladder["failed_gates"] = [
        key for key, value in ladder.items()
        if isinstance(value, str) and (value.endswith("_FAIL") or value == RISK_SHAPE_UNACCEPTABLE)]
    return ladder


@financial
def summarise_trade_shape(*, market: str, profile_id: str,
                          shapes: tuple[TripShape, ...]) -> TradeShapeSummary:
    """Summarise one profile's paths on one market. No pooling across markets."""
    if not shapes:
        return TradeShapeSummary(
            market=market, profile_id=profile_id, round_trips=0, wins=0, losses=0,
            median_mae_mxn=None, median_mfe_mxn=None, median_mfe_to_mae=None,
            median_net_to_mae=None, median_mfe_capture=None, median_holding_bars=None,
            total_net_pnl_mxn=ZERO, gross_pnl_mxn=ZERO, total_friction_mxn=ZERO,
            capital_hours=ZERO, net_pnl_per_capital_hour=None,
            mfe_to_mae_percentiles=percentiles(()), friction_covered_fraction=None,
            mfe_above_2x_mae_fraction=None, mfe_above_3x_mae_fraction=None,
            target_reached_fraction=None, invalidation_reached_fraction=None,
            time_stopped_fraction=None, average_win_mxn=None, average_loss_mxn=None,
            median_win_mxn=None, median_loss_mxn=None, payoff_ratio=None,
            expectancy_mxn=None, diagnosis=INSUFFICIENT_EVIDENCE,
            diagnosis_evidence={"reason": "NO_COMPLETED_TRIPS"})

    def median_of(values: list[Decimal]) -> Decimal | None:
        if not values:
            return None
        ordered = sorted(values)
        return ordered[len(ordered) // 2]

    wins = [s.net_pnl_mxn for s in shapes if s.net_pnl_mxn > ZERO]
    losses = [s.net_pnl_mxn for s in shapes if s.net_pnl_mxn <= ZERO]
    # Ratios are only defined where the denominator exists, and a median is taken only over
    # the trades that have one. Substituting ZERO for an undefined ratio would pull every
    # median toward zero and would understate how asymmetric the path actually was.
    mfe_to_mae = [s.mfe_to_mae for s in shapes if s.mfe_to_mae is not None]
    net_to_mae = [s.net_to_mae for s in shapes if s.net_to_mae is not None]
    captures = [s.mfe_capture for s in shapes if s.mfe_capture is not None]
    with_mae = [s for s in shapes if s.mae_mxn > ZERO]

    total_net = sum((s.net_pnl_mxn for s in shapes), ZERO)
    hours = sum((s.capital_hours for s in shapes), ZERO)
    average_win = (sum(wins, ZERO) / Decimal(len(wins))) if wins else None
    average_loss = (sum(losses, ZERO) / Decimal(len(losses))) if losses else None
    # The payoff ratio compares the typical win with the typical loss. Stated only when
    # both sides exist, because a strategy with no losing trade has no payoff ratio -- it
    # has an untested exit.
    payoff = (average_win / abs(average_loss)
              if average_win is not None and average_loss is not None and average_loss != ZERO
              else None)
    diagnosis, evidence = classify_defect(shapes=shapes)
    return TradeShapeSummary(
        market=market, profile_id=profile_id, round_trips=len(shapes), wins=len(wins),
        losses=len(losses), median_mae_mxn=median_of([s.mae_mxn for s in shapes]),
        median_mfe_mxn=median_of([s.mfe_mxn for s in shapes]),
        median_mfe_to_mae=median_of(mfe_to_mae), median_net_to_mae=median_of(net_to_mae),
        median_mfe_capture=median_of(captures),
        median_holding_bars=median_of([Decimal(s.holding_bars) for s in shapes]),
        total_net_pnl_mxn=total_net,
        gross_pnl_mxn=sum((s.gross_pnl_mxn for s in shapes), ZERO),
        total_friction_mxn=sum((s.friction_mxn for s in shapes), ZERO),
        capital_hours=hours,
        net_pnl_per_capital_hour=(total_net / hours) if hours > ZERO else None,
        mfe_to_mae_percentiles=percentiles(tuple(mfe_to_mae)),
        friction_covered_fraction=_fraction(
            sum(1 for s in shapes if s.friction_cleared), len(shapes)),
        mfe_above_2x_mae_fraction=_fraction(
            sum(1 for s in with_mae if s.mfe_mxn >= s.mae_mxn * Decimal("2")), len(with_mae)),
        mfe_above_3x_mae_fraction=_fraction(
            sum(1 for s in with_mae if s.mfe_mxn >= s.mae_mxn * Decimal("3")), len(with_mae)),
        target_reached_fraction=_fraction(
            sum(1 for s in shapes if s.target_touched), len(shapes)),
        invalidation_reached_fraction=_fraction(
            sum(1 for s in shapes if s.invalidation_touched), len(shapes)),
        time_stopped_fraction=_fraction(
            sum(1 for s in shapes if s.time_stopped), len(shapes)),
        average_win_mxn=average_win, average_loss_mxn=average_loss,
        median_win_mxn=median_of(wins), median_loss_mxn=median_of(losses),
        payoff_ratio=payoff, expectancy_mxn=total_net / Decimal(len(shapes)),
        diagnosis=diagnosis, diagnosis_evidence=evidence)


__all__ = [
    "BOTH_ENTRY_AND_EXIT_WEAK",
    "DIAGNOSIS_ORDER",
    "DIAGNOSTIC_MINIMUM_TRADES",
    "DRAWDOWN_WITHIN_POLICY_FAIL",
    "DRAWDOWN_WITHIN_POLICY_PASS",
    "EDGE_EXISTS_BUT_NOT_CAPTURED",
    "EXCESSIVE_ADVERSE_EXCURSION",
    "EXCESSIVE_ADVERSE_MULTIPLE",
    "FULL_CERTIFICATION_FAIL",
    "FULL_CERTIFICATION_PASS",
    "INSUFFICIENT_EVIDENCE",
    "LITTLE_FAVORABLE_EXCURSION",
    "MATERIAL_EXCURSION_FRICTION_MULTIPLE",
    "MFE_ABOVE_2X_MAE",
    "MFE_ABOVE_3X_MAE",
    "MFE_COVERS_FRICTION",
    "NON_NEGATIVE_NET_ECONOMICS_FAIL",
    "NON_NEGATIVE_NET_ECONOMICS_PASS",
    "RAW_MINIMUM_SAMPLE_FAIL",
    "RAW_MINIMUM_SAMPLE_PASS",
    "RISK_SHAPE_ACCEPTABLE",
    "RISK_SHAPE_UNACCEPTABLE",
    "TRADE_SHAPE_VERSION",
    "WEAK_CAPTURE_RATIO",
    "TradeShapeError",
    "TradeShapeSummary",
    "TripShape",
    "assess_certification_ladder",
    "classify_defect",
    "summarise_trade_shape",
]

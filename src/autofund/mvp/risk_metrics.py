"""Risk-path metrics for research: MAE, MFE, distributions and capital efficiency.

MVP 0.2.1 established that `volatility-mean-reversion-v1` risks roughly 0.34-0.85 MXN
of adverse excursion to capture roughly 0.04-0.20 MXN of net reward. The milestone
could see that because it measured unrealized drawdown at all. This module makes that
measurement a first-class, per-trade research metric so the *shape* of a trade's path
is reported alongside its outcome, and a challenger can be judged on whether its risk
path is compatible with the fee structure rather than only on where it finished.

Two definitions matter and are the easiest thing to get wrong:

**MAE (Maximum Adverse Excursion)** is the worst unrealized net loss the position
carried while open. "Net" because the position is valued as if it were closed right
then -- exit side, exit fee -- so the number is comparable to the realized P&L it is
set against. A pre-fee figure would flatter every trade.

**MFE (Maximum Favourite Excursion)** is the best unrealized net gain the position
carried while open. It answers a different question than MAE: not "how wrong did this
go" but "how much was on the table". The gap between MFE and the realized result is
the most honest available measure of how much of the available move a profile actually
captured, and a profile whose exits systematically give back most of its MFE is
leaking edge to its own exit rule.

**Path only, and only forward.** Every observation used here occurs strictly after the
entry fill. The path is accumulated during the replay, after each bar's decision has
already been taken, so nothing in this module can influence a signal. MAE and MFE are
therefore descriptive: they are never inputs to entry, exit or admission.

**Why percentiles and not just the worst.** A single worst MAE is one trip. Policy is
made on the ninth decile and the median, so both are reported, and a distribution
below the minimum sample reports `None` rather than a number that would look like
evidence. Reporting `p90` from two trades would be manufactured confidence, which is
the failure this project exists to avoid.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial

BPS = Decimal("10000")

# Distribution summaries need at least this many observations before any percentile is
# stated. Below it every field is None. Chosen to match the certification floor's
# spirit: five round trips is the smallest sample the project already treats as a
# floor, so a p90 from fewer would contradict an existing decision.
MINIMUM_PERCENTILE_SAMPLE = 5

MINUTES_PER_BAR_DEFAULT = 60


@dataclass(frozen=True, slots=True)
class PercentileSummary:
    """Nearest-rank distribution. Every field is None when the sample is too small.

    Nearest-rank (no interpolation) because an interpolated percentile would be a value
    that never occurred in the data, and this project does not report numbers that were
    not observed.
    """

    count: int
    minimum: Decimal | None = None
    p10: Decimal | None = None
    p50: Decimal | None = None
    p90: Decimal | None = None
    maximum: Decimal | None = None

    @property
    def stated(self) -> bool:
        return self.p50 is not None

    def telemetry(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {"count": self.count, "stated": self.stated,
                "min": s(self.minimum), "p10": s(self.p10), "p50": s(self.p50),
                "p90": s(self.p90), "max": s(self.maximum),
                "percentiles_withheld_below_sample": not self.stated}


@financial
def percentiles(values: tuple[Decimal, ...],
                minimum_sample: int = MINIMUM_PERCENTILE_SAMPLE) -> PercentileSummary:
    if not values:
        return PercentileSummary(0)
    ordered = sorted(values)
    if len(ordered) < minimum_sample:
        return PercentileSummary(len(ordered))

    def rank(fraction: Decimal) -> Decimal:
        index = int((Decimal(len(ordered) - 1) * fraction).to_integral_value())
        return ordered[max(0, min(index, len(ordered) - 1))]

    return PercentileSummary(count=len(ordered), minimum=ordered[0],
                             p10=rank(Decimal("0.10")), p50=rank(Decimal("0.50")),
                             p90=rank(Decimal("0.90")), maximum=ordered[-1])


@dataclass(frozen=True, slots=True)
class TradeRiskPath:
    """The forward-only path of one position, recorded per trip.

    `mae_*` and `mfe_*` are net-of-exit-fee counterfactual valuations: what the position
    would have realised had it been closed at that moment. They are never used as
    signals.
    """

    mae_mxn: Decimal
    mae_bps: Decimal
    mfe_mxn: Decimal
    mfe_bps: Decimal
    realised_gross_pnl_mxn: Decimal
    realised_net_pnl_mxn: Decimal
    holding_bars: int
    holding_minutes: int
    time_to_mae_bars: int
    time_to_mfe_bars: int
    exit_reason: str
    observations: int

    @property
    def mfe_capture_ratio(self) -> Decimal | None:
        """Realized net P&L as a fraction of MFE. None when there was no MFE to capture.

        This is the number that exposes a profile giving back its own gains: a low ratio
        with a positive MFE means the exit rule returned a favourable move to the market.
        """
        if self.mfe_mxn <= ZERO:
            return None
        return self.realised_net_pnl_mxn / self.mfe_mxn

    @property
    def mae_to_reward_ratio(self) -> Decimal | None:
        """MAE per unit of realized net reward. None when reward is not positive.

        The headline risk-efficiency figure. When this exceeds 1 the trade carried more
        adverse exposure than it ultimately earned, which is the defect 0.2.1 found in
        the mean-reversion family. Not defined for a losing trade, because dividing by a
        non-positive reward would produce a meaningless negative or infinite ratio.
        """
        if self.realised_net_pnl_mxn <= ZERO:
            return None
        return self.mae_mxn / self.realised_net_pnl_mxn

    @property
    def capital_hours(self) -> Decimal:
        """Capital-time the position occupied, in MXN-hours."""
        return Decimal(self.holding_minutes) / Decimal("60")

    def telemetry(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {"mae_mxn": str(self.mae_mxn), "mae_bps": str(self.mae_bps),
                "mfe_mxn": str(self.mfe_mxn), "mfe_bps": str(self.mfe_bps),
                "realised_gross_pnl_mxn": str(self.realised_gross_pnl_mxn),
                "realised_net_pnl_mxn": str(self.realised_net_pnl_mxn),
                "holding_bars": self.holding_bars, "holding_minutes": self.holding_minutes,
                "time_to_mae_bars": self.time_to_mae_bars,
                "time_to_mfe_bars": self.time_to_mfe_bars,
                "exit_reason": self.exit_reason, "path_observations": self.observations,
                "mfe_capture_ratio": s(self.mfe_capture_ratio),
                "mae_to_reward_ratio": s(self.mae_to_reward_ratio),
                "path_is_forward_only": True}


@dataclass(frozen=True, slots=True)
class RiskAdjustedSummary:
    """Per-profile, per-market risk-adjusted research summary.

    Deliberately reports each market separately and never pools: combining a gain on one
    book with a loss on another would let a market-specific failure average away, which
    is the outcome this whole milestone is trying to avoid reporting.
    """

    market: str
    profile_id: str
    round_trips: int
    mae_mxn: PercentileSummary
    mfe_mxn: PercentileSummary
    realised_net_pnl_mxn: PercentileSummary
    holding_bars: PercentileSummary
    total_net_pnl_mxn: Decimal
    total_gross_pnl_mxn: Decimal
    total_friction_mxn: Decimal
    capital_hours: Decimal
    net_pnl_per_capital_hour: Decimal | None
    aggregate_mfe_capture_ratio: Decimal | None
    worst_mae_mxn: Decimal
    exit_reason_counts: dict[str, int]

    def telemetry(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {"market": self.market, "profile_id": self.profile_id,
                "round_trips": self.round_trips,
                "mae_mxn": self.mae_mxn.telemetry(),
                "mfe_mxn": self.mfe_mxn.telemetry(),
                "realised_net_pnl_mxn": self.realised_net_pnl_mxn.telemetry(),
                "holding_bars": self.holding_bars.telemetry(),
                "total_net_pnl_mxn": str(self.total_net_pnl_mxn),
                "total_gross_pnl_mxn": str(self.total_gross_pnl_mxn),
                "total_friction_mxn": str(self.total_friction_mxn),
                "capital_hours": str(self.capital_hours),
                "net_pnl_per_capital_hour": s(self.net_pnl_per_capital_hour),
                "aggregate_mfe_capture_ratio": s(self.aggregate_mfe_capture_ratio),
                "worst_mae_mxn": str(self.worst_mae_mxn),
                "exit_reason_counts": dict(sorted(self.exit_reason_counts.items())),
                "markets_pooled": False}


@financial
def summarise_risk_paths(*, market: str, profile_id: str,
                         paths: tuple[TradeRiskPath, ...],
                         friction_mxn: Decimal = ZERO,
                         gross_pnl_mxn: Decimal = ZERO,
                         minimum_sample: int = MINIMUM_PERCENTILE_SAMPLE,
                         ) -> RiskAdjustedSummary:
    """Summarise one profile's paths on one market. No pooling across markets."""
    if not paths:
        return RiskAdjustedSummary(
            market=market, profile_id=profile_id, round_trips=0,
            mae_mxn=percentiles((), minimum_sample=minimum_sample),
            mfe_mxn=percentiles((), minimum_sample=minimum_sample),
            realised_net_pnl_mxn=percentiles((), minimum_sample=minimum_sample),
            holding_bars=percentiles((), minimum_sample=minimum_sample),
            total_net_pnl_mxn=ZERO, total_gross_pnl_mxn=gross_pnl_mxn,
            total_friction_mxn=friction_mxn, capital_hours=ZERO,
            net_pnl_per_capital_hour=None, aggregate_mfe_capture_ratio=None,
            worst_mae_mxn=ZERO, exit_reason_counts={})

    net = sum((path.realised_net_pnl_mxn for path in paths), ZERO)
    hours = sum((path.capital_hours for path in paths), ZERO)
    mfe_total = sum((path.mfe_mxn for path in paths if path.mfe_mxn > ZERO), ZERO)
    counts: dict[str, int] = {}
    for path in paths:
        counts[path.exit_reason] = counts.get(path.exit_reason, 0) + 1
    return RiskAdjustedSummary(
        market=market, profile_id=profile_id, round_trips=len(paths),
        mae_mxn=percentiles(tuple(p.mae_mxn for p in paths), minimum_sample=minimum_sample),
        mfe_mxn=percentiles(tuple(p.mfe_mxn for p in paths), minimum_sample=minimum_sample),
        realised_net_pnl_mxn=percentiles(tuple(p.realised_net_pnl_mxn for p in paths),
                                         minimum_sample=minimum_sample),
        holding_bars=percentiles(tuple(Decimal(p.holding_bars) for p in paths),
                                 minimum_sample=minimum_sample),
        total_net_pnl_mxn=net, total_gross_pnl_mxn=gross_pnl_mxn,
        total_friction_mxn=friction_mxn, capital_hours=hours,
        # None rather than a division by zero when no position ever opened.
        net_pnl_per_capital_hour=(net / hours) if hours > ZERO else None,
        aggregate_mfe_capture_ratio=(net / mfe_total) if mfe_total > ZERO else None,
        worst_mae_mxn=max((p.mae_mxn for p in paths), default=ZERO),
        exit_reason_counts=counts)


__all__ = [
    "MINIMUM_PERCENTILE_SAMPLE",
    "MINUTES_PER_BAR_DEFAULT",
    "PercentileSummary",
    "RiskAdjustedSummary",
    "TradeRiskPath",
    "percentiles",
    "summarise_risk_paths",
]

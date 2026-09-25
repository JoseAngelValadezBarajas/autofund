"""Predeclared, small parameter exploration for the 0.2.2 challengers.

The discipline in this module is the point of it. A wide grid search whose winner is
reported after the fact is not research: the reported configuration is then the one that
happened to fit the sample, and its holdout result is contaminated by the search even
though the holdout data was never touched.

Three rules are therefore enforced structurally rather than by convention:

**Small and predeclared.** The configuration set is a module-level constant, written
before any evaluation ran. It is not generated from a range, so the number of tested
configurations cannot grow to fit the data.

**Everything is recorded.** Every configuration that was evaluated appears in the results,
with its outcome, including the ones that performed badly. There is no code path that
keeps only the winner.

**Selection is risk-first, not profit-first.** The ranking is lexicographic and puts the
risk gates ahead of P&L, so a configuration cannot win by earning more while carrying
worse adverse excursion. Net P&L is only the last tie-break. This is the specific
behaviour the milestone asked for, and it is expressed as data (`SELECTION_RULE`) so a
reader can check it rather than trust it.

Parameter exploration happens in DEVELOPMENT only. Once a configuration is frozen, it is
evaluated on HOLDOUT without modification; any parameter change produces a different
identity and therefore a different challenger, invalidating the earlier holdout.
"""

from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial

from .executable_replay import DEFAULT_MIN_ROUND_TRIPS, ExecutableReplayResult
from .profiles import TargetModel

RESEARCH_VERSION = "autofund.challenger-research.v1"

# The predeclared configuration set. Four per challenger, chosen to probe the axes the
# hypothesis actually names rather than to fill a space.
#
# The spec's research question is whether AutoFund can find "larger dislocations or
# momentum expansions where the expected executable move exceeds fees while invalidating
# the position early enough to keep adverse excursion within current risk policy". That
# names two axes explicitly -- the size of the expected move and how early the position is
# invalidated -- so both are in the set. Leaving the target floor out would have made the
# search unable to test its own hypothesis, because at 173 bps round-trip friction the
# target floor is what decides whether a move can pay for itself at all.
#
# Written out literally, not generated, so the set cannot silently grow to fit the data.
VOLATILITY_MR_V2_CONFIGS: tuple[dict[str, str], ...] = (
    {"displacement_atr_multiple": "2.0", "stop_floor_bps": "80", "target_floor_bps": "250",
     "max_holding_bars": "240"},
    {"displacement_atr_multiple": "2.5", "stop_floor_bps": "100", "target_floor_bps": "400",
     "max_holding_bars": "240"},
    {"displacement_atr_multiple": "3.0", "stop_floor_bps": "120", "target_floor_bps": "600",
     "max_holding_bars": "480"},
    {"displacement_atr_multiple": "3.5", "stop_floor_bps": "150", "target_floor_bps": "800",
     "max_holding_bars": "480"},
)

RANGE_EXPANSION_CONFIGS: tuple[dict[str, str], ...] = (
    {"expansion_atr_multiple": "1.5", "stop_floor_bps": "100", "target_floor_bps": "300",
     "max_holding_bars": "180"},
    {"expansion_atr_multiple": "1.5", "stop_floor_bps": "100", "target_floor_bps": "500",
     "max_holding_bars": "180"},
    {"expansion_atr_multiple": "2.0", "stop_floor_bps": "120", "target_floor_bps": "500",
     "max_holding_bars": "360"},
    {"expansion_atr_multiple": "2.0", "stop_floor_bps": "150", "target_floor_bps": "700",
     "max_holding_bars": "360"},
)

CONFIG_SETS: dict[str, tuple[dict[str, str], ...]] = {
    "volatility-mean-reversion-v2": VOLATILITY_MR_V2_CONFIGS,
    "range-expansion-v1": RANGE_EXPANSION_CONFIGS,
}

# The selection rule, as data. Lexicographic, in this order:
#   1. satisfies the risk gates at all (drawdown within policy, risk-adjusted entry used)
#   2. lower median MAE per unit of realized reward (None/empty sorts worst)
#   3. lower effective drawdown
#   4. higher net P&L -- last, and only as a tie-break
SELECTION_RULE: tuple[str, ...] = (
    "risk_gates_satisfied",
    "mae_to_reward_ratio_ascending",
    "effective_drawdown_ascending",
    "net_pnl_descending",
)


def config_identity_token(profile_id: str, config: dict[str, str]) -> str:
    """A stable token identifying one tested configuration."""
    parts = ",".join(f"{key}={config[key]}" for key in sorted(config))
    return f"{profile_id}[{parts}]"


def build_evaluator(profile_id: str, config: dict[str, str]) -> Any:
    """Instantiate a challenger with one predeclared configuration applied.

    `target_floor_bps` is not a dataclass field: it lives inside the profile's frozen
    `TargetModel`. It is handled explicitly by deriving a new target model from the base
    one, so the profile's other target bounds are preserved rather than silently reset.

    An unknown key raises instead of being silently ignored, which would make two
    different configurations share behaviour and their results indistinguishable.
    """
    from .profile_library import RangeExpansionV1, VolatilityMeanReversionV2

    classes: dict[str, Any] = {
        "volatility-mean-reversion-v2": VolatilityMeanReversionV2,
        "range-expansion-v1": RangeExpansionV1,
    }
    cls = classes.get(profile_id)
    if cls is None:
        raise KeyError(f"NOT_A_RESEARCH_CHALLENGER:{profile_id}")
    declared = set(cls.__dataclass_fields__)
    unknown = sorted(set(config) - declared - {"target_floor_bps"})
    if unknown:
        raise KeyError(f"UNDECLARED_PARAMETERS:{','.join(unknown)}")
    kwargs: dict[str, Any] = {}
    for key, value in config.items():
        if key == "target_floor_bps":
            continue
        field_type = cls.__dataclass_fields__[key].type
        kwargs[key] = Decimal(value) if field_type in (Decimal, "Decimal") else int(value)
    if "target_floor_bps" in config:
        floor = Decimal(config["target_floor_bps"])
        base = cls().target_model
        if floor > base.cap_bps:
            raise KeyError(f"TARGET_FLOOR_ABOVE_CAP:{floor}>{base.cap_bps}")
        kwargs["target_model"] = TargetModel(
            atr_multiple=base.atr_multiple, floor_bps=floor, cap_bps=base.cap_bps)
    return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class SearchRecord:
    """One evaluated configuration, with its development outcome. Never discarded."""

    profile_id: str
    token: str
    config: dict[str, str]
    profile_fingerprint: str
    round_trips: int
    net_pnl_mxn: Decimal
    effective_drawdown_mxn: Decimal
    median_mae_mxn: Decimal | None
    p90_mae_mxn: Decimal | None
    median_mfe_mxn: Decimal | None
    mae_to_reward_ratio: Decimal | None
    economic_rejects: int
    risk_adjusted_rejects: int
    capital_hours: Decimal
    net_pnl_per_capital_hour: Decimal | None
    exit_reason_counts: dict[str, int]
    median_holding_bars: Decimal | None
    selection_rank_key: tuple[Any, ...] = field(default=())

    @property
    def risk_gates_satisfied(self) -> bool:
        return self.effective_drawdown_mxn <= Decimal("0.50")

    def telemetry(self) -> dict[str, Any]:
        def s(value: Decimal | None) -> str | None:
            return None if value is None else str(value)

        return {"research_version": RESEARCH_VERSION, "profile_id": self.profile_id,
                "token": self.token, "config": dict(self.config),
                "profile_fingerprint": self.profile_fingerprint,
                "round_trips": self.round_trips, "net_pnl_mxn": str(self.net_pnl_mxn),
                "effective_drawdown_mxn": str(self.effective_drawdown_mxn),
                "median_mae_mxn": s(self.median_mae_mxn),
                "p90_mae_mxn": s(self.p90_mae_mxn),
                "median_mfe_mxn": s(self.median_mfe_mxn),
                "mae_to_reward_ratio": s(self.mae_to_reward_ratio),
                "economic_rejects": self.economic_rejects,
                "risk_adjusted_rejects": self.risk_adjusted_rejects,
                "capital_hours": str(self.capital_hours),
                "net_pnl_per_capital_hour": s(self.net_pnl_per_capital_hour),
                "exit_reason_counts": dict(sorted(self.exit_reason_counts.items())),
                "median_holding_bars": s(self.median_holding_bars),
                "risk_gates_satisfied": self.risk_gates_satisfied,
                "sample_floor_met": self.round_trips >= DEFAULT_MIN_ROUND_TRIPS}


@financial
def _ratio_of(result: ExecutableReplayResult) -> Decimal | None:
    """Median MAE per unit of realized net reward, or None when reward is not positive.

    Uses the median rather than the worst trade so one outlier cannot decide the ranking,
    and returns None rather than a negative ratio when the profile lost money: a
    "risk per reward" figure is undefined without a reward.
    """
    if result.net_pnl_mxn <= ZERO:
        return None
    median_mae = result.median_mae_mxn
    if median_mae is None or median_mae <= ZERO:
        return ZERO
    return median_mae / result.net_pnl_mxn


def _rank_key(record: SearchRecord) -> tuple[Any, ...]:
    """The predeclared lexicographic ranking. See SELECTION_RULE."""
    return (
        0 if record.risk_gates_satisfied else 1,
        # A missing ratio is worse than any measured one, and is sorted last among ratios.
        1 if record.mae_to_reward_ratio is None else 0,
        record.mae_to_reward_ratio if record.mae_to_reward_ratio is not None else ZERO,
        record.effective_drawdown_mxn,
        -record.net_pnl_mxn,
        record.token,
    )


def record_from_result(*, profile_id: str, config: dict[str, str],
                       result: ExecutableReplayResult) -> SearchRecord:
    holding = result.median_holding_bars
    record = SearchRecord(
        profile_id=profile_id, token=config_identity_token(profile_id, config),
        config=dict(config), profile_fingerprint=result.profile_fingerprint,
        round_trips=len(result.trips), net_pnl_mxn=result.net_pnl_mxn,
        effective_drawdown_mxn=result.effective_drawdown_mxn,
        median_mae_mxn=result.median_mae_mxn, p90_mae_mxn=result.p90_mae_mxn,
        median_mfe_mxn=result.median_mfe_mxn, mae_to_reward_ratio=_ratio_of(result),
        economic_rejects=result.economic_rejects,
        risk_adjusted_rejects=result.risk_adjusted_rejects,
        capital_hours=result.capital_hours,
        net_pnl_per_capital_hour=result.net_pnl_per_capital_hour,
        exit_reason_counts=result.exit_reason_counts, median_holding_bars=holding)
    return replace(record, selection_rank_key=_rank_key(record))


def select_configuration(records: tuple[SearchRecord, ...]) -> SearchRecord | None:
    """The winner under the predeclared rule, or None when nothing was evaluated.

    Returns the best-ranked record rather than only a "passing" one: the milestone needs to
    report *how* the best configuration failed, and refusing to name anything would hide
    that. Whether the winner is actually acceptable is a separate question, answered by the
    holdout evaluation, not by this function.
    """
    if not records:
        return None
    return min(records, key=lambda record: record.selection_rank_key)


__all__ = [
    "CONFIG_SETS",
    "RANGE_EXPANSION_CONFIGS",
    "RESEARCH_VERSION",
    "SELECTION_RULE",
    "VOLATILITY_MR_V2_CONFIGS",
    "SearchRecord",
    "build_evaluator",
    "config_identity_token",
    "record_from_result",
    "select_configuration",
]

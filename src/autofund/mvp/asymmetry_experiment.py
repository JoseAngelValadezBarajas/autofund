"""Freezes the asymmetric-opportunity experiment before any result is inspected.

The manifest mechanism is carried forward from 0.2.4 unchanged in spirit and extended in one
respect that this milestone needs: it now freezes the **reward/risk threshold band** as well
as the parameters.

That extension is the point. Section 9 of the specification forbids assuming 2:1 or 3:1 is
correct and requires the candidate range to come from development evidence and be frozen
before the holdout. A threshold that could be adjusted after seeing results would make the
entire experiment unfalsifiable — any failing configuration could be re-thresholded until
something passed. So the band, the per-challenger configuration sets, and the comparison
semantics used to claim "materially improved" are all recorded here, and the store refuses to
rewrite the file once it exists.

**Why the band is 0.5 to 2.0 and not higher.** Derived, not chosen. The unchanged risk gate
nets friction from both paths and so requires `gross >= friction + ratio * (risk + friction)`.
At the confirmed 173 bps round trip and a 260 bps invalidation, the required gross move is
~606 bps at ratio 1.0 and ~866 bps at ratio 2.0 — roughly 21 and 30 times a 15m ATR of 29 bps.
A band extending to 3.0 would commit budget to thresholds the measured excursion distribution
cannot reach, which spends the experiment on a question already answered. That reasoning is
recorded in the artifact so a later reader can check whether the band was defensible rather
than merely narrow.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.replay.serialization import fingerprint

from .asymmetric_challengers import (
    DEFAULT_TARGET_ATR_CAP_MULTIPLE,
    ExpansionRetestV1,
    StructuralInvalidationPullbackV1,
    asymmetric_challenger_ids,
)
from .asymmetry_gate import (
    PREDECLARED_REWARD_RISK_THRESHOLDS,
    friction_bps_for,
    maximum_risk_bps,
)
from .horizon import FIFTEEN_MINUTE, ONE_HOUR_TIMEFRAME

ASYMMETRY_EXPERIMENT_VERSION = "autofund.asymmetry-experiment.v1"
ASYMMETRY_MANIFEST_FILE = "ASYMMETRY_EXPERIMENT_MANIFEST.json"

# Result classifications. `NO_CURRENT_EDGE` and `INSUFFICIENT_EVIDENCE` are genuinely
# different findings and are kept apart for the same reason 0.2.4 kept them apart: a
# measured negative is a result, an unmeasured one is a defect in the experiment.
FORWARD_SHADOW_CANDIDATE_FOUND = "FORWARD_SHADOW_CANDIDATE_FOUND"
NO_CURRENT_EDGE = "NO_CURRENT_EDGE"
INSUFFICIENT_ASYMmetry_EVIDENCE = "INSUFFICIENT_EVIDENCE"
ASYMMETRY_BLOCKED = "BLOCKED"

# Configurations per challenger. The spec's ceiling is four.
MAX_CONFIGURATIONS_PER_CHALLENGER = 4

# The horizon pair 0.2.4 measured. No timeframe search: section 14 forbids reopening it.
PREDECLARED_HORIZONS = (FIFTEEN_MINUTE, ONE_HOUR_TIMEFRAME)

# The comparison semantics for "materially improved", predeclared (section 22).
#
# Deliberately conjunctive and stated in terms of *shape* rather than profit, because profit
# on a small holdout sample is exactly what a shape comparison is meant to avoid relying on.
# A challenger claims improvement only by also improving the path it travelled to get there.
COMPARISON_SEMANTICS: tuple[str, ...] = (
    "risk_gates_satisfied_on_holdout",
    "median_mfe_to_mae_strictly_greater_than_predecessor",
    "median_net_to_mae_not_worse_than_predecessor",
    "holdout_net_pnl_non_negative",
)

# The frozen predecessor each concept is compared against. Named here so the comparison
# target cannot be selected after the holdout is known.
PREDECESSOR_BY_CONCEPT: dict[str, str] = {
    "structural_invalidation_pullback": "volatility-mean-reversion-v2",
    "expansion_retest": "range-expansion-v1",
}

SELECTION_RULE: tuple[str, ...] = (
    "risk_gates_satisfied",
    "median_mfe_to_mae_descending",
    "median_net_to_mae_descending",
    "net_pnl_descending",
)


class AsymmetryExperimentError(ValueError):
    """The experiment was asked for something it cannot defend."""


@dataclass(frozen=True, slots=True)
class AsymmetryConfig:
    """One fully specified challenger configuration.

    The varying parameters are the ones the arithmetic says matter: how far the invalidation
    sits (which sets the risk term) and how large a reward multiple is demanded on top of
    break-even. `max_risk_bps` is the binding constraint on whether the geometry can satisfy
    the unchanged risk policy at all, so it varies across the set rather than being fixed.
    """

    label: str
    max_risk_bps: Decimal
    max_holding_bars: int
    expected_holding_horizon: int
    invalidation_buffer_fraction: Decimal
    target_margin_risk_multiple: Decimal

    def __post_init__(self) -> None:
        if not self.label:
            raise AsymmetryExperimentError("configuration requires a label")
        if self.max_risk_bps <= 0 or self.max_holding_bars <= 0:
            raise AsymmetryExperimentError("risk and holding limits must be positive")
        if self.expected_holding_horizon > self.max_holding_bars:
            raise AsymmetryExperimentError(
                "expected holding horizon cannot exceed the holding limit")
        if not (0 < self.invalidation_buffer_fraction < 1):
            raise AsymmetryExperimentError("buffer fraction must lie within (0, 1)")

    def public(self, *, concept: str, timeframe_name: str) -> dict[str, Any]:
        params = self.parameters(timeframe_name=timeframe_name)
        return {"label": self.label, "concept": concept, "timeframe": timeframe_name,
                "topic": "ASYMMETRY_CONFIG", "parameters": [list(p) for p in params],
                "fingerprint": fingerprint({"label": self.label, "concept": concept,
                                            "params": params})}

    def parameters(self, *, timeframe_name: str) -> tuple[tuple[str, str], ...]:
        return (("timeframe", timeframe_name), ("max_risk_bps", str(self.max_risk_bps)),
                ("max_holding_bars", str(self.max_holding_bars)),
                ("expected_holding_horizon", str(self.expected_holding_horizon)),
                ("invalidation_buffer_fraction",
                 str(self.invalidation_buffer_fraction)),
                ("target_margin_risk_multiple", str(self.target_margin_risk_multiple)),
                ("target_atr_cap_multiple", str(DEFAULT_TARGET_ATR_CAP_MULTIPLE)))


def pullback_configs() -> tuple[AsymmetryConfig, ...]:
    """Four invalidation widths for the structural pullback concept.

    The set walks the risk term from narrow to the widest the 0.50 MXN cap permits, because
    the arithmetic says the required gross move grows by the full width of the stop. The
    narrowest configuration is the one most likely to trade; the widest is the one that can
    reach a larger reward, and the experiment measures whether either actually produces
    asymmetric paths rather than assuming the trade-off.
    """
    return (
        AsymmetryConfig(label="PB-A", max_risk_bps=Decimal("120"),
                        max_holding_bars=16, expected_holding_horizon=8,
                        invalidation_buffer_fraction=Decimal("0.30"),
                        target_margin_risk_multiple=Decimal("0.5")),
        AsymmetryConfig(label="PB-B", max_risk_bps=Decimal("180"),
                        max_holding_bars=20, expected_holding_horizon=10,
                        invalidation_buffer_fraction=Decimal("0.35"),
                        target_margin_risk_multiple=Decimal("0.5")),
        AsymmetryConfig(label="PB-C", max_risk_bps=Decimal("80"),
                        max_holding_bars=12, expected_holding_horizon=6,
                        invalidation_buffer_fraction=Decimal("0.25"),
                        target_margin_risk_multiple=Decimal("0.4")),
        AsymmetryConfig(label="PB-D", max_risk_bps=Decimal("260"),
                        max_holding_bars=24, expected_holding_horizon=12,
                        invalidation_buffer_fraction=Decimal("0.40"),
                        target_margin_risk_multiple=Decimal("0.6")),
    )


def retest_configs() -> tuple[AsymmetryConfig, ...]:
    """Four invalidation widths for the expansion-retest concept."""
    return (
        AsymmetryConfig(label="RT-A", max_risk_bps=Decimal("120"),
                        max_holding_bars=16, expected_holding_horizon=8,
                        invalidation_buffer_fraction=Decimal("0.30"),
                        target_margin_risk_multiple=Decimal("0.5")),
        AsymmetryConfig(label="RT-B", max_risk_bps=Decimal("180"),
                        max_holding_bars=20, expected_holding_horizon=10,
                        invalidation_buffer_fraction=Decimal("0.35"),
                        target_margin_risk_multiple=Decimal("0.5")),
        AsymmetryConfig(label="RT-C", max_risk_bps=Decimal("80"),
                        max_holding_bars=12, expected_holding_horizon=6,
                        invalidation_buffer_fraction=Decimal("0.25"),
                        target_margin_risk_multiple=Decimal("0.4")),
        AsymmetryConfig(label="RT-D", max_risk_bps=Decimal("260"),
                        max_holding_bars=24, expected_holding_horizon=12,
                        invalidation_buffer_fraction=Decimal("0.40"),
                        target_margin_risk_multiple=Decimal("0.6")),
    )


CONFIG_SETS: dict[str, tuple[AsymmetryConfig, ...]] = {
    "structural_invalidation_pullback": pullback_configs(),
    "expansion_retest": retest_configs(),
}


def build_challenger(*, concept: str, timeframe: Any, config: AsymmetryConfig) -> Any:
    """Build one challenger instance from a configuration.

    Every configuration field reaches the evaluator. A field that silently did not would make
    the four configurations identical while still reporting four — the exact defect found and
    fixed in 0.2.4, where a dropped target floor produced `sig=304 econ=304` four times over
    and a search that never searched.
    """
    common: dict[str, Any] = {
        "timeframe_name": timeframe.name,
        "config_label": config.label,
        "max_risk_bps": config.max_risk_bps,
        "max_holding_bars": config.max_holding_bars,
        "expected_holding_horizon": config.expected_holding_horizon,
        "invalidation_buffer_fraction": config.invalidation_buffer_fraction,
        "target_margin_risk_multiple": config.target_margin_risk_multiple,
    }
    if concept == "structural_invalidation_pullback":
        return StructuralInvalidationPullbackV1(**common)
    if concept == "expansion_retest":
        return ExpansionRetestV1(**common)
    raise AsymmetryExperimentError(f"UNKNOWN_CONCEPT:{concept}")


def configuration_budget() -> tuple[tuple[str, int], ...]:
    """Configurations per (concept, timeframe), which is the unit the spec bounds."""
    return tuple((f"{concept}:{tf.name}", len(configs))
                 for concept, configs in CONFIG_SETS.items()
                 for tf in PREDECLARED_HORIZONS)


def predeclared_configuration_count() -> int:
    return sum(len(configs) for configs in CONFIG_SETS.values()) * len(PREDECLARED_HORIZONS)


@dataclass(frozen=True, slots=True)
class AsymmetryExperimentManifest:
    """Frozen inputs of the asymmetric-opportunity experiment."""

    baseline_commit: str
    product_version: str
    code_commit: str
    created_at: str

    horizons: tuple[tuple[str, int], ...]
    challenger_fingerprints: tuple[tuple[str, str], ...]
    configuration_budget: tuple[tuple[str, int], ...]
    configuration_parameters: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]

    reward_risk_thresholds: tuple[str, ...]
    reward_risk_threshold_basis: str
    comparison_semantics: tuple[str, ...]
    predecessor_by_concept: tuple[tuple[str, str], ...]

    economic_policy_version: str
    economic_policy_fingerprint: str
    minimum_net_profit_mxn: Decimal
    minimum_net_edge_bps: Decimal

    risk_policy_fingerprint: str
    max_drawdown_mxn: Decimal
    max_single_trade_risk_mxn: Decimal
    minimum_reward_risk_ratio: Decimal
    capital_policy_fingerprint: str
    single_order_cap_mxn: Decimal
    authorized_capital_mxn: Decimal

    execution_model_version: str
    certifying_execution_mode: str
    account_maker_fee_rate: Decimal
    account_taker_fee_rate: Decimal
    modelled_spread_bps: Decimal
    modelled_slippage_bps: Decimal
    friction_bps: Decimal
    maximum_risk_bps_allowed: Decimal

    dataset_cutoff_ms: int
    development_window: tuple[str, int, int]
    holdout_window: tuple[str, int, int]
    bar_base_seconds: int
    parameter_freeze_diagnosis: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not self.baseline_commit or not self.code_commit:
            raise AsymmetryExperimentError("baseline and code commit are required")
        if tuple(item[0] for item in self.horizons) != ("15m", "1h"):
            raise AsymmetryExperimentError("exactly the 0.2.4 horizons may be used")
        for _, count in self.configuration_budget:
            if count > MAX_CONFIGURATIONS_PER_CHALLENGER:
                raise AsymmetryExperimentError(
                    f"configuration budget exceeds {MAX_CONFIGURATIONS_PER_CHALLENGER}")
        if self.certifying_execution_mode != "TAKER_TAKER":
            raise AsymmetryExperimentError(
                "only taker-taker execution may certify an asymmetry experiment")
        if self.max_drawdown_mxn != Decimal("0.50"):
            raise AsymmetryExperimentError("DRAWDOWN_WITHIN_POLICY must not be changed")
        if self.max_single_trade_risk_mxn != Decimal("0.50"):
            raise AsymmetryExperimentError("single-trade risk policy must not be changed")
        if self.holdout_window[2] > self.development_window[1]:
            raise AsymmetryExperimentError("holdout must precede development")
        if self.development_window[2] > self.dataset_cutoff_ms:
            raise AsymmetryExperimentError("development window may not cross the cutoff")
        if not self.reward_risk_thresholds:
            raise AsymmetryExperimentError("a reward/risk band must be predeclared")
        if not self.parameter_freeze_diagnosis:
            raise AsymmetryExperimentError(
                "the diagnostic that justified the challengers must be recorded")

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public())

    def public(self) -> dict[str, Any]:
        return {"version": ASYMMETRY_EXPERIMENT_VERSION,
                "product_version": self.product_version,
                "baseline_commit": self.baseline_commit, "code_commit": self.code_commit,
                "created_at": self.created_at,
                "horizons": [{"name": n, "seconds": s} for n, s in self.horizons],
                "challenger_fingerprints": [list(i) for i in self.challenger_fingerprints],
                "configuration_budget": dict(self.configuration_budget),
                "configuration_parameters": [
                    [k, [list(p) for p in v]] for k, v in self.configuration_parameters],
                "reward_risk_thresholds": list(self.reward_risk_thresholds),
                "reward_risk_threshold_basis": self.reward_risk_threshold_basis,
                "comparison_semantics": list(self.comparison_semantics),
                "predecessor_by_concept": dict(self.predecessor_by_concept),
                "economic_policy_version": self.economic_policy_version,
                "economic_policy_fingerprint": self.economic_policy_fingerprint,
                "minimum_net_profit_mxn": str(self.minimum_net_profit_mxn),
                "minimum_net_edge_bps": str(self.minimum_net_edge_bps),
                "risk_policy_fingerprint": self.risk_policy_fingerprint,
                "max_drawdown_mxn": str(self.max_drawdown_mxn),
                "max_single_trade_risk_mxn": str(self.max_single_trade_risk_mxn),
                "minimum_reward_risk_ratio": str(self.minimum_reward_risk_ratio),
                "capital_policy_fingerprint": self.capital_policy_fingerprint,
                "single_order_cap_mxn": str(self.single_order_cap_mxn),
                "authorized_capital_mxn": str(self.authorized_capital_mxn),
                "execution_model_version": self.execution_model_version,
                "certifying_execution_mode": self.certifying_execution_mode,
                "account_maker_fee_rate": str(self.account_maker_fee_rate),
                "account_taker_fee_rate": str(self.account_taker_fee_rate),
                "modelled_spread_bps": str(self.modelled_spread_bps),
                "modelled_slippage_bps": str(self.modelled_slippage_bps),
                "friction_bps": str(self.friction_bps),
                "maximum_risk_bps_allowed": str(self.maximum_risk_bps_allowed),
                "dataset_cutoff_ms": self.dataset_cutoff_ms,
                "development_window": list(self.development_window),
                "holdout_window": list(self.holdout_window),
                "bar_base_seconds": self.bar_base_seconds,
                "parameter_freeze_diagnosis": [list(i) for i in
                                               self.parameter_freeze_diagnosis],
                "selection_rule": list(SELECTION_RULE),
                "all_configurations_retained": True,
                "holdout_used_for_selection": False,
                "thresholds_chosen_before_results": True,
                "previous_research_frozen": True,
                "maker_may_certify": False}


class AsymmetryManifestStore:
    """Writes the manifest once and refuses to overwrite it."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.root / ASYMMETRY_MANIFEST_FILE

    def exists(self) -> bool:
        return self.path.exists()

    def freeze(self, manifest: AsymmetryExperimentManifest) -> dict[str, Any]:
        if self.path.exists():
            raise AsymmetryExperimentError("ASYMMETRY_MANIFEST_ALREADY_FROZEN")
        payload = manifest.public()
        payload["manifest_fingerprint"] = manifest.fingerprint
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return payload

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            raise AsymmetryExperimentError("ASYMMETRY_MANIFEST_MISSING")
        loaded: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        return loaded


def threshold_basis_text(*, friction_bps: Decimal, max_risk_bps: Decimal) -> str:
    """The recorded justification for the band, computed from the frozen policy."""
    lines = []
    for ratio in PREDECLARED_REWARD_RISK_THRESHOLDS:
        required = friction_bps + ratio * (max_risk_bps + friction_bps)
        lines.append(f"ratio {ratio}: gross >= {required:.1f} bps")
    return ("band derived from the unchanged gate, which nets friction from both paths: "
            "gross >= friction + ratio * (risk + friction). At friction "
            f"{friction_bps} bps and risk {max_risk_bps} bps -> " + "; ".join(lines)
            + ". A band beyond 2.0 was excluded because the required move exceeds the "
              "measured excursion distribution, so it would spend budget on a question "
              "already answered.")


def freeze_asymmetry_manifest(*, root: Path, baseline_commit: str, product_version: str,
                              code_commit: str, dataset_cutoff_ms: int,
                              development: tuple[str, int, int],
                              holdout: tuple[str, int, int], economic_policy: Any,
                              max_drawdown_mxn: Decimal,
                              max_single_trade_risk_mxn: Decimal,
                              minimum_reward_risk_ratio: Decimal,
                              single_order_cap_mxn: Decimal,
                              authorized_capital_mxn: Decimal,
                              execution_model_version: str, maker_fee_rate: Decimal,
                              taker_fee_rate: Decimal, spread_bps: Decimal,
                              slippage_bps: Decimal,
                              diagnosis: tuple[tuple[str, str], ...],
                              ) -> dict[str, Any]:
    """Freeze the experiment. Called before any window is evaluated."""
    friction = friction_bps_for(taker_fee_rate=taker_fee_rate, spread_bps=spread_bps,
                                slippage_bps=slippage_bps)
    risk_cap = maximum_risk_bps(budget_mxn=single_order_cap_mxn,
                                max_single_trade_risk_mxn=max_single_trade_risk_mxn,
                                friction_bps=friction)
    fingerprints = tuple(
        (build_challenger(concept=concept, timeframe=tf, config=configs[0])
         .identity.profile_id,
         build_challenger(concept=concept, timeframe=tf, config=configs[0])
         .identity.fingerprint)
        for concept, configs in CONFIG_SETS.items()
        for tf in PREDECLARED_HORIZONS)
    declared = tuple(
        (f"{concept}:{tf.name}:{config.label}", config.parameters(timeframe_name=tf.name))
        for concept, configs in CONFIG_SETS.items()
        for tf in PREDECLARED_HORIZONS for config in configs)
    manifest = AsymmetryExperimentManifest(
        baseline_commit=baseline_commit, product_version=product_version,
        code_commit=code_commit, created_at=datetime.now(UTC).isoformat(),
        horizons=tuple((tf.name, tf.seconds) for tf in PREDECLARED_HORIZONS),
        challenger_fingerprints=fingerprints, configuration_budget=configuration_budget(),
        configuration_parameters=declared,
        reward_risk_thresholds=tuple(str(t) for t in PREDECLARED_REWARD_RISK_THRESHOLDS),
        reward_risk_threshold_basis=threshold_basis_text(friction_bps=friction,
                                                         max_risk_bps=risk_cap),
        comparison_semantics=COMPARISON_SEMANTICS,
        predecessor_by_concept=tuple(sorted(PREDECESSOR_BY_CONCEPT.items())),
        economic_policy_version=str(getattr(economic_policy, "version", "")),
        economic_policy_fingerprint=fingerprint(
            economic_policy.public() if hasattr(economic_policy, "public")
            else str(economic_policy)),
        minimum_net_profit_mxn=Decimal(str(economic_policy.minimum_net_profit_mxn)),
        minimum_net_edge_bps=Decimal(str(economic_policy.minimum_net_edge_bps)),
        risk_policy_fingerprint=fingerprint(
            {"max_drawdown_mxn": str(max_drawdown_mxn),
             "max_single_trade_risk_mxn": str(max_single_trade_risk_mxn),
             "minimum_reward_risk_ratio": str(minimum_reward_risk_ratio)}),
        max_drawdown_mxn=max_drawdown_mxn,
        max_single_trade_risk_mxn=max_single_trade_risk_mxn,
        minimum_reward_risk_ratio=minimum_reward_risk_ratio,
        capital_policy_fingerprint=fingerprint(
            {"single_order_cap_mxn": str(single_order_cap_mxn),
             "authorized_capital_mxn": str(authorized_capital_mxn)}),
        single_order_cap_mxn=single_order_cap_mxn,
        authorized_capital_mxn=authorized_capital_mxn,
        execution_model_version=execution_model_version,
        certifying_execution_mode="TAKER_TAKER",
        account_maker_fee_rate=maker_fee_rate, account_taker_fee_rate=taker_fee_rate,
        modelled_spread_bps=spread_bps, modelled_slippage_bps=slippage_bps,
        friction_bps=friction, maximum_risk_bps_allowed=risk_cap,
        dataset_cutoff_ms=dataset_cutoff_ms, development_window=development,
        holdout_window=holdout, bar_base_seconds=60,
        parameter_freeze_diagnosis=diagnosis)
    return AsymmetryManifestStore(root).freeze(manifest)


__all__ = [
    "ASYMMETRY_BLOCKED",
    "ASYMMETRY_EXPERIMENT_VERSION",
    "ASYMMETRY_MANIFEST_FILE",
    "COMPARISON_SEMANTICS",
    "CONFIG_SETS",
    "FORWARD_SHADOW_CANDIDATE_FOUND",
    "MAX_CONFIGURATIONS_PER_CHALLENGER",
    "NO_CURRENT_EDGE",
    "PREDECESSOR_BY_CONCEPT",
    "PREDECLARED_HORIZONS",
    "SELECTION_RULE",
    "AsymmetryConfig",
    "AsymmetryExperimentError",
    "AsymmetryExperimentManifest",
    "AsymmetryManifestStore",
    "INSUFFICIENT_ASYMmetry_EVIDENCE",
    "asymmetric_challenger_ids",
    "build_challenger",
    "configuration_budget",
    "freeze_asymmetry_manifest",
    "predeclared_configuration_count",
    "threshold_basis_text",
]

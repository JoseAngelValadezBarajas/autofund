"""Freezes the time-horizon experiment before any result is inspected.

The mechanism this module implements exists because of a specific failure mode: a
researcher evaluates several horizons, notices which one looks best, and then reports that
one as though it had been the plan. The defence is not better intentions, it is a record
written too early to be influenced by the answer.

So the manifest is frozen first, refuses to be overwritten, and captures every input that
could move the outcome: the exact profile identities and parameters, both timeframes, the
economic and risk policies in force, the dataset cutoff, and the development and holdout
windows. If a field that can change the result is missing, that is a hole through which a
later tweak could inherit this experiment's evidence.

**The parameter budget is deliberately small and fully recorded.** At most four
configurations per concept and horizon, all of them retained in the result — not just the
winner. Reporting only the winner is the same failure as choosing the horizon after the
fact, one level down.

**Taker fees are the certifying basis.** The maker-fee bound is reported alongside because
it bounds how much of any failure is attributable to fees rather than to the idea, but it
can never decide the verdict. A verdict that depended on fills that were never observed
would be a claim about a market that was not measured.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.replay.serialization import fingerprint

from .horizon import FIFTEEN_MINUTE, ONE_HOUR_TIMEFRAME, TimeframeSpec
from .horizon_profiles import (
    MR_HORIZON_TARGET_MODEL,
    RANGE_HORIZON_TARGET_MODEL,
    horizon_profiles,
)
from .profiles import TargetModel

HORIZON_EXPERIMENT_VERSION = "autofund.time-horizon-experiment.v1"
HORIZON_MANIFEST_FILE = "TIME_HORIZON_EXPERIMENT_MANIFEST.json"

# Result classifications. Each is a genuinely different finding, and collapsing them would
# lose the distinction that matters most: "we measured and there is no edge" is a useful
# result, whereas "we could not measure it" is a defect in the experiment.
LONGER_HORIZON_PROMISING = "LONGER_HORIZON_PROMISING"
NO_HORIZON_EDGE = "NO_HORIZON_EDGE"
INSUFFICIENT_HORIZON_EVIDENCE = "INSUFFICIENT_HORIZON_EVIDENCE"
HORIZON_BLOCKED = "BLOCKED"

FORWARD_MICROSTRUCTURE_CAPTURE_READY = "FORWARD_MICROSTRUCTURE_CAPTURE_READY"
MICROSTRUCTURE_BLOCKED = "BLOCKED"

# The complete set of admissible time-horizon verdicts, in reporting order. Declared here so
# the research view can publish what a result *could* be without pretending to know which
# one it is: no verdict exists until the frozen experiment has been evaluated.
HORIZON_OUTCOMES: tuple[str, ...] = (
    LONGER_HORIZON_PROMISING, NO_HORIZON_EDGE, INSUFFICIENT_HORIZON_EVIDENCE,
    HORIZON_BLOCKED)

# The certifying execution mode. Passive modes are reported for sensitivity only.
CERTIFYING_EXECUTION_MODE = "TAKER_TAKER"

# Configurations per (concept, horizon). The spec's ceiling is four; using exactly four for
# each is not required, but exceeding it is.
MAX_CONFIGURATIONS_PER_HORIZON = 4


class HorizonExperimentError(ValueError):
    """The experiment was asked for something it cannot defend."""


@dataclass(frozen=True, slots=True)
class HorizonConfig:
    """One fully specified configuration, identified by its own fingerprint.

    `label` is for reporting only and carries no authority; selection reads the declared
    rule, never the label.
    """

    label: str
    stop_atr_multiple: Decimal
    target_floor_bps: Decimal
    max_holding_bars: int
    expected_holding_horizon: int
    displacement_atr_multiple: Decimal = Decimal("2.0")
    expansion_atr_multiple: Decimal = Decimal("1.5")
    window: int = 21

    def __post_init__(self) -> None:
        if not self.label:
            raise HorizonExperimentError("configuration requires a label")
        if self.max_holding_bars <= 0 or self.expected_holding_horizon <= 0:
            raise HorizonExperimentError("holding limits must be positive")
        if self.stop_atr_multiple <= 0 or self.target_floor_bps <= 0:
            raise HorizonExperimentError("stop and target must be positive")
        if self.expected_holding_horizon > self.max_holding_bars:
            raise HorizonExperimentError(
                "expected holding horizon cannot exceed the declared holding limit")

    def parameters(self, *, timeframe: TimeframeSpec) -> tuple[tuple[str, str], ...]:
        return (("timeframe", timeframe.name), ("timeframe_seconds", str(timeframe.seconds)),
                ("window", str(self.window)),
                ("stop_atr_multiple", str(self.stop_atr_multiple)),
                ("target_floor_bps", str(self.target_floor_bps)),
                ("max_holding_bars", str(self.max_holding_bars)),
                ("expected_holding_horizon", str(self.expected_holding_horizon)))

    def target_model_for(self, concept: str) -> TargetModel:
        """The target model this configuration actually implies.

        Configurations differ in their target floor, so the floor must reach the evaluator.
        Leaving it at a module default would make four configurations behave identically —
        a search that reports a budget it never spent.
        """
        base = (MR_HORIZON_TARGET_MODEL if concept == "volatility_mean_reversion"
                else RANGE_HORIZON_TARGET_MODEL)
        return TargetModel(atr_multiple=base.atr_multiple,
                           floor_bps=self.target_floor_bps, cap_bps=base.cap_bps)

    def public(self, *, timeframe: TimeframeSpec, concept: str) -> dict[str, Any]:
        params = self.parameters(timeframe=timeframe)
        return {"label": self.label, "concept": concept, "timeframe": timeframe.name,
                "topic": "HORIZON_CONFIG", "parameters": [list(item) for item in params],
                "fingerprint": fingerprint({"label": self.label, "concept": concept,
                                            "params": params})}


def _mr_configs() -> tuple[HorizonConfig, ...]:
    """Four volatility-mean-reversion configurations, scaling with the horizon.

    The set varies the two things that decide whether a coarser horizon helps: how far the
    stop sits, and how large a target is demanded. Holding limits are expressed in bars of
    the horizon, so they mean a longer wall-clock period on 1h than on 15m — which is the
    entire point, and is why capital-hours must be reported rather than assumed benign.
    """
    return (
        HorizonConfig(label="MR-A", stop_atr_multiple=Decimal("1.0"),
                      target_floor_bps=Decimal("250"), max_holding_bars=16,
                      expected_holding_horizon=8),
        HorizonConfig(label="MR-B", stop_atr_multiple=Decimal("1.5"),
                      target_floor_bps=Decimal("300"), max_holding_bars=24,
                      expected_holding_horizon=12),
        HorizonConfig(label="MR-C", stop_atr_multiple=Decimal("0.75"),
                      target_floor_bps=Decimal("200"), max_holding_bars=8,
                      expected_holding_horizon=4),
        HorizonConfig(label="MR-D", stop_atr_multiple=Decimal("2.0"),
                      target_floor_bps=Decimal("400"), max_holding_bars=32,
                      expected_holding_horizon=16),
    )


def _range_configs() -> tuple[HorizonConfig, ...]:
    """Four range-expansion configurations, likewise horizon-scaled.

    Range expansion is included because it is the natural counterpart hypothesis: if
    friction rather than the idea is the obstacle, a breakout concept whose target is a
    multiple of a *coarser* range should also see its friction share fall. Testing only one
    concept would leave the conclusion attributable to the concept rather than the horizon.
    """
    return (
        HorizonConfig(label="RE-A", stop_atr_multiple=Decimal("1.2"),
                      target_floor_bps=Decimal("300"), max_holding_bars=12,
                      expected_holding_horizon=6, expansion_atr_multiple=Decimal("1.5")),
        HorizonConfig(label="RE-B", stop_atr_multiple=Decimal("1.5"),
                      target_floor_bps=Decimal("400"), max_holding_bars=18,
                      expected_holding_horizon=9, expansion_atr_multiple=Decimal("2.0")),
        HorizonConfig(label="RE-C", stop_atr_multiple=Decimal("1.0"),
                      target_floor_bps=Decimal("250"), max_holding_bars=8,
                      expected_holding_horizon=4, expansion_atr_multiple=Decimal("1.2")),
        HorizonConfig(label="RE-D", stop_atr_multiple=Decimal("2.0"),
                      target_floor_bps=Decimal("500"), max_holding_bars=24,
                      expected_holding_horizon=12, expansion_atr_multiple=Decimal("2.5")),
    )


VOLATILITY_MR_HORIZON_CONFIGS = _mr_configs()
RANGE_EXPANSION_HORIZON_CONFIGS = _range_configs()

CONFIG_SETS: dict[str, tuple[HorizonConfig, ...]] = {
    "volatility_mean_reversion": VOLATILITY_MR_HORIZON_CONFIGS,
    "range_expansion": RANGE_EXPANSION_HORIZON_CONFIGS,
}

PREDECLARED_HORIZONS: tuple[TimeframeSpec, ...] = (FIFTEEN_MINUTE, ONE_HOUR_TIMEFRAME)

# The selection rule, stated as data so it cannot be reworded after seeing results. Risk
# gates come first: a configuration that breaches policy is not selectable regardless of
# how attractive its return, because the alternative is choosing returns with unpriced risk.
SELECTION_RULE: tuple[str, ...] = (
    "risk_gates_satisfied",
    "friction_ratio_ascending",
    "net_pnl_per_capital_hour_descending",
    "net_pnl_descending",
)


def predeclared_configuration_count() -> int:
    """Total configurations the manifest commits to testing, per horizon."""
    return sum(len(configs) for configs in CONFIG_SETS.values())


def predeclared_configuration_budget() -> tuple[tuple[str, int], ...]:
    """Configurations per (concept, horizon). The ceiling in the spec is per strategy and
    horizon, so the key must include both — a per-concept key would permit four
    configurations on 15m *and* four more on 1h, which is eight for one strategy."""
    return tuple((f"{concept}:{timeframe.name}", len(configs))
                 for concept, configs in CONFIG_SETS.items()
                 for timeframe in PREDECLARED_HORIZONS)


@dataclass(frozen=True, slots=True)
class HorizonExperimentManifest:
    """Frozen inputs of the time-horizon experiment.

    Every field here is one that could move the outcome. The profile fingerprints are
    recorded rather than the profile objects, so the manifest cannot drift silently when a
    module changes.
    """

    baseline_commit: str
    product_version: str
    code_commit: str
    created_at: str

    horizons: tuple[tuple[str, int], ...]
    profile_versions: tuple[tuple[str, str], ...]
    profile_fingerprints: tuple[tuple[str, str], ...]
    configuration_budget: tuple[tuple[str, int], ...]
    configuration_parameters: tuple[tuple[str, tuple[tuple[str, str], ...]], ...]

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
    friction_model: str

    dataset_cutoff_ms: int
    development_window: tuple[str, int, int]
    holdout_window: tuple[str, int, int]
    bar_base_seconds: int

    def __post_init__(self) -> None:
        if not self.baseline_commit or not self.code_commit:
            raise HorizonExperimentError("baseline and code commit are required")
        if tuple(item[0] for item in self.horizons) != ("15m", "1h"):
            raise HorizonExperimentError("exactly the predeclared 15m and 1h timeframes")
        for _, count in self.configuration_budget:
            if count > MAX_CONFIGURATIONS_PER_HORIZON:
                raise HorizonExperimentError(
                    f"configuration budget exceeds {MAX_CONFIGURATIONS_PER_HORIZON}")
        if self.certifying_execution_mode != CERTIFYING_EXECUTION_MODE:
            raise HorizonExperimentError(
                "only taker-taker execution may certify a horizon experiment")
        if self.max_drawdown_mxn <= 0 or self.minimum_reward_risk_ratio <= 0:
            raise HorizonExperimentError("risk policy must be positive")
        if self.development_window[2] > self.dataset_cutoff_ms:
            raise HorizonExperimentError("development window may not cross the dataset cutoff")
        if self.holdout_window[2] > self.development_window[1]:
            raise HorizonExperimentError("holdout must precede development")

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public())

    def public(self) -> dict[str, Any]:
        return {"version": HORIZON_EXPERIMENT_VERSION,
                "product_version": self.product_version,
                "baseline_commit": self.baseline_commit, "code_commit": self.code_commit,
                "created_at": self.created_at,
                "horizons": [{"name": name, "seconds": seconds}
                             for name, seconds in self.horizons],
                "profile_versions": [list(item) for item in self.profile_versions],
                "profile_fingerprints": [list(item) for item in self.profile_fingerprints],
                "configuration_budget": dict(self.configuration_budget),
                "configuration_parameters": [
                    [key, [list(item) for item in value]]
                    for key, value in self.configuration_parameters],
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
                "friction_model": self.friction_model,
                "dataset_cutoff_ms": self.dataset_cutoff_ms,
                "development_window": list(self.development_window),
                "holdout_window": list(self.holdout_window),
                "bar_base_seconds": self.bar_base_seconds,
                "selection_rule": list(SELECTION_RULE),
                "parameters_tested_retained": True,
                "holdout_used_for_selection": False}


class HorizonManifestStore:
    """Writes the horizon manifest once and refuses to overwrite it."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.root / HORIZON_MANIFEST_FILE

    def exists(self) -> bool:
        return self.path.exists()

    def freeze(self, manifest: HorizonExperimentManifest) -> dict[str, Any]:
        if self.path.exists():
            raise HorizonExperimentError("HORIZON_MANIFEST_ALREADY_FROZEN")
        payload = manifest.public()
        payload["manifest_fingerprint"] = manifest.fingerprint
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        return payload

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            raise HorizonExperimentError("HORIZON_MANIFEST_MISSING")
        loaded: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        return loaded


def freeze_horizon_manifest(*, root: Path, baseline_commit: str, product_version: str,
                            code_commit: str, dataset_cutoff_ms: int,
                            development: tuple[str, int, int],
                            holdout: tuple[str, int, int], economic_policy: Any,
                            max_drawdown_mxn: Decimal, max_single_trade_risk_mxn: Decimal,
                            minimum_reward_risk_ratio: Decimal, single_order_cap_mxn: Decimal,
                            authorized_capital_mxn: Decimal, execution_model_version: str,
                            maker_fee_rate: Decimal,
                            taker_fee_rate: Decimal) -> dict[str, Any]:
    """Freeze the experiment. Called before any window is evaluated."""
    profiles = horizon_profiles()
    versions = tuple((p.identity.profile_id, p.identity.version) for p in profiles)
    fingerprints = tuple((p.identity.profile_id, p.identity.fingerprint) for p in profiles)
    budget = tuple((f"{concept}:{timeframe.name}", len(configs))
                   for concept, configs in CONFIG_SETS.items()
                   for timeframe in PREDECLARED_HORIZONS)
    declared = tuple(
        (f"{concept}:{timeframe.name}:{config.label}",
         config.parameters(timeframe=timeframe))
        for concept, configs in CONFIG_SETS.items()
        for timeframe in PREDECLARED_HORIZONS for config in configs)
    manifest = HorizonExperimentManifest(
        baseline_commit=baseline_commit, product_version=product_version,
        code_commit=code_commit, created_at=datetime.now(UTC).isoformat(),
        horizons=tuple((tf.name, tf.seconds) for tf in PREDECLARED_HORIZONS),
        profile_versions=versions, profile_fingerprints=fingerprints,
        configuration_budget=budget, configuration_parameters=declared,
        economic_policy_version=str(getattr(economic_policy, "version", "")),
        economic_policy_fingerprint=fingerprint(economic_policy.public()
                                                if hasattr(economic_policy, "public")
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
        certifying_execution_mode=CERTIFYING_EXECUTION_MODE,
        account_maker_fee_rate=maker_fee_rate, account_taker_fee_rate=taker_fee_rate,
        friction_model="round_trip_spread_plus_slippage_plus_fee_at_declared_rates",
        dataset_cutoff_ms=dataset_cutoff_ms, development_window=development,
        holdout_window=holdout, bar_base_seconds=60)
    return HorizonManifestStore(root).freeze(manifest)


__all__ = [
    "CERTIFYING_EXECUTION_MODE",
    "CONFIG_SETS",
    "FORWARD_MICROSTRUCTURE_CAPTURE_READY",
    "HORIZON_BLOCKED",
    "HORIZON_EXPERIMENT_VERSION",
    "HORIZON_MANIFEST_FILE",
    "HORIZON_OUTCOMES",
    "INSUFFICIENT_HORIZON_EVIDENCE",
    "LONGER_HORIZON_PROMISING",
    "MAX_CONFIGURATIONS_PER_HORIZON",
    "MICROSTRUCTURE_BLOCKED",
    "NO_HORIZON_EDGE",
    "PREDECLARED_HORIZONS",
    "RANGE_EXPANSION_HORIZON_CONFIGS",
    "SELECTION_RULE",
    "VOLATILITY_MR_HORIZON_CONFIGS",
    "HorizonConfig",
    "HorizonExperimentError",
    "HorizonExperimentManifest",
    "HorizonManifestStore",
    "freeze_horizon_manifest",
    "predeclared_configuration_budget",
    "predeclared_configuration_count",
]

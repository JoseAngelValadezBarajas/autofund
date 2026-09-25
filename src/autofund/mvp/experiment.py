"""Freeze the trading experiment before evaluating evidence.

The ordering rule in this module is the entire point: the manifest is written *before*
any holdout evidence is fetched or examined. If thresholds, fees, fill assumptions or
strategy parameters could be adjusted after seeing the result, then "certified" would
mean "we searched until something passed", which is not a claim anyone can act on.

Two consequences follow, and both are enforced here rather than by convention:

**Lineage.** Every field that could influence an outcome is hashed into
`TRADING_EXPERIMENT_FINGERPRINT`. Change any of them and the fingerprint changes,
which starts a *new* experiment. Evidence collected under one fingerprint is never
merged into the assessment of another, because that would let a variant borrow
another variant's sample.

**Provenance is not interchangeable.** Five evidence kinds exist and they carry
different weight:

    SYNTHETIC_FIXTURE          proves the implementation runs, nothing about markets
    REAL_HISTORICAL_DEVELOPMENT  used to design/debug; NOT independent confirmation
    REAL_HISTORICAL_HOLDOUT      unseen by the frozen profile; independent
    REAL_CAPTURED_FORWARD        arrives after the freeze; prospective
    REAL_PRODUCTION_FILL         real exchange execution; strongest

Only the last three may contribute to certification. Development evidence is retained
and reported because it is genuinely useful, but it is never presented as if it were
out-of-sample. MVP 0.2 evaluated 30 days of history while building and debugging the
profiles, so that window is development evidence by definition, and this module says so
explicitly instead of letting it masquerade as validation.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.replay.serialization import fingerprint

EXPERIMENT_VERSION = "autofund.trading-experiment.v1"
MANIFEST_FILE = "TRADING_EXPERIMENT_MANIFEST.json"

# ---------------------------------------------------------------------------
# Evidence provenance (spec section 3). Never silently aggregated.
# ---------------------------------------------------------------------------

SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
REAL_HISTORICAL_DEVELOPMENT = "REAL_HISTORICAL_DEVELOPMENT"
REAL_HISTORICAL_HOLDOUT = "REAL_HISTORICAL_HOLDOUT"
REAL_CAPTURED_FORWARD = "REAL_CAPTURED_FORWARD"
REAL_PRODUCTION_FILL = "REAL_PRODUCTION_FILL"

ALL_PROVENANCE = (SYNTHETIC_FIXTURE, REAL_HISTORICAL_DEVELOPMENT,
                  REAL_HISTORICAL_HOLDOUT, REAL_CAPTURED_FORWARD, REAL_PRODUCTION_FILL)

# Provenance that may contribute toward Production certification. Development is
# deliberately absent: it is the data the profile was built on.
CERTIFYING_PROVENANCE = frozenset({REAL_HISTORICAL_HOLDOUT, REAL_CAPTURED_FORWARD,
                                   REAL_PRODUCTION_FILL})

# Provenance that is real market data but not independent confirmation.
DESCRIPTIVE_PROVENANCE = frozenset({REAL_HISTORICAL_DEVELOPMENT})

NON_CERTIFYING_PROVENANCE = frozenset({SYNTHETIC_FIXTURE})

# Evidence-quality ladder for fills (spec section 9).
FULL_ORDER_BOOK = "FULL_ORDER_BOOK"
TOP_OF_BOOK = "TOP_OF_BOOK"
CANDLE_ONLY_ESTIMATE = "CANDLE_ONLY_ESTIMATE"

ALL_EVIDENCE_QUALITY = (FULL_ORDER_BOOK, TOP_OF_BOOK, CANDLE_ONLY_ESTIMATE)


class ExperimentError(ValueError):
    """The experiment contract was violated."""


@dataclass(frozen=True, slots=True)
class HoldoutWindow:
    """A predeclared, fixed evaluation interval.

    Declared before evaluation so the interval cannot be chosen to suit the answer.
    `reason` is required and recorded, because "we picked this range" without a stated
    rationale is indistinguishable from interval shopping.
    """

    name: str
    start_ms: int
    end_ms: int
    reason: str

    def __post_init__(self) -> None:
        if not self.name or not self.reason:
            raise ExperimentError("holdout requires a name and a stated reason")
        if self.end_ms <= self.start_ms:
            raise ExperimentError("holdout end must be after its start")

    @property
    def duration_days(self) -> Decimal:
        return Decimal(self.end_ms - self.start_ms) / Decimal("86400000")

    def overlaps(self, other: "HoldoutWindow") -> bool:
        """Half-open interval overlap. Declared here so callers cannot invent a rule."""
        return self.start_ms < other.end_ms and other.start_ms < self.end_ms

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "start_ms": self.start_ms, "end_ms": self.end_ms,
                "start_at": datetime.fromtimestamp(self.start_ms / 1000, tz=UTC).isoformat(),
                "end_at": datetime.fromtimestamp(self.end_ms / 1000, tz=UTC).isoformat(),
                "duration_days": str(self.duration_days), "reason": self.reason}


def declare_holdout(*, name: str, development_start_ms: int, duration_days: int,
                    reason: str) -> HoldoutWindow:
    """A holdout immediately preceding the development window.

    Adjacent and equal-length by construction, so the two windows differ in time but
    not in regime-catching opportunity. A shorter holdout would weaken the comparison;
    an overlapping one would reuse development data as if it were unseen.
    """
    if duration_days <= 0:
        raise ExperimentError("holdout duration must be positive")
    span = duration_days * 86400000
    return HoldoutWindow(name=name, start_ms=development_start_ms - span,
                         end_ms=development_start_ms, reason=reason)


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    """Everything that could change an outcome, frozen before evaluation.

    Deliberately exhaustive: a field that can move the result but is absent here would
    be a hole through which a later tweak could inherit this experiment's evidence.
    """

    strategy_profile_id: str
    strategy_version: str
    strategy_fingerprint: str
    strategy_parameters: tuple[tuple[str, str], ...]

    economic_policy_version: str
    economic_policy_fingerprint: str
    minimum_net_profit_mxn: Decimal
    minimum_net_edge_bps: Decimal

    fee_model_version: str
    fee_source_semantics: str
    confirmed_taker_fee_rate: Decimal

    slippage_model_version: str
    fill_model_version: str
    market_classification_version: str
    certification_policy_version: str

    risk_policy_fingerprint: str
    capital_policy_fingerprint: str
    single_order_cap_mxn: Decimal
    max_deployment_mxn: Decimal
    authorized_capital_mxn: Decimal

    dataset_cutoff_ms: int
    code_commit: str
    created_at: str

    development_window: HoldoutWindow
    holdout_windows: tuple[HoldoutWindow, ...] = ()

    def __post_init__(self) -> None:
        for name in ("strategy_profile_id", "strategy_version", "code_commit"):
            if not getattr(self, name):
                raise ExperimentError(f"manifest requires {name}")
        if self.single_order_cap_mxn > Decimal("11"):
            raise ExperimentError("single-order cap must remain <= 11 MXN")
        if self.max_deployment_mxn > Decimal("25"):
            raise ExperimentError("deployment cap must remain <= 25 MXN")
        if self.authorized_capital_mxn > Decimal("50"):
            raise ExperimentError("authorized capital must remain <= 50 MXN")

    @property
    def fingerprint_value(self) -> str:
        """TRADING_EXPERIMENT_FINGERPRINT: identity of the frozen experiment."""
        return fingerprint({
            "schema": EXPERIMENT_VERSION,
            "strategy_profile_id": self.strategy_profile_id,
            "strategy_version": self.strategy_version,
            "strategy_fingerprint": self.strategy_fingerprint,
            "strategy_parameters": dict(self.strategy_parameters),
            "economic_policy_version": self.economic_policy_version,
            "economic_policy_fingerprint": self.economic_policy_fingerprint,
            "minimum_net_profit_mxn": str(self.minimum_net_profit_mxn),
            "minimum_net_edge_bps": str(self.minimum_net_edge_bps),
            "fee_model_version": self.fee_model_version,
            "fee_source_semantics": self.fee_source_semantics,
            "confirmed_taker_fee_rate": str(self.confirmed_taker_fee_rate),
            "slippage_model_version": self.slippage_model_version,
            "fill_model_version": self.fill_model_version,
            "market_classification_version": self.market_classification_version,
            "certification_policy_version": self.certification_policy_version,
            "risk_policy_fingerprint": self.risk_policy_fingerprint,
            "capital_policy_fingerprint": self.capital_policy_fingerprint,
            "single_order_cap_mxn": str(self.single_order_cap_mxn),
            "max_deployment_mxn": str(self.max_deployment_mxn),
            "authorized_capital_mxn": str(self.authorized_capital_mxn),
            "dataset_cutoff_ms": self.dataset_cutoff_ms,
            "development_window": self.development_window.public(),
            "holdout_windows": [w.public() for w in self.holdout_windows],
        })

    def public(self) -> dict[str, Any]:
        return {
            "version": EXPERIMENT_VERSION,
            "experiment_fingerprint": self.fingerprint_value,
            "created_at": self.created_at, "code_commit": self.code_commit,
            "strategy": {"profile_id": self.strategy_profile_id,
                         "version": self.strategy_version,
                         "fingerprint": self.strategy_fingerprint,
                         "parameters": dict(self.strategy_parameters)},
            "economics": {"policy_version": self.economic_policy_version,
                          "policy_fingerprint": self.economic_policy_fingerprint,
                          "minimum_net_profit_mxn": str(self.minimum_net_profit_mxn),
                          "minimum_net_edge_bps": str(self.minimum_net_edge_bps)},
            "execution": {"fee_model_version": self.fee_model_version,
                          "fee_source_semantics": self.fee_source_semantics,
                          "confirmed_taker_fee_rate": str(self.confirmed_taker_fee_rate),
                          "slippage_model_version": self.slippage_model_version,
                          "fill_model_version": self.fill_model_version},
            "policy": {"market_classification_version": self.market_classification_version,
                       "certification_policy_version": self.certification_policy_version,
                       "risk_policy_fingerprint": self.risk_policy_fingerprint,
                       "capital_policy_fingerprint": self.capital_policy_fingerprint,
                       "single_order_cap_mxn": str(self.single_order_cap_mxn),
                       "max_deployment_mxn": str(self.max_deployment_mxn),
                       "authorized_capital_mxn": str(self.authorized_capital_mxn)},
            "dataset_cutoff_ms": self.dataset_cutoff_ms,
            "development_window": self.development_window.public(),
            "holdout_windows": [w.public() for w in self.holdout_windows],
            "single_order_cap_unchanged": self.single_order_cap_mxn == Decimal("11"),
        }


def development_window(*, hours: int, now: datetime | None = None,
                       time_bucket: int = 60, name: str = "DEVELOPMENT") -> HoldoutWindow:
    """The window the profiles were built and debugged on. Never certifying."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    bucket_ms = time_bucket * 1000
    end_ms = (int(moment.timestamp() * 1000) // bucket_ms) * bucket_ms
    return HoldoutWindow(name=name, start_ms=end_ms - hours * 3600000, end_ms=end_ms,
                         reason="window used while building and debugging the frozen profiles")


@dataclass
class ExperimentManifestStore:
    """Writes the manifest once, then refuses to overwrite it.

    Refusing is the feature. A manifest that could be rewritten would let a later
    evaluation quietly inherit an earlier freeze, which is the failure this whole
    mechanism exists to prevent.
    """

    root: Path

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.root / MANIFEST_FILE

    def exists(self) -> bool:
        return self.path.exists()

    def freeze(self, manifest: ExperimentManifest) -> dict[str, Any]:
        """Persist the manifest. Raises if a manifest already exists."""
        if self.exists():
            raise ExperimentError("EXPERIMENT_ALREADY_FROZEN")
        payload = manifest.public()
        self.path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n",
                            encoding="utf-8")
        return payload

    def load(self) -> dict[str, Any]:
        if not self.exists():
            raise ExperimentError("NO_FROZEN_EXPERIMENT_MANIFEST")
        loaded: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        return loaded

    def freeze_holdouts(self, windows: tuple[HoldoutWindow, ...]) -> dict[str, Any]:
        """Append predeclared holdout windows. Records them before evaluation."""
        payload = self.load()
        existing = {item["name"] for item in payload.get("holdout_windows", [])}
        for window in windows:
            if window.name in existing:
                raise ExperimentError(f"HOLDOUT_ALREADY_DECLARED:{window.name}")
        payload["holdout_windows"] = [*payload.get("holdout_windows", []),
                                      *(w.public() for w in windows)]
        self.path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n",
                            encoding="utf-8")
        return payload


def provenance_for_window(*, window_name: str) -> str:
    """Map a window name onto its evidence provenance.

    Kept as one function so a window cannot acquire certifying status by being labelled
    carelessly at a call site.
    """
    if window_name.startswith("HOLDOUT"):
        return REAL_HISTORICAL_HOLDOUT
    if window_name == "FORWARD":
        return REAL_CAPTURED_FORWARD
    if window_name == "PRODUCTION":
        return REAL_PRODUCTION_FILL
    if window_name == "FIXTURE":
        return SYNTHETIC_FIXTURE
    return REAL_HISTORICAL_DEVELOPMENT


def may_certify(provenance_kind: str) -> bool:
    """Whether a provenance class may contribute toward Production certification.

    Fixture evidence is excluded outright: it demonstrates that the code runs and says
    nothing about whether a market offers an edge.
    """
    return provenance_kind in CERTIFYING_PROVENANCE


def is_independent(provenance_kind: str) -> bool:
    """Whether the evidence was unseen when the profile was frozen.

    Stated separately from `may_certify` even though the two currently agree, because
    they answer different questions ("was this person looking?" versus "may this count?")
    and Development evidence is the case where conflating them would be dangerous. It is
    real market data, and it is still not independent.
    """
    return (provenance_kind in CERTIFYING_PROVENANCE
            and provenance_kind not in DESCRIPTIVE_PROVENANCE)


__all__ = [
    "ALL_EVIDENCE_QUALITY",
    "ALL_PROVENANCE",
    "CANDLE_ONLY_ESTIMATE",
    "CERTIFYING_PROVENANCE",
    "DESCRIPTIVE_PROVENANCE",
    "EXPERIMENT_VERSION",
    "FULL_ORDER_BOOK",
    "MANIFEST_FILE",
    "NON_CERTIFYING_PROVENANCE",
    "REAL_CAPTURED_FORWARD",
    "REAL_HISTORICAL_DEVELOPMENT",
    "REAL_HISTORICAL_HOLDOUT",
    "REAL_PRODUCTION_FILL",
    "SYNTHETIC_FIXTURE",
    "TOP_OF_BOOK",
    "ExperimentError",
    "ExperimentManifest",
    "ExperimentManifestStore",
    "HoldoutWindow",
    "declare_holdout",
    "development_window",
    "is_independent",
    "may_certify",
    "provenance_for_window",
]

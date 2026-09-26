"""Durable research read-model: alpha sources, strategies, experiments, evidence, campaigns.

MVP 0.3.0 turns eleven milestones of research into a product. The infrastructure already exists —
market observation, Production execution, reconciliation, wallet separation, the economic guard,
the risk engine, replay, shadow trading, certification, holdout validation, microstructure and
cross-venue capture — and what is missing is a coherent way to *see* it. This module is the
read-model that makes that possible.

**Three truths, kept apart.** The single most important property of this model is that it never
lets three different questions collapse into one status:

    ENGINEERING      is the machinery working?          (adapters, ledger, collectors)
    EVIDENCE         what did the research conclude?    (predictive? strong? sufficient?)
    AUTHORIZATION    may money move?                    (Production, certification, session)

A strategy can legitimately be engineering-PASS, evidence-STRONG, economics-FAIL and
Production-DISABLED all at once, and that is not a contradiction to be smoothed over — it is the
most common real state in this project. Any model that produced a single green/red per row would
destroy exactly the information an operator needs. So the fields are separate, and a test asserts
they cannot be derived from one another.

**Read model, not financial truth.** The Production ledger and journal stay authoritative for
fills, positions, cash and realised P&L. Nothing here writes to them. The registry consumes
financial state to *describe* it and has no capability to change it.

**Unknown is a value, not a gap to fill.** Older milestones lack fields that newer ones record.
The model carries `NOT_RECORDED`, `NOT_APPLICABLE` and `UNKNOWN` explicitly rather than inventing
a plausible default, because a fabricated fingerprint or a guessed MAE is worse than a blank: it
looks like evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

# The schema version of the read model itself, separate from the product version and from any
# experiment's own fingerprint. Future formats evolve; readers migrate rather than rewrite history.
RESEARCH_REGISTRY_SCHEMA_VERSION = "autofund.research-registry.v1"

# Values that mean "we do not know" and are deliberately distinguishable from each other.
#
# UNKNOWN          the question applies but no answer was recorded
# NOT_RECORDED     the artifact predates the field
# NOT_APPLICABLE   the question does not apply to this thing
#
# Collapsing these into one sentinel would lose the difference between "we never measured it",
# "this milestone did not have the field" and "this concept has no meaning here", and an operator
# debugging a gap needs to know which one they are looking at.
UNKNOWN = "UNKNOWN"
NOT_RECORDED = "NOT_RECORDED"
NOT_APPLICABLE = "NOT_APPLICABLE"


class ResearchError(ValueError):
    """A read-model record is malformed or internally inconsistent."""


# ---------------------------------------------------------------------------------------------
# Alpha sources: information, not strategies
# ---------------------------------------------------------------------------------------------

class AlphaClassification(StrEnum):
    """What is known about an information source.

    The vocabulary separates *predictive* from *economic* throughout, because those are different
    findings and the project's own history is the proof: its one validated signal is predictive on
    unseen data and roughly seventy times too small to trade.
    """

    HYPOTHESIS = "HYPOTHESIS"
    DISCOVERY = "DISCOVERY"
    PREDICTIVE = "PREDICTIVE"
    VALIDATED_INFORMATION = "VALIDATED_INFORMATION"
    PREDICTIVE_NOT_ECONOMIC = "PREDICTIVE_NOT_ECONOMIC"
    VALIDATED_ALPHA_SOURCE = "VALIDATED_ALPHA_SOURCE"
    REJECTED = "REJECTED"
    FROZEN = "FROZEN"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    ACCUMULATING = "ACCUMULATING"


class PredictionStatus(StrEnum):
    """Whether the source predicts anything, independent of whether that could pay."""

    PREDICTIVE = "PREDICTIVE"
    NOT_PREDICTIVE = "NOT_PREDICTIVE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NOT_TESTED = "NOT_TESTED"


class EconomicStatus(StrEnum):
    """Whether the predictive content could cover its costs. A separate question, always."""

    ECONOMIC = "ECONOMIC"
    NOT_ECONOMIC = "NOT_ECONOMIC"
    UNKNOWN = "UNKNOWN"
    NOT_EVALUATED = "NOT_EVALUATED"


class AlphaFamily(StrEnum):
    PRICE_ONLY = "PRICE_ONLY"
    CROSS_MARKET = "CROSS_MARKET"
    MICROSTRUCTURE = "MICROSTRUCTURE"
    CROSS_VENUE = "CROSS_VENUE"
    EXECUTION = "EXECUTION"
    OTHER = "OTHER"


@dataclass(frozen=True, slots=True)
class AlphaSourceRecord:
    """One information source, with its predictive and economic standing held apart.

    `movement_bps` and `required_friction_bps` are both optional on purpose. Where they exist the
    gap between them *is* the milestone's central finding, and reporting one without the other
    would let a real signal look tradeable or a tradeable-looking gap look real.
    """

    alpha_id: str
    name: str
    family: AlphaFamily
    version: str = NOT_RECORDED
    fingerprint: str | None = None
    classification: AlphaClassification = AlphaClassification.HYPOTHESIS
    prediction_status: PredictionStatus = PredictionStatus.NOT_TESTED
    economic_status: EconomicStatus = EconomicStatus.NOT_EVALUATED
    markets: tuple[str, ...] = ()
    prediction_horizon: str = NOT_RECORDED
    development_status: str = NOT_RECORDED
    validation_status: str = NOT_RECORDED
    movement_bps: Decimal | None = None
    required_friction_bps: Decimal | None = None
    evidence_provenance: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    source_experiment: str = NOT_RECORDED
    artifact_refs: tuple[str, ...] = ()
    created_at: str | None = None
    frozen_at: str | None = None
    is_a_strategy: bool = False
    hypothesis: str = NOT_RECORDED
    definition: str = NOT_RECORDED

    def __post_init__(self) -> None:
        if not self.alpha_id or not self.name:
            raise ResearchError("an alpha source needs an id and a name")
        if self.is_a_strategy:
            # The registry exists partly to enforce this: information is not a trade.
            raise ResearchError(
                f"{self.alpha_id}: an alpha source is information and cannot be a strategy")

    @property
    def economic_headroom_bps(self) -> Decimal | None:
        """How much room the signal has after its costs. Negative means it cannot pay."""
        if self.movement_bps is None or self.required_friction_bps is None:
            return None
        return self.movement_bps - self.required_friction_bps

    @property
    def tradeable(self) -> bool | None:
        """Whether this could be traded at the recorded costs, or None when that is undecided.

        Deliberately three-valued. `False` covers a measured shortfall and `None` covers an
        unmeasured one, and reporting the second as the first would claim a conclusion the
        evidence does not support.
        """
        if self.economic_status == EconomicStatus.NOT_EVALUATED:
            return None
        if self.economic_status == EconomicStatus.UNKNOWN:
            return None
        return self.economic_status == EconomicStatus.ECONOMIC

    def public(self) -> dict[str, Any]:
        return {
            "alpha_id": self.alpha_id, "name": self.name, "family": str(self.family),
            "version": self.version, "fingerprint": self.fingerprint,
            "classification": str(self.classification),
            "prediction_status": str(self.prediction_status),
            "economic_status": str(self.economic_status),
            "markets": list(self.markets),
            "prediction_horizon": self.prediction_horizon,
            "development_status": self.development_status,
            "validation_status": self.validation_status,
            "movement_bps": _s(self.movement_bps),
            "required_friction_bps": _s(self.required_friction_bps),
            "economic_headroom_bps": _s(self.economic_headroom_bps),
            "tradeable": self.tradeable,
            "evidence_provenance": list(self.evidence_provenance),
            "reason_codes": list(self.reason_codes),
            "source_experiment": self.source_experiment,
            "artifact_refs": list(self.artifact_refs),
            "created_at": self.created_at, "frozen_at": self.frozen_at,
            "is_a_strategy": False, "hypothesis": self.hypothesis,
            "definition": self.definition,
        }


# ---------------------------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------------------------

class StrategyStatus(StrEnum):
    """The lifecycle a profile can be in.

    These reuse the project's existing canonical vocabulary rather than introducing a parallel set,
    so a status shown here means the same thing it means in the strategy library.
    """

    RESEARCH = "RESEARCH"
    DEVELOPMENT = "DEVELOPMENT"
    HOLDOUT = "HOLDOUT"
    FORWARD_SHADOW = "FORWARD_SHADOW"
    PRODUCTION_CERTIFIABLE = "PRODUCTION_CERTIFIABLE"
    CERTIFIED = "CERTIFIED"
    ACTIVE_PRODUCTION = "ACTIVE_PRODUCTION"
    FROZEN = "FROZEN"
    NOT_VIABLE = "NOT_VIABLE"
    REJECTED = "REJECTED"
    SUSPENDED = "SUSPENDED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True, slots=True)
class StrategyProfileRecord:
    """One strategy profile and everything known about its evidence and its authorization.

    The four verdict fields are independent. `engineering_status` describes whether the code runs;
    `economic_status` and `risk_status` describe what the research found; `production_eligible`
    describes whether money may move. They are never derived from one another.
    """

    profile_id: str
    strategy_id: str
    version: str = NOT_RECORDED
    fingerprint: str | None = None
    status: StrategyStatus = StrategyStatus.RESEARCH
    markets_evaluated: tuple[str, ...] = ()
    timeframes: tuple[str, ...] = ()
    alpha_dependency: str | None = None
    entry_model: str = NOT_RECORDED
    exit_model: str = NOT_RECORDED
    risk_model: str = NOT_RECORDED
    economic_policy_fingerprint: str | None = None
    development_evidence: dict[str, Any] = field(default_factory=dict)
    holdout_evidence: dict[str, Any] = field(default_factory=dict)
    forward_shadow_evidence: dict[str, Any] = field(default_factory=dict)
    production_certification: str = NOT_RECORDED
    reason_codes: tuple[str, ...] = ()
    predecessor_profile: str | None = None
    successor_profile: str | None = None
    frozen: bool = False
    # The four separated verdicts.
    engineering_status: str = UNKNOWN
    economic_status: str = UNKNOWN
    risk_status: str = UNKNOWN
    production_eligible: bool = False
    # Metrics that older artifacts may not carry. None means not recorded, never zero.
    round_trips: int | None = None
    net_pnl_mxn: Decimal | None = None
    max_drawdown_mxn: Decimal | None = None
    median_mae_mxn: Decimal | None = None
    median_mfe_mxn: Decimal | None = None
    economic_rejection_rate: Decimal | None = None
    risk_rejection_rate: Decimal | None = None
    mae_mfe_available: bool = False
    artifact_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.profile_id or not self.strategy_id:
            raise ResearchError("a strategy profile needs a profile id and a strategy id")

    @property
    def is_frozen_terminal(self) -> bool:
        """Whether this profile is closed to further work.

        Drives a UI property that matters: a frozen profile must look finished rather than pending,
        so an operator does not wait for a result that is never coming. A future attempt is a new
        profile, not an edit to this one.
        """
        return self.frozen or self.status in (
            StrategyStatus.FROZEN, StrategyStatus.NOT_VIABLE, StrategyStatus.REJECTED)

    def public(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id, "strategy_id": self.strategy_id,
            "version": self.version, "fingerprint": self.fingerprint, "status": str(self.status),
            "markets_evaluated": list(self.markets_evaluated),
            "timeframes": list(self.timeframes), "alpha_dependency": self.alpha_dependency,
            "entry_model": self.entry_model, "exit_model": self.exit_model,
            "risk_model": self.risk_model,
            "economic_policy_fingerprint": self.economic_policy_fingerprint,
            "development_evidence": dict(self.development_evidence),
            "holdout_evidence": dict(self.holdout_evidence),
            "forward_shadow_evidence": dict(self.forward_shadow_evidence),
            "production_certification": self.production_certification,
            "reason_codes": list(self.reason_codes),
            "predecessor_profile": self.predecessor_profile,
            "successor_profile": self.successor_profile,
            "frozen": self.frozen, "is_frozen_terminal": self.is_frozen_terminal,
            "engineering_status": self.engineering_status,
            "economic_status": self.economic_status,
            "risk_status": self.risk_status,
            "production_eligible": self.production_eligible,
            "round_trips": self.round_trips, "net_pnl_mxn": _s(self.net_pnl_mxn),
            "max_drawdown_mxn": _s(self.max_drawdown_mxn),
            "median_mae_mxn": _s(self.median_mae_mxn),
            "median_mfe_mxn": _s(self.median_mfe_mxn),
            "economic_rejection_rate": _s(self.economic_rejection_rate),
            "risk_rejection_rate": _s(self.risk_rejection_rate),
            "mae_mfe_available": self.mae_mfe_available,
            "artifact_refs": list(self.artifact_refs),
        }


# ---------------------------------------------------------------------------------------------
# Experiments
# ---------------------------------------------------------------------------------------------

class ExperimentStatus(StrEnum):
    DRAFT = "DRAFT"
    FROZEN = "FROZEN"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    SUPERSEDED = "SUPERSEDED"
    ARCHIVED = "ARCHIVED"


@dataclass(frozen=True, slots=True)
class ExperimentRecord:
    """What was actually run, described rather than reconstructed.

    Every optional field means "not recorded". The spec is explicit that fictional experiment
    metadata must not be invented, so a milestone that did not record a holdout window gets
    `NOT_RECORDED` and the UI shows that rather than a plausible-looking date range.
    """

    experiment_id: str
    milestone: str
    title: str
    hypothesis: str = NOT_RECORDED
    baseline_commit: str = NOT_RECORDED
    fingerprint: str | None = None
    status: ExperimentStatus = ExperimentStatus.COMPLETED
    started_at: str | None = None
    finished_at: str | None = None
    development_interval: dict[str, Any] | None = None
    holdout_interval: dict[str, Any] | None = None
    forward_interval: dict[str, Any] | None = None
    markets: tuple[str, ...] = ()
    strategies: tuple[str, ...] = ()
    alpha_sources: tuple[str, ...] = ()
    economic_policy_fingerprint: str | None = None
    risk_policy_fingerprint: str | None = None
    dataset_fingerprints: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    result_classification: str = NOT_RECORDED
    reason_codes: tuple[str, ...] = ()
    test_summary: dict[str, Any] = field(default_factory=dict)
    superseded_by: str | None = None
    supersedes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.milestone:
            raise ResearchError("an experiment needs an id and a milestone")

    def public(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id, "milestone": self.milestone,
            "title": self.title, "hypothesis": self.hypothesis,
            "baseline_commit": self.baseline_commit, "fingerprint": self.fingerprint,
            "status": str(self.status), "started_at": self.started_at,
            "finished_at": self.finished_at,
            "development_interval": self.development_interval,
            "holdout_interval": self.holdout_interval,
            "forward_interval": self.forward_interval,
            "markets": list(self.markets), "strategies": list(self.strategies),
            "alpha_sources": list(self.alpha_sources),
            "economic_policy_fingerprint": self.economic_policy_fingerprint,
            "risk_policy_fingerprint": self.risk_policy_fingerprint,
            "dataset_fingerprints": list(self.dataset_fingerprints),
            "artifact_refs": list(self.artifact_refs),
            "result_classification": self.result_classification,
            "reason_codes": list(self.reason_codes),
            "test_summary": dict(self.test_summary),
            "superseded_by": self.superseded_by, "supersedes": list(self.supersedes),
        }


# ---------------------------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------------------------

class EvidenceProvenance(StrEnum):
    """Where an observation came from. Never merged silently.

    The distinction between a synthetic fixture, historical development data, a holdout and real
    forward capture is the difference between an experiment that has been tested and one that has
    only been described. Merging them would make the strongest evidence in the project
    indistinguishable from its weakest.
    """

    SYNTHETIC_FIXTURE = "SYNTHETIC_FIXTURE"
    REAL_HISTORICAL_DEVELOPMENT = "REAL_HISTORICAL_DEVELOPMENT"
    REAL_HISTORICAL_HOLDOUT = "REAL_HISTORICAL_HOLDOUT"
    REAL_CAPTURED_FORWARD = "REAL_CAPTURED_FORWARD"
    REAL_CAPTURED_MICROSTRUCTURE = "REAL_CAPTURED_MICROSTRUCTURE"
    REAL_CAPTURED_CROSS_VENUE = "REAL_CAPTURED_CROSS_VENUE"
    REAL_PRODUCTION_FILL = "REAL_PRODUCTION_FILL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One evidence source and the data-quality facts needed to weigh it."""

    evidence_id: str
    provenance: EvidenceProvenance
    experiment_id: str = NOT_RECORDED
    dataset_fingerprint: str | None = None
    start: str | None = None
    end: str | None = None
    markets: tuple[str, ...] = ()
    observation_count: int | None = None
    quality: str = UNKNOWN
    gaps: int | None = None
    artifact_path: str | None = None
    artifact_bytes: int | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    alpha_sources: tuple[str, ...] = ()
    strategies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.evidence_id:
            raise ResearchError("evidence needs an id")

    @property
    def is_real_observation(self) -> bool:
        """Whether this is observation rather than construction.

        A synthetic fixture proves the code runs; it says nothing about a market. Recording the
        difference on the record means a UI cannot accidentally present fixture-derived numbers
        with the same weight as captured ones.
        """
        return self.provenance != EvidenceProvenance.SYNTHETIC_FIXTURE

    def public(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id, "provenance": str(self.provenance),
            "experiment_id": self.experiment_id,
            "dataset_fingerprint": self.dataset_fingerprint,
            "start": self.start, "end": self.end, "markets": list(self.markets),
            "observation_count": self.observation_count, "quality": self.quality,
            "gaps": self.gaps, "artifact_path": self.artifact_path,
            "artifact_bytes": self.artifact_bytes, "summary": dict(self.summary),
            "alpha_sources": list(self.alpha_sources),
            "strategies": list(self.strategies),
            "is_real_observation": self.is_real_observation,
        }


# ---------------------------------------------------------------------------------------------
# Datasets, certifications, campaigns
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DatasetRecord:
    dataset_id: str
    market: str = NOT_RECORDED
    interval_seconds: int | None = None
    start: str | None = None
    end: str | None = None
    candle_count: int | None = None
    fingerprint: str | None = None
    provenance: EvidenceProvenance = EvidenceProvenance.UNKNOWN
    gaps: int | None = None
    artifact_path: str | None = None

    def public(self) -> dict[str, Any]:
        return {"dataset_id": self.dataset_id, "market": self.market,
                "interval_seconds": self.interval_seconds, "start": self.start,
                "end": self.end, "candle_count": self.candle_count,
                "fingerprint": self.fingerprint, "provenance": str(self.provenance),
                "gaps": self.gaps, "artifact_path": self.artifact_path}


@dataclass(frozen=True, slots=True)
class CertificationRecord:
    """One certification outcome, with the four truths it must not conflate."""

    certification_id: str
    experiment_id: str = NOT_RECORDED
    market: str = NOT_RECORDED
    profile_id: str = NOT_RECORDED
    certified: bool = False
    status: str = NOT_RECORDED
    holdout_inspected: bool | None = None
    engineering_status: str = UNKNOWN
    evidence_status: str = UNKNOWN
    economic_status: str = UNKNOWN
    authorization_status: str = UNKNOWN
    reason_codes: tuple[str, ...] = ()
    artifact_path: str | None = None
    certified_at: str | None = None

    def public(self) -> dict[str, Any]:
        return {"certification_id": self.certification_id,
                "experiment_id": self.experiment_id, "market": self.market,
                "profile_id": self.profile_id, "certified": self.certified,
                "status": self.status, "holdout_inspected": self.holdout_inspected,
                "engineering_status": self.engineering_status,
                "evidence_status": self.evidence_status,
                "economic_status": self.economic_status,
                "authorization_status": self.authorization_status,
                "reason_codes": list(self.reason_codes),
                "artifact_path": self.artifact_path, "certified_at": self.certified_at}


class CampaignStatus(StrEnum):
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    DEGRADED = "DEGRADED"
    COMPLETE = "COMPLETE"
    NOT_STARTED = "NOT_STARTED"


@dataclass(frozen=True, slots=True)
class ResearchCampaignRecord:
    """A long-running collection campaign, with process health kept apart from its conclusion.

    The two are separate fields on purpose. A collector can be perfectly healthy while the evidence
    it has gathered is still insufficient, and rendering that as one ambiguous amber state would
    hide the only fact that matters: the process is fine and the *evidence* is not there yet.
    """

    campaign_id: str
    title: str
    experiment_id: str = NOT_RECORDED
    experiment_fingerprint: str | None = None
    status: CampaignStatus = CampaignStatus.NOT_STARTED
    process_health: str = UNKNOWN
    evidence_conclusion: str = UNKNOWN
    planned_duration_hours: Decimal | None = None
    actual_coverage_hours: Decimal | None = None
    markets: tuple[str, ...] = ()
    observations: int | None = None
    gaps: int | None = None
    latest_sample_at: str | None = None
    storage_bytes: int | None = None
    evidence_provenance: EvidenceProvenance = EvidenceProvenance.UNKNOWN
    coverage_sufficient: bool | None = None
    artifact_paths: tuple[str, ...] = ()

    def public(self) -> dict[str, Any]:
        return {
            "campaign_id": self.campaign_id, "title": self.title,
            "experiment_id": self.experiment_id,
            "experiment_fingerprint": self.experiment_fingerprint,
            "status": str(self.status), "process_health": self.process_health,
            "evidence_conclusion": self.evidence_conclusion,
            "planned_duration_hours": _s(self.planned_duration_hours),
            "actual_coverage_hours": _s(self.actual_coverage_hours),
            "coverage_percent": _s(_coverage_percent(self.actual_coverage_hours,
                                                     self.planned_duration_hours)),
            "markets": list(self.markets), "observations": self.observations,
            "gaps": self.gaps, "latest_sample_at": self.latest_sample_at,
            "storage_bytes": self.storage_bytes,
            "evidence_provenance": str(self.evidence_provenance),
            "coverage_sufficient": self.coverage_sufficient,
            "artifact_paths": list(self.artifact_paths),
            "health_is_separate_from_conclusion": True,
        }


# ---------------------------------------------------------------------------------------------
# Production eligibility
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ProductionEligibilityRecord:
    """Why a market/profile pair is or is not eligible, with the reason always concrete.

    The spec is emphatic that a generic "unavailable" is unacceptable when a specific reason
    exists. So `blocking_reason` is a required field: a pair is either eligible, or it has a named
    reason it is not. `NO_SIGNAL` and `NOT_CERTIFIED` are very different problems and an operator
    needs to know which one they have.
    """

    market: str
    profile_id: str = NOT_RECORDED
    alpha_source: str | None = None
    research_status: str = UNKNOWN
    data_quality: str = UNKNOWN
    strategy_compatibility: str = UNKNOWN
    economic_status: str = UNKNOWN
    risk_status: str = UNKNOWN
    certification_status: str = NOT_RECORDED
    authorization_status: str = UNKNOWN
    position_status: str = UNKNOWN
    blocking_reason: str = UNKNOWN
    eligible: bool = False

    def __post_init__(self) -> None:
        if not self.market:
            raise ResearchError("eligibility needs a market")
        if not self.eligible and self.blocking_reason in (UNKNOWN, "", None):
            # A pair that cannot be traded and cannot say why is exactly the state this record
            # exists to prevent.
            raise ResearchError(
                f"{self.market}: an ineligible pair must carry a concrete blocking reason")

    def public(self) -> dict[str, Any]:
        return {
            "market": self.market, "profile_id": self.profile_id,
            "alpha_source": self.alpha_source, "research_status": self.research_status,
            "data_quality": self.data_quality,
            "strategy_compatibility": self.strategy_compatibility,
            "economic_status": self.economic_status, "risk_status": self.risk_status,
            "certification_status": self.certification_status,
            "authorization_status": self.authorization_status,
            "position_status": self.position_status,
            "blocking_reason": self.blocking_reason, "eligible": self.eligible,
        }


# ---------------------------------------------------------------------------------------------
# The read model as a whole
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ResearchRegistry:
    """Everything the Control Center reads, assembled once and served from memory.

    Held as tuples and dictionaries rather than loaded per request, because the underlying
    artifacts reach 5.5 MB and re-parsing them on every dashboard view is the specific performance
    failure the spec warns about.
    """

    alpha_sources: tuple[AlphaSourceRecord, ...] = ()
    strategies: tuple[StrategyProfileRecord, ...] = ()
    experiments: tuple[ExperimentRecord, ...] = ()
    evidence: tuple[EvidenceRecord, ...] = ()
    datasets: tuple[DatasetRecord, ...] = ()
    certifications: tuple[CertificationRecord, ...] = ()
    campaigns: tuple[ResearchCampaignRecord, ...] = ()
    eligibility: tuple[ProductionEligibilityRecord, ...] = ()
    artifacts: tuple[dict[str, Any], ...] = ()
    built_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    schema_version: str = RESEARCH_REGISTRY_SCHEMA_VERSION
    source_roots: tuple[str, ...] = ()
    invalid_artifacts: tuple[str, ...] = ()

    @property
    def counts(self) -> dict[str, int]:
        return {"alpha_records": len(self.alpha_sources),
                "strategy_records": len(self.strategies),
                "experiment_records": len(self.experiments),
                "evidence_records": len(self.evidence),
                "dataset_records": len(self.datasets),
                "certification_records": len(self.certifications),
                "campaign_records": len(self.campaigns),
                "eligibility_records": len(self.eligibility),
                "artifact_records": len(self.artifacts),
                "invalid_artifacts": len(self.invalid_artifacts)}

    def alpha(self, alpha_id: str) -> AlphaSourceRecord | None:
        return next((item for item in self.alpha_sources if item.alpha_id == alpha_id), None)

    def strategy(self, profile_id: str) -> StrategyProfileRecord | None:
        return next((item for item in self.strategies
                     if item.profile_id == profile_id), None)

    def experiment(self, experiment_id: str) -> ExperimentRecord | None:
        return next((item for item in self.experiments
                     if item.experiment_id == experiment_id), None)

    def evidence_for(self, *, experiment_id: str | None = None,
                     alpha_id: str | None = None,
                     strategy: str | None = None,
                     market: str | None = None,
                     provenance: str | None = None) -> tuple[EvidenceRecord, ...]:
        """Filter evidence along any of the axes the explorer exposes."""
        out = []
        for item in self.evidence:
            if experiment_id and item.experiment_id != experiment_id:
                continue
            if alpha_id and alpha_id not in item.alpha_sources:
                continue
            if strategy and strategy not in item.strategies:
                continue
            if market and market not in item.markets:
                continue
            if provenance and str(item.provenance) != provenance:
                continue
            out.append(item)
        return tuple(out)

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version, "built_at": self.built_at,
            "counts": self.counts, "source_roots": list(self.source_roots),
            "invalid_artifacts": list(self.invalid_artifacts),
        }


def _s(value: Decimal | None) -> str | None:
    """Decimal to string, preserving None so a missing metric stays missing."""
    return None if value is None else str(value)


def _coverage_percent(actual: Decimal | None, planned: Decimal | None) -> Decimal | None:
    if actual is None or planned is None or planned <= 0:
        return None
    return actual / planned * Decimal("100")

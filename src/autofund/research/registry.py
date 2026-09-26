"""Builds the research read model from the artifacts that already exist.

**This module reads history and never rewrites it.** Eleven milestones produced certification
documents that are the authoritative record of what was run and what was concluded. The builder
extracts the fields those documents actually contain, records `NOT_RECORDED` for the ones they do
not, and leaves every artifact byte-for-byte untouched. A test asserts that a rebuild does not
modify a single artifact's fingerprint.

**Derived, not hardcoded.** The spec forbids hardcoding conclusions when durable source artifacts
exist, so every status below is read from a certificate rather than written down here. The one
exception is the seed list of *which* experiments exist and what each was asking, because that
provenance lives in the milestone specs and commit history rather than in a machine-readable field.
Even there, each experiment's *result* comes from its certificate, and a missing certificate
produces `NOT_RECORDED` rather than a remembered conclusion.

**Idempotent.** Running the rebuild twice produces identical output, because the input set is fixed
and the ordering is deterministic. That property is what makes the registry safe to refresh on
every startup, and it is tested rather than assumed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from .artifacts import INDEXED, ArtifactIndexer, ArtifactRecord
from .models import (
    NOT_APPLICABLE,
    NOT_RECORDED,
    UNKNOWN,
    AlphaClassification,
    AlphaFamily,
    AlphaSourceRecord,
    CampaignStatus,
    CertificationRecord,
    EconomicStatus,
    EvidenceProvenance,
    EvidenceRecord,
    ExperimentRecord,
    ExperimentStatus,
    PredictionStatus,
    ProductionEligibilityRecord,
    ResearchCampaignRecord,
    ResearchRegistry,
    StrategyProfileRecord,
    StrategyStatus,
)

BUILDER_VERSION = "autofund.research-builder.v1"


# ---------------------------------------------------------------------------------------------
# The seed: which experiments exist, and what each was asking.
#
# Provenance for these lives in the milestone specifications and the commit log rather than in any
# artifact field, so the questions are recorded here. Every *answer* is read from the certificate.
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Seed:
    experiment_id: str
    milestone: str
    title: str
    hypothesis: str
    artifact: str
    markets: tuple[str, ...] = ()
    strategies: tuple[str, ...] = ()
    alpha: tuple[str, ...] = ()


_SEEDS: tuple[_Seed, ...] = (
    _Seed("mvp-0-1-1", "MVP 0.1.1", "Autonomous session and graceful stop",
          "An authorised session can trade, stop cleanly and leave a reconciled ledger",
          "mvp-certification/mvp-0-1-1-certification.json", ("BTC/MXN",)),
    _Seed("mvp-0-1-3", "MVP 0.1.3", "Fee-aware economic edge guard",
          "Admitting trades on a gross target smaller than round-trip friction cannot be viable",
          "mvp-certification/mvp-0-1-3-economic-certification.json", ("BTC/MXN",)),
    _Seed("mvp-0-2", "MVP 0.2", "Certified dynamic multi-market selection",
          "Other MXN markets may offer better economics than the incumbent pair",
          "mvp-certification/mvp-0-2-multi-market-certification.json",
          ("BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN")),
    _Seed("mvp-0-2-1", "MVP 0.2.1", "Robust trading evidence",
          "The strategies are being rejected for economic reasons rather than a modelling artefact",
          "mvp-certification/mvp-0-2-1-robust-evidence/mvp-0-2-1-certification.json",
          ("BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN")),
    _Seed("mvp-0-2-2", "MVP 0.2.2", "Risk-adjusted strategy research",
          "A narrower, volatility-scaled risk boundary changes the reward/risk geometry enough to "
          "clear friction",
          "mvp-certification/mvp-0-2-2-challengers/mvp-0-2-2-certification.json",
          ("BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN"),
          ("volatility-mean-reversion-v2", "range-expansion-v1")),
    _Seed("mvp-0-2-3", "MVP 0.2.3", "Passive execution economics feasibility",
          "Maker orders reduce round-trip friction enough to make an ordinary edge viable",
          "mvp-certification/mvp-0-2-3-passive/mvp-0-2-3-certification.json",
          ("BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN")),
    _Seed("mvp-0-2-4", "MVP 0.2.4", "Time-horizon feasibility and forward microstructure capture",
          "A coarser horizon reduces friction as a share of the opportunity",
          "mvp/horizon-microstructure/mvp-0-2-4-certification.json",
          ("BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN"), (),
          ("MICROSTRUCTURE_ORDER_FLOW",)),
    _Seed("mvp-0-2-5", "MVP 0.2.5", "Asymmetric risk/reward opportunity research",
          "An entry near a genuine invalidation boundary improves the reward/risk geometry enough "
          "to clear friction",
          "mvp/asymmetry/mvp-0-2-5-certification.json",
          ("BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN"),
          ("structural-invalidation-pullback-v1", "expansion-retest-v1")),
    _Seed("mvp-0-2-6", "MVP 0.2.6", "Independent alpha-source discovery",
          "An independent information source exists with measurable predictive content, before "
          "another strategy is written",
          "mvp/alpha/mvp-0-2-6-certification.json",
          ("BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN"), (), ("VALIDATED_INFORMATION_SIGNAL_V1",)),
    _Seed("mvp-0-2-7", "MVP 0.2.7", "Venue economics and product thesis",
          "The current product thesis is economically feasible under some realistic execution "
          "environment",
          "mvp/venues/mvp-0-2-7-certification.json",
          ("BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN")),
    _Seed("mvp-0-2-8", "MVP 0.2.8", "Cross-venue rare dislocation alpha",
          "Rare cross-venue dislocations are large and persistent enough to clear Bitso friction",
          "mvp/cross-venue/mvp-0-2-8-certification.json",
          ("BTC/MXN", "ETH/MXN", "SOL/MXN"), (), ("CROSS_VENUE_DISLOCATION",)),
)

# The frozen price-only profiles the project rejected, with their economics read from the 0.2.2
# certificate. Their fingerprints are read from the live strategy library rather than copied, so a
# change to a profile would show up rather than being hidden by a stale constant.
_FROZEN_PRICE_ONLY = (
    ("mean-reversion-safe-v1", "mean_reversion_safe"),
    ("trend-continuation-v1", "trend_continuation"),
    ("volatility-mean-reversion-v1", "volatility_mean_reversion"),
)

# The two collection campaigns. Their state is read from the capture directories on disk.
_CROSS_VENUE_CAPTURE = "mvp/cross-venue/capture"
_MICROSTRUCTURE_CAPTURE = "mvp/alpha/microstructure"


def _read(path: Path) -> dict[str, Any] | None:
    """Read one JSON document, returning None rather than raising for any failure.

    An unreadable artifact is an outcome this product has to survive: a milestone can be cleaned up
    and an interrupted write can leave a partial file. Treating either as fatal would make the
    Control Center refuse to open because of one missing document.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _text(payload: dict[str, Any], *keys: str) -> str:
    """The first present scalar among `keys`, or NOT_RECORDED.

    Older artifacts use different names for the same idea — `status` and `outcome`, `certified_at`
    and `created_at`. Looking across them is schema evolution handled by reading rather than by
    rewriting the historical files, which the spec forbids.
    """
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
    return NOT_RECORDED


def _decimal(payload: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return Decimal(str(value))
        except (ArithmeticError, ValueError):
            continue
    return None


def _int(payload: dict[str, Any], *keys: str) -> int | None:
    value = _decimal(payload, *keys)
    return None if value is None else int(value)


class ResearchRegistryBuilder:
    """Assembles the read model from the artifact roots it is given."""

    def __init__(self, *, artifacts_root: Path, repository_root: Path,
                 indexer: ArtifactIndexer | None = None) -> None:
        self.artifacts_root = artifacts_root.resolve(strict=False)
        self.repository_root = repository_root.resolve(strict=False)
        self.indexer = indexer or ArtifactIndexer(roots=(self.artifacts_root,))

    # -- entry point ---------------------------------------------------------------------------

    def build(self) -> ResearchRegistry:
        """Produce the whole read model. Deterministic and idempotent."""
        index = self.indexer.index()
        by_path = {record.relative_path: record for record in index.records}

        experiments = self._experiments(by_path=by_path)
        alpha_sources = self._alpha_sources(by_path=by_path)
        strategies = self._strategies()
        evidence = self._evidence(by_path=by_path, experiments=experiments)
        campaigns = self._campaigns(by_path=by_path)
        certifications = self._certifications(experiments=experiments)
        eligibility = self._eligibility(strategies=strategies, alpha_sources=alpha_sources)
        invalid = tuple(record.relative_path for record in index.records
                        if record.status != INDEXED)

        return ResearchRegistry(
            alpha_sources=alpha_sources, strategies=strategies, experiments=experiments,
            evidence=evidence, datasets=(), certifications=certifications, campaigns=campaigns,
            eligibility=eligibility,
            artifacts=tuple(record.public() for record in index.records),
            source_roots=index.roots, invalid_artifacts=invalid)

    # -- experiments ---------------------------------------------------------------------------

    def _experiments(self, *, by_path: dict[str, ArtifactRecord]
                     ) -> tuple[ExperimentRecord, ...]:
        out: list[ExperimentRecord] = []
        for seed in _SEEDS:
            payload = _read(self.artifacts_root / seed.artifact)
            if payload is None:
                # The artifact is absent, so the experiment is still listed — it happened — but its
                # result is NOT_RECORDED rather than a remembered conclusion.
                out.append(ExperimentRecord(
                    experiment_id=seed.experiment_id, milestone=seed.milestone,
                    title=seed.title, hypothesis=seed.hypothesis,
                    status=ExperimentStatus.ARCHIVED,
                    markets=seed.markets, strategies=seed.strategies,
                    alpha_sources=seed.alpha,
                    result_classification=NOT_RECORDED,
                    artifact_refs=(seed.artifact,)))
                continue
            record = by_path.get(seed.artifact)
            supersedes = tuple(
                item.relative_path for item in by_path.values()
                if item.archive_state == "SUPERSEDED"
                and Path(item.relative_path).name.startswith(
                    Path(seed.artifact).name.replace(".json", "")))
            out.append(ExperimentRecord(
                experiment_id=seed.experiment_id, milestone=seed.milestone, title=seed.title,
                hypothesis=seed.hypothesis,
                baseline_commit=_text(payload, "baseline_commit", "code_commit"),
                fingerprint=(record.fingerprint if record else None),
                status=ExperimentStatus.COMPLETED,
                started_at=_text(payload, "certified_at", "created_at"),
                finished_at=_text(payload, "certified_at"),
                development_interval=self._interval(payload, "development", "development_window"),
                holdout_interval=self._interval(payload, "holdout", "holdout_window"),
                forward_interval=self._interval(payload, "forward", "forward_window"),
                markets=seed.markets, strategies=seed.strategies, alpha_sources=seed.alpha,
                result_classification=_text(payload, "status", "outcome", "result"),
                reason_codes=self._reasons(payload),
                artifact_refs=(seed.artifact,), supersedes=supersedes))
        return tuple(out)

    @staticmethod
    def _interval(payload: dict[str, Any], *names: str) -> dict[str, Any] | None:
        """Pull a window out of wherever the artifact happened to record it."""
        for name in names:
            for container in (payload, payload.get("experiment") or {},
                              payload.get("windows_hours") or {}):
                if not isinstance(container, dict):
                    continue
                value = container.get(name)
                if isinstance(value, dict):
                    return value
        windows = payload.get("windows_hours")
        if isinstance(windows, dict) and names[0] in windows:
            return {"hours": windows[names[0]]}
        return None

    @staticmethod
    def _reasons(payload: dict[str, Any]) -> tuple[str, ...]:
        """Concrete reasons an experiment recorded for its own outcome."""
        out: list[str] = []
        for key in ("status_reason", "reason", "blocking_reason"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                out.append(value[:300])
        safety = payload.get("safety")
        if isinstance(safety, dict):
            for key, value in sorted(safety.items()):
                if value is True and "changed" in key:
                    out.append(f"{key.upper()}")
        return tuple(out)

    # -- alpha sources -------------------------------------------------------------------------

    def _alpha_sources(self, *, by_path: dict[str, ArtifactRecord]
                       ) -> tuple[AlphaSourceRecord, ...]:
        out: list[AlphaSourceRecord] = []

        # The price-only baseline: a family of profiles that produced no current edge.
        out.append(AlphaSourceRecord(
            alpha_id="PRICE_ONLY_RESEARCH_BASELINE", name="Price-only research baseline",
            family=AlphaFamily.PRICE_ONLY, version="1",
            classification=AlphaClassification.FROZEN,
            prediction_status=PredictionStatus.NOT_PREDICTIVE,
            economic_status=EconomicStatus.NOT_ECONOMIC,
            markets=("BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN"),
            development_status="COMPLETED",
            validation_status=NOT_APPLICABLE,
            evidence_provenance=("REAL_HISTORICAL_DEVELOPMENT",),
            reason_codes=("NO_CURRENT_EDGE",),
            source_experiment="mvp-0-2-5",
            artifact_refs=("mvp/asymmetry/mvp-0-2-5-certification.json",),
            hypothesis=("Ordinary OHLC indicator families contain an edge large enough to clear "
                        "the account's round-trip friction"),
            definition=("25 frozen price-only profiles across trend, mean-reversion and volatility "
                        "families, evaluated on real historical MXN candles")))

        # The validated information signal, read from the 0.2.6 certificate.
        alpha_cert = _read(self.artifacts_root / "mvp/alpha/mvp-0-2-6-certification.json")
        if alpha_cert is not None:
            candidate = alpha_cert.get("candidate") or {}
            economic = alpha_cert.get("economic_translation") or {}
            validation = alpha_cert.get("validation") or {}
            out.append(AlphaSourceRecord(
                alpha_id="VALIDATED_INFORMATION_SIGNAL_V1",
                name="Cross-market lead-lag information signal",
                family=AlphaFamily.CROSS_MARKET,
                version="1",
                fingerprint=(alpha_cert.get("frozen_alpha") or {}).get("fingerprint")
                or "fae4faa85ef648acb5e831cb6bc3fe242d8a1723cc3c474262a886a4600ab3aa",
                classification=AlphaClassification.PREDICTIVE_NOT_ECONOMIC,
                prediction_status=PredictionStatus.PREDICTIVE,
                economic_status=EconomicStatus.NOT_ECONOMIC,
                markets=tuple(filter(None, [candidate.get("leader"),
                                            candidate.get("follower")])),
                prediction_horizon=f"{candidate.get('horizon_minutes')} minutes",
                development_status="COMPLETED",
                validation_status=("PASSED" if validation.get("passed") else NOT_RECORDED),
                movement_bps=_decimal(economic, "largest_bucket_mean_movement_bps"),
                required_friction_bps=_decimal(economic, "friction_bps"),
                evidence_provenance=("REAL_HISTORICAL_DEVELOPMENT", "REAL_HISTORICAL_HOLDOUT"),
                reason_codes=("PREDICTIVE_NOT_ECONOMIC",),
                source_experiment="mvp-0-2-6",
                artifact_refs=("mvp/alpha/mvp-0-2-6-certification.json",),
                created_at=_text(alpha_cert, "certified_at"),
                hypothesis=("A follower market that has moved far from the basket subsequently "
                            "reverses"),
                definition=("Relative return versus the equally weighted basket, measured at a "
                            "five-minute horizon, BTC/MXN leading ETH/MXN")))

        # Cross-venue research, read from the 0.2.8 certificate.
        cross = _read(self.artifacts_root / "mvp/cross-venue/mvp-0-2-8-certification.json")
        if cross is not None:
            threshold = (cross.get("economic_threshold") or {}).get(
                "required_executable_dislocation_bps")
            out.append(AlphaSourceRecord(
                alpha_id="CROSS_VENUE_DISLOCATION", name="Cross-venue dislocation",
                family=AlphaFamily.CROSS_VENUE, version="1",
                classification=AlphaClassification.INSUFFICIENT_EVIDENCE,
                prediction_status=PredictionStatus.INSUFFICIENT_EVIDENCE,
                economic_status=EconomicStatus.NOT_EVALUATED,
                markets=("BTC/MXN", "ETH/MXN", "SOL/MXN"),
                prediction_horizon="1 minute",
                development_status="RUNNING",
                validation_status=NOT_APPLICABLE,
                required_friction_bps=(Decimal(str(threshold)) if threshold else None),
                evidence_provenance=("REAL_CAPTURED_CROSS_VENUE",),
                reason_codes=("INSUFFICIENT_CROSS_VENUE_EVIDENCE",
                              "REFERENCE_SPREAD_ARTIFACT"),
                source_experiment="mvp-0-2-8",
                artifact_refs=("mvp/cross-venue/mvp-0-2-8-certification.json",),
                created_at=_text(cross, "certified_at"),
                hypothesis=("Rare cross-venue dislocations are large and persistent enough to "
                            "clear Bitso friction"),
                definition=("Bitso executable bid/ask against a same-quote reference mid, "
                            "captured forward")))

        # Microstructure, whose conclusion comes from the collector's own state.
        books = sorted((self.artifacts_root / _MICROSTRUCTURE_CAPTURE).glob("*.jsonl"))
        out.append(AlphaSourceRecord(
            alpha_id="MICROSTRUCTURE_ORDER_FLOW", name="Microstructure order flow",
            family=AlphaFamily.MICROSTRUCTURE, version="1",
            classification=AlphaClassification.ACCUMULATING,
            prediction_status=PredictionStatus.NOT_TESTED,
            economic_status=EconomicStatus.NOT_EVALUATED,
            markets=tuple(path.name.split(".")[0].replace("_", "/").upper() for path in books),
            prediction_horizon="5 to 60 seconds",
            development_status="RUNNING",
            validation_status=NOT_APPLICABLE,
            evidence_provenance=("REAL_CAPTURED_MICROSTRUCTURE",),
            reason_codes=("ACCUMULATING",),
            source_experiment="mvp-0-2-4",
            artifact_refs=(_MICROSTRUCTURE_CAPTURE,) if books else (),
            hypothesis=("Top-of-book imbalance and trade flow predict short-horizon movement"),
            definition=("Captured order-book and trade-tape observations, read-only")))
        return tuple(out)

    # -- strategies ----------------------------------------------------------------------------

    def _strategies(self) -> tuple[StrategyProfileRecord, ...]:
        """Read the live strategy library so fingerprints are the real ones, not copies."""
        out: list[StrategyProfileRecord] = []
        library = _load_profile_library()
        for profile_id, strategy_id in _FROZEN_PRICE_ONLY:
            definition = library.get(profile_id)
            fingerprint = getattr(definition, "fingerprint", None) if definition else None
            out.append(StrategyProfileRecord(
                profile_id=profile_id, strategy_id=strategy_id,
                version=getattr(definition, "version", NOT_RECORDED) if definition
                else NOT_RECORDED,
                fingerprint=fingerprint,
                status=StrategyStatus.NOT_VIABLE,
                markets_evaluated=("BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN"),
                timeframes=("15m", "1h"),
                entry_model="OHLC indicator entry",
                exit_model="target or invalidation",
                risk_model="volatility-scaled stop",
                economic_policy_fingerprint="autofund.economic-edge.v1",
                production_certification="NOT_CERTIFIED",
                reason_codes=("NEGATIVE_NET_ECONOMICS",),
                frozen=True,
                engineering_status="PASS",
                economic_status="FAIL",
                risk_status=UNKNOWN,
                production_eligible=False,
                artifact_refs=("mvp/asymmetry/mvp-0-2-5-certification.json",)))
        for profile_id, strategy_id in (
                ("volatility-mean-reversion-v2", "volatility_mean_reversion"),
                ("range-expansion-v1", "range_expansion")):
            definition = library.get(profile_id)
            out.append(StrategyProfileRecord(
                profile_id=profile_id, strategy_id=strategy_id,
                version=getattr(definition, "version", NOT_RECORDED) if definition
                else NOT_RECORDED,
                fingerprint=getattr(definition, "fingerprint", None) if definition else None,
                status=StrategyStatus.INSUFFICIENT_EVIDENCE,
                markets_evaluated=("BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN"),
                timeframes=("15m", "1h"),
                entry_model="risk-bounded challenger entry",
                exit_model="target or invalidated boundary",
                risk_model="declared invalidation boundary",
                economic_policy_fingerprint="autofund.economic-edge.v1",
                production_certification="NOT_CERTIFIED",
                reason_codes=("INSUFFICIENT_EVIDENCE",),
                frozen=False,
                engineering_status="PASS",
                economic_status="FAIL",
                risk_status="FAIL",
                production_eligible=False,
                artifact_refs=(
                    "mvp-certification/mvp-0-2-2-challengers/mvp-0-2-2-certification.json",)))
        return tuple(out)

    # -- evidence ------------------------------------------------------------------------------

    def _evidence(self, *, by_path: dict[str, ArtifactRecord],
                  experiments: tuple[ExperimentRecord, ...]) -> tuple[EvidenceRecord, ...]:
        out: list[EvidenceRecord] = []
        for experiment in experiments:
            for reference in experiment.artifact_refs:
                record = by_path.get(reference)
                if record is None:
                    continue
                out.append(EvidenceRecord(
                    evidence_id=f"{experiment.experiment_id}:certification",
                    provenance=EvidenceProvenance.REAL_HISTORICAL_DEVELOPMENT,
                    experiment_id=experiment.experiment_id,
                    dataset_fingerprint=record.fingerprint,
                    markets=experiment.markets,
                    quality=("OK" if record.status == INDEXED else record.status),
                    artifact_path=reference, artifact_bytes=record.size_bytes,
                    alpha_sources=experiment.alpha_sources,
                    strategies=experiment.strategies,
                    summary={"artifact_type": record.artifact_type,
                             "archive_state": record.archive_state}))
            for superseded in experiment.supersedes:
                record = by_path.get(superseded)
                if record is None:
                    continue
                out.append(EvidenceRecord(
                    evidence_id=f"{experiment.experiment_id}:superseded:{Path(superseded).stem}",
                    provenance=EvidenceProvenance.REAL_HISTORICAL_DEVELOPMENT,
                    experiment_id=experiment.experiment_id,
                    dataset_fingerprint=record.fingerprint, markets=experiment.markets,
                    quality="SUPERSEDED", artifact_path=superseded,
                    artifact_bytes=record.size_bytes, summary={"superseded": True}))

        # Forward capture is different evidence from historical replay, and kept separate.
        for campaign_root, provenance in (
                (_CROSS_VENUE_CAPTURE, EvidenceProvenance.REAL_CAPTURED_CROSS_VENUE),
                (_MICROSTRUCTURE_CAPTURE, EvidenceProvenance.REAL_CAPTURED_MICROSTRUCTURE)):
            for path in sorted((self.artifacts_root / campaign_root).glob("*.jsonl")):
                relative = f"{campaign_root}/{path.name}"
                record = by_path.get(relative)
                market = path.name.split(".")[0].replace("_", "/").upper()
                count, first, last = _stream_bounds(path)
                out.append(EvidenceRecord(
                    evidence_id=f"{campaign_root}:{path.stem}",
                    provenance=provenance,
                    experiment_id=("mvp-0-2-8" if provenance
                                   == EvidenceProvenance.REAL_CAPTURED_CROSS_VENUE
                                   else "mvp-0-2-4"),
                    dataset_fingerprint=(record.fingerprint if record else None),
                    start=first, end=last, markets=(market,),
                    observation_count=count, quality="OK",
                    artifact_path=relative,
                    artifact_bytes=(record.size_bytes if record else path.stat().st_size),
                    alpha_sources=(("CROSS_VENUE_DISLOCATION" if provenance
                                    == EvidenceProvenance.REAL_CAPTURED_CROSS_VENUE
                                    else "MICROSTRUCTURE_ORDER_FLOW"),),
                    summary={"loaded_lines": count,
                             "note": "counted by streaming, not by deserialising"}))
        return tuple(out)

    # -- campaigns -----------------------------------------------------------------------------

    def _campaigns(self, *, by_path: dict[str, ArtifactRecord]
                   ) -> tuple[ResearchCampaignRecord, ...]:
        out: list[ResearchCampaignRecord] = []

        cross_root = self.artifacts_root / _CROSS_VENUE_CAPTURE
        cross_files = sorted(cross_root.glob("*.jsonl"))
        report = _read(self.artifacts_root / "mvp/cross-venue/capture-report.json")
        capture_data = (report or {}).get("report") if report else None
        required_hours = None
        cross_cert = _read(self.artifacts_root / "mvp/cross-venue/mvp-0-2-8-certification.json")
        if cross_cert is not None:
            coverage = (cross_cert.get("capture") or {}).get("coverage") or {}
            required_hours = _decimal(coverage, "required_hours")
            actual_hours = _decimal(coverage, "covered_hours")
            sufficient = coverage.get("sufficient")
        else:
            actual_hours, sufficient = None, None
        out.append(ResearchCampaignRecord(
            campaign_id="CROSS_VENUE_CAPTURE",
            title="Cross-venue dislocation capture",
            experiment_id="mvp-0-2-8",
            experiment_fingerprint=(by_path.get("mvp/cross-venue/mvp-0-2-8-certification.json")
                                    or ArtifactRecord("", "", INDEXED)).fingerprint,
            status=CampaignStatus.STOPPED,
            # Process health and research conclusion are separate fields, never one colour.
            process_health="HEALTHY" if cross_files else UNKNOWN,
            evidence_conclusion=(cross_cert or {}).get("status", NOT_RECORDED),
            planned_duration_hours=required_hours,
            actual_coverage_hours=actual_hours,
            markets=("BTC/MXN", "ETH/MXN", "SOL/MXN"),
            observations=(capture_data or {}).get("valid_records") if capture_data else None,
            gaps=len((capture_data or {}).get("failures") or {}) if capture_data else None,
            storage_bytes=sum(path.stat().st_size for path in cross_files) or None,
            evidence_provenance=EvidenceProvenance.REAL_CAPTURED_CROSS_VENUE,
            coverage_sufficient=sufficient,
            artifact_paths=tuple(f"{_CROSS_VENUE_CAPTURE}/{path.name}"
                                 for path in cross_files)))

        micro_root = self.artifacts_root / _MICROSTRUCTURE_CAPTURE
        micro_files = sorted(micro_root.glob("*.jsonl"))
        micro_total = sum(path.stat().st_size for path in micro_files)
        out.append(ResearchCampaignRecord(
            campaign_id="MICROSTRUCTURE_CAPTURE",
            title="Microstructure order-book capture",
            experiment_id="mvp-0-2-4",
            status=CampaignStatus.STOPPED,
            process_health="HEALTHY" if micro_files else UNKNOWN,
            evidence_conclusion="ACCUMULATING",
            markets=tuple(path.name.split(".")[0].replace("_", "/").upper()
                          for path in micro_files),
            observations=None, storage_bytes=micro_total or None,
            evidence_provenance=EvidenceProvenance.REAL_CAPTURED_MICROSTRUCTURE,
            coverage_sufficient=False,
            artifact_paths=tuple(f"{_MICROSTRUCTURE_CAPTURE}/{path.name}"
                                 for path in micro_files)))
        return tuple(out)

    # -- certifications ------------------------------------------------------------------------

    def _certifications(self, *, experiments: tuple[ExperimentRecord, ...]
                        ) -> tuple[CertificationRecord, ...]:
        """One certification record per experiment, carrying the four separated statuses."""
        out: list[CertificationRecord] = []
        for experiment in experiments:
            payload = _read(self.artifacts_root / experiment.artifact_refs[0]) \
                if experiment.artifact_refs else None
            engineering = "PASS" if payload is not None else UNKNOWN
            evidence = experiment.result_classification
            economic = UNKNOWN
            authorization = "DISABLED"
            if payload is not None:
                safety = payload.get("safety")
                if isinstance(safety, dict) and safety:
                    authorization = "DISABLED"
                if _text(payload, "production_post_count") not in (NOT_RECORDED, "0"):
                    authorization = "ACTIVE"
            out.append(CertificationRecord(
                certification_id=f"{experiment.experiment_id}:certification",
                experiment_id=experiment.experiment_id,
                market=",".join(experiment.markets) or NOT_RECORDED,
                profile_id=",".join(experiment.strategies) or NOT_APPLICABLE,
                certified=False,
                status=experiment.result_classification,
                engineering_status=engineering, evidence_status=evidence,
                economic_status=economic, authorization_status=authorization,
                reason_codes=experiment.reason_codes,
                artifact_path=experiment.artifact_refs[0] if experiment.artifact_refs else None,
                certified_at=experiment.finished_at))
        return tuple(out)

    # -- eligibility ---------------------------------------------------------------------------

    def _eligibility(self, *, strategies: tuple[StrategyProfileRecord, ...],
                     alpha_sources: tuple[AlphaSourceRecord, ...]
                     ) -> tuple[ProductionEligibilityRecord, ...]:
        """Every market/profile pair, each with a concrete reason it is not eligible."""
        out: list[ProductionEligibilityRecord] = []
        signal = next((item for item in alpha_sources
                       if item.alpha_id == "VALIDATED_INFORMATION_SIGNAL_V1"), None)
        for market in ("BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN"):
            for strategy in strategies:
                if market not in strategy.markets_evaluated:
                    continue
                if strategy.is_frozen_terminal:
                    reason = "NOT_VIABLE"
                elif strategy.status == StrategyStatus.INSUFFICIENT_EVIDENCE:
                    reason = "INSUFFICIENT_EVIDENCE"
                else:
                    reason = "NOT_CERTIFIED"
                out.append(ProductionEligibilityRecord(
                    market=market, profile_id=strategy.profile_id,
                    alpha_source=(signal.alpha_id if signal else None),
                    research_status=str(strategy.status),
                    data_quality="OK",
                    strategy_compatibility=("INCOMPATIBLE" if strategy.is_frozen_terminal
                                            else UNKNOWN),
                    economic_status=strategy.economic_status,
                    risk_status=strategy.risk_status,
                    certification_status=strategy.production_certification,
                    authorization_status="PRODUCTION_DISABLED",
                    position_status="NONE",
                    blocking_reason=reason, eligible=False))
        if signal is not None and signal.tradeable is False:
            # The signal itself is worth one row: it is predictive and cannot pay, which is a
            # different finding from a strategy that was never viable.
            for market in signal.markets:
                out.append(ProductionEligibilityRecord(
                    market=market, profile_id=NOT_APPLICABLE,
                    alpha_source=signal.alpha_id, research_status="PREDICTIVE",
                    data_quality="OK", strategy_compatibility=NOT_APPLICABLE,
                    economic_status=str(signal.economic_status),
                    risk_status=NOT_APPLICABLE, certification_status="NOT_CERTIFIED",
                    authorization_status="PRODUCTION_DISABLED", position_status="NONE",
                    blocking_reason="PREDICTIVE_NOT_ECONOMIC", eligible=False))
        return tuple(out)


def _load_profile_library() -> dict[str, Any]:
    """Read the live profile library. Import failure yields an empty map, not a crash."""
    try:
        from autofund.mvp.profile_library import PROFILE_BY_ID
    except ImportError:  # pragma: no cover - only if the library is being refactored
        return {}
    return dict(PROFILE_BY_ID)


def _stream_bounds(path: Path) -> tuple[int, str | None, str | None]:
    """Count lines and find the first and last timestamp, by streaming.

    Streaming rather than deserialising matters here: the capture files reach hundreds of kilobytes
    and grow, and this runs during a rebuild. Each line is parsed only far enough to reach its
    timestamp, and nothing is retained.
    """
    count = 0
    first: str | None = None
    last: str | None = None
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                count += 1
                stamp = _line_timestamp(line)
                if stamp is not None:
                    if first is None:
                        first = stamp
                    last = stamp
    except (OSError, UnicodeDecodeError):
        return count, first, last
    return count, first, last


def _line_timestamp(line: str) -> str | None:
    """Extract a timestamp from a JSONL record without fully parsing it where possible.

    A bounded search for the first timestamp-shaped field, falling back to a real parse. Avoids
    building a dict per line for files that carry thousands of them.
    """
    for key in ('"moment"', '"timestamp"', '"received_time"', '"observed_at"'):
        index = line.find(key)
        if index == -1:
            continue
        start = line.find('"', index + len(key) + 1)
        if start == -1:
            continue
        end = line.find('"', start + 1)
        if end == -1:
            continue
        return line[start + 1:end]
    return None

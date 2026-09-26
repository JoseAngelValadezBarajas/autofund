"""Read-only research endpoints for the Control Center.

**There is no write endpoint here, and that is a design commitment rather than an omission.** The
Control Center answers questions; it does not place orders, change limits, promote profiles or edit
research. The existing control plane owns session lifecycle, and the research API is kept separate
from it so that no future change to a dashboard can accidentally acquire the ability to move money.
A test asserts the router declares only GET routes.

**The registry is built once and served from memory.** The artifacts behind it reach 5.5 MB, and
re-parsing them per request is the specific performance failure the spec warns about. So the router
holds a single immutable read model and every endpoint projects from it, which keeps a dashboard
response in the microseconds and makes the artifact cost a startup cost instead of a per-view one.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .models import NOT_RECORDED, ResearchRegistry
from .overview import build_overview
from .registry import ResearchRegistryBuilder

RESEARCH_API_VERSION = "autofund.research-api.v1"

# How many records a list endpoint will return before requiring pagination. Kept modest because the
# UI is a dense engineering view, not an export tool.
DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 500


def _page(items: tuple[Any, ...], *, limit: int, offset: int) -> dict[str, Any]:
    """Slice a tuple for transport, reporting the totals so the client can page honestly."""
    if limit <= 0 or limit > MAX_PAGE_SIZE:
        raise HTTPException(status_code=422, detail=f"limit must be 1..{MAX_PAGE_SIZE}")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be non-negative")
    window = items[offset:offset + limit]
    return {"total": len(items), "offset": offset, "limit": limit,
            "returned": len(window), "items": [item for item in window]}


class ResearchService:
    """Holds the built read model and projects from it.

    Deliberately an object rather than module-level globals so tests can hand it a registry built
    from fixture roots, and so the application controls its lifetime.
    """

    def __init__(self, *, registry: ResearchRegistry, orchestrator: Any) -> None:
        self.registry = registry
        self.orchestrator = orchestrator

    def overview(self) -> dict[str, Any]:
        """The whole dashboard, from one consistent snapshot of the read model."""
        snapshot = self.orchestrator.snapshot() if self.orchestrator is not None else {}
        unresolved = _unresolved_orders(snapshot)
        return build_overview(registry=self.registry, snapshot=snapshot,
                             unresolved_orders=unresolved).public()

    def timeline(self, *, limit: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
        """A read-only activity timeline of research and production events.

        Assembled from the registry and the runtime's own telemetry rather than from a second store.
        Financial events are *referenced* where they already exist and never duplicated, because two
        records of the same fill would eventually disagree.
        """
        events: list[dict[str, Any]] = []
        for experiment in self.registry.experiments:
            if experiment.finished_at:
                events.append({
                    "at": experiment.finished_at, "kind": "EXPERIMENT_COMPLETED",
                    "subject": experiment.experiment_id,
                    "detail": experiment.result_classification,
                    "source": "research registry"})
            for superseded in experiment.supersedes:
                events.append({
                    "at": experiment.finished_at, "kind": "ARTIFACT_SUPERSEDED",
                    "subject": superseded, "detail": "archived by the project",
                    "source": "artifact index"})
        for alpha in self.registry.alpha_sources:
            if alpha.created_at:
                events.append({
                    "at": alpha.created_at,
                    "kind": ("ALPHA_VALIDATED" if alpha.prediction_status.value == "PREDICTIVE"
                             else "ALPHA_CLASSIFIED"),
                    "subject": alpha.alpha_id, "detail": str(alpha.classification),
                    "source": "alpha registry"})
        for campaign in self.registry.campaigns:
            if campaign.latest_sample_at:
                events.append({
                    "at": campaign.latest_sample_at, "kind": "COLLECTOR_SAMPLE",
                    "subject": campaign.campaign_id, "detail": campaign.process_health,
                    "source": "campaign registry"})
        # Financial and session events are deliberately absent from this timeline. They already
        # have an authoritative source in the production journal, and copying them here would create
        # a second record of the same fill that could eventually disagree with the first.
        events.sort(key=lambda item: str(item.get("at") or ""), reverse=True)
        return {**_page(tuple(events), limit=limit, offset=0),
                "read_only": True,
                "financial_events_come_from_their_own_source": True,
                "financial_events_included_here": False}


def _unresolved_orders(snapshot: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    """Any Production order the ledger has not resolved.

    Read from the runtime's blocked-recovery state, which is the authoritative signal that an order
    is outstanding. Nothing here infers an order from a position.
    """
    blocked = snapshot.get("blocked_recovery") or {}
    origins = blocked.get("origins") or []
    return tuple({"origin_id": origin, "status": "UNRESOLVED"} for origin in origins)


def build_research_router(service: ResearchService) -> APIRouter:
    """Read-only routes. Every handler is a GET; see the module docstring for why."""
    router = APIRouter(prefix="/api/v1/research", tags=["research"])

    @router.get("/overview")
    def overview() -> dict[str, Any]:
        return service.overview()

    @router.get("/alpha")
    def alpha_list(
        family: str | None = Query(default=None),
        classification: str | None = Query(default=None),
        market: str | None = Query(default=None),
        limit: int = Query(default=DEFAULT_PAGE_SIZE),
        offset: int = Query(default=0),
    ) -> dict[str, Any]:
        items = service.registry.alpha_sources
        if family:
            items = tuple(item for item in items if str(item.family) == family)
        if classification:
            items = tuple(item for item in items if str(item.classification) == classification)
        if market:
            items = tuple(item for item in items if market in item.markets)
        return _page(tuple(item.public() for item in items), limit=limit, offset=offset)

    @router.get("/alpha/{alpha_id}")
    def alpha_detail(alpha_id: str) -> dict[str, Any]:
        record = service.registry.alpha(alpha_id)
        if record is None:
            raise HTTPException(status_code=404, detail="UNKNOWN_ALPHA")
        evidence = service.registry.evidence_for(alpha_id=alpha_id)
        experiments = tuple(item.public() for item in service.registry.experiments
                            if alpha_id in item.alpha_sources)
        strategies = tuple(item.public() for item in service.registry.strategies
                           if alpha_id in (item.alpha_dependency or ""))
        return {"alpha": record.public(),
                "evidence": [item.public() for item in evidence],
                "experiments": list(experiments),
                "associated_strategies": list(strategies),
                "predictive_is_not_tradeable": {
                    "prediction_status": str(record.prediction_status),
                    "economic_status": str(record.economic_status),
                    "tradeable": record.tradeable,
                    "note": ("predictive means the signal knows something; tradeable means it "
                             "could pay for itself. They are separate findings.")}}

    @router.get("/strategies")
    def strategies_list(
        status: str | None = Query(default=None),
        market: str | None = Query(default=None),
        production_eligible: bool | None = Query(default=None),
        limit: int = Query(default=DEFAULT_PAGE_SIZE),
        offset: int = Query(default=0),
    ) -> dict[str, Any]:
        items = service.registry.strategies
        if status:
            items = tuple(item for item in items if str(item.status) == status)
        if market:
            items = tuple(item for item in items if market in item.markets_evaluated)
        if production_eligible is not None:
            items = tuple(item for item in items
                          if item.production_eligible == production_eligible)
        return _page(tuple(item.public() for item in items), limit=limit, offset=offset)

    @router.get("/strategies/{profile_id}")
    def strategy_detail(profile_id: str) -> dict[str, Any]:
        record = service.registry.strategy(profile_id)
        if record is None:
            raise HTTPException(status_code=404, detail="UNKNOWN_PROFILE")
        evidence = service.registry.evidence_for(strategy=profile_id)
        eligibility = tuple(item.public() for item in service.registry.eligibility
                            if item.profile_id == profile_id)
        return {"strategy": record.public(),
                "evidence": [item.public() for item in evidence],
                "eligibility": list(eligibility),
                "truth_separation": {
                    "engineering": record.engineering_status,
                    "evidence": (record.development_evidence or {}).get("status", NOT_RECORDED),
                    "economics": record.economic_status,
                    "authorization": ("ELIGIBLE" if record.production_eligible
                                      else "DISABLED"),
                    "note": "four independent facts; none is derived from another"}}

    @router.get("/experiments")
    def experiments_list(
        status: str | None = Query(default=None),
        limit: int = Query(default=DEFAULT_PAGE_SIZE),
        offset: int = Query(default=0),
    ) -> dict[str, Any]:
        items = service.registry.experiments
        if status:
            items = tuple(item for item in items if str(item.status) == status)
        ordered = tuple(sorted(items, key=lambda item: item.finished_at or "",
                               reverse=True))
        return _page(tuple(item.public() for item in ordered), limit=limit, offset=offset)

    @router.get("/experiments/{experiment_id}")
    def experiment_detail(experiment_id: str) -> dict[str, Any]:
        record = service.registry.experiment(experiment_id)
        if record is None:
            raise HTTPException(status_code=404, detail="UNKNOWN_EXPERIMENT")
        evidence = service.registry.evidence_for(experiment_id=experiment_id)
        certifications = tuple(item.public() for item in service.registry.certifications
                               if item.experiment_id == experiment_id)
        return {"experiment": record.public(),
                "evidence": [item.public() for item in evidence],
                "certifications": list(certifications),
                "lineage": {
                    "hypothesis": record.hypothesis,
                    "experiment": record.experiment_id,
                    "datasets": [
                        {"provenance": str(item.provenance),
                         "artifact_path": item.artifact_path,
                         "observation_count": item.observation_count}
                        for item in evidence],
                    "conclusion": record.result_classification,
                    "reason_codes": list(record.reason_codes)}}

    @router.get("/evidence")
    def evidence_list(
        experiment_id: str | None = Query(default=None),
        alpha_id: str | None = Query(default=None),
        strategy: str | None = Query(default=None),
        market: str | None = Query(default=None),
        provenance: str | None = Query(default=None),
        limit: int = Query(default=DEFAULT_PAGE_SIZE),
        offset: int = Query(default=0),
    ) -> dict[str, Any]:
        items = service.registry.evidence_for(
            experiment_id=experiment_id, alpha_id=alpha_id, strategy=strategy,
            market=market, provenance=provenance)
        # Summaries only: an evidence row never carries a multi-megabyte payload to the browser.
        return _page(tuple(item.public() for item in items), limit=limit, offset=offset)

    @router.get("/campaigns")
    def campaigns_list() -> dict[str, Any]:
        return {"total": len(service.registry.campaigns), "offset": 0,
                "limit": len(service.registry.campaigns),
                "returned": len(service.registry.campaigns),
                "items": [item.public() for item in service.registry.campaigns],
                "health_is_separate_from_conclusion": True}

    @router.get("/artifacts")
    def artifacts_list(
        limit: int = Query(default=DEFAULT_PAGE_SIZE),
        offset: int = Query(default=0),
        artifact_type: str | None = Query(default=None),
        status: str | None = Query(default=None),
    ) -> dict[str, Any]:
        items = tuple(service.registry.artifacts)
        if artifact_type:
            items = tuple(item for item in items if item.get("artifact_type") == artifact_type)
        if status:
            items = tuple(item for item in items if item.get("status") == status)
        return _page(items, limit=limit, offset=offset)

    @router.get("/timeline")
    def timeline(limit: int = Query(default=DEFAULT_PAGE_SIZE)) -> dict[str, Any]:
        return service.timeline(limit=limit)

    @router.get("/eligibility")
    def eligibility_list(
        market: str | None = Query(default=None),
        eligible: bool | None = Query(default=None),
        limit: int = Query(default=DEFAULT_PAGE_SIZE),
        offset: int = Query(default=0),
    ) -> dict[str, Any]:
        items = service.registry.eligibility
        if market:
            items = tuple(item for item in items if item.market == market)
        if eligible is not None:
            items = tuple(item for item in items if item.eligible == eligible)
        return _page(tuple(item.public() for item in items), limit=limit, offset=offset)

    @router.get("/schema")
    def schema() -> dict[str, Any]:
        """The registry's own metadata, so a client can tell what it is reading."""
        return {"api_version": RESEARCH_API_VERSION, **service.registry.public()}

    return router


@lru_cache(maxsize=1)
def _default_service() -> ResearchService:
    """Build the registry once per process, lazily.

    Cached because the artifact walk costs about a second on a cold cache and the result is
    immutable for the process lifetime. A dashboard request must never pay that cost.
    """
    repository = Path(__file__).resolve().parents[3]
    registry = ResearchRegistryBuilder(artifacts_root=repository / "artifacts",
                                       repository_root=repository).build()
    try:
        from autofund.mvp.orchestrator import AutoFundOrchestrator

        orchestrator: Any = AutoFundOrchestrator(artifacts=repository / "artifacts" / "mvp"
                                                 / "runtime")
    except Exception:
        orchestrator = None
    return ResearchService(registry=registry, orchestrator=orchestrator)

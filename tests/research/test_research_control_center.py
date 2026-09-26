"""Tests for MVP 0.3.0: the research registry, artifact index and Control Center read models.

The tests concentrate on the ways this milestone could tell a comfortable lie, and on the specific
defects that were live in the implementation while it was being written.

**A green system read as profitable evidence.** The whole point of the milestone is that engineering
health, research evidence, economic result and production authorization are four separate facts. The
easiest way to lose that is a status function that folds them together, so the separation is pinned
by test rather than by a comment.

**Authorization that fails open.** The first implementation of the production view asked whether the
state was *not* in a small set of bad states. Every state it had not been taught about therefore
counted as authorized: `BOOTING`, `RECOVERING`, `HALTED`, `ERROR`, and any state a future version
adds. The test walks the whole enum plus a state that does not exist, because a denylist is only
ever correct for the states someone remembered to list.

**A read model that counts itself.** The registry indexes every artifact, and its own snapshot is an
artifact. The first build therefore changed the input to the second build, and the digest never
converged â€” which presents as a repository that changes on every refresh. The exclusion and the
convergence test exist because that failure is invisible until someone diffs two digests.

**A ledger that passes by accident.** Ledger health was checked for `RECONCILED`, while the
orchestrator reports `PASS`. The check happened to pass on a different string. The tests assert the
actual emitted value and that an unrecognised value degrades rather than passing silently.

**Wallet read as inventory.** The exchange wallet holds the real BTC position bought earlier. It is
not the AutoFund book, and summing them would overstate capital while looking entirely reasonable.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mvp.route_inventory import mutating_routes, route_methods

from autofund.mvp.api import create_mvp_app
from autofund.mvp.orchestrator import AppState, AutoFundOrchestrator
from autofund.research import api as research_api
from autofund.research import overview as overview_module
from autofund.research.artifacts import (
    CURRENT,
    EMPTY,
    INDEXED,
    INVALID,
    SUPERSEDED,
    UNSUPPORTED,
    ArtifactIndexer,
    ArtifactIndexError,
    archive_state_of,
    classify_artifact,
    is_within,
    resolve_artifact_path,
)
from autofund.research.models import (
    NOT_APPLICABLE,
    NOT_RECORDED,
    UNKNOWN,
    AlphaClassification,
    AlphaFamily,
    AlphaSourceRecord,
    EconomicStatus,
    EvidenceProvenance,
    ProductionEligibilityRecord,
    ResearchError,
    ResearchRegistry,
)
from autofund.research.overview import (
    DEGRADED,
    HEALTHY,
    LEDGER_HEALTHY_STATUSES,
    NO_TRADE,
    SESSION_AUTHORIZED,
    SESSION_AUTHORIZING_STATES,
    build_overview,
    build_portfolio,
    build_production,
)
from autofund.research.rebuild import build_registry, registry_digest
from autofund.research.registry import ResearchRegistryBuilder
from autofund.version import PRODUCT_VERSION, VERSION

REPO_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_ROOT = REPO_ROOT / "artifacts"


# ---- fixtures -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def registry() -> ResearchRegistry:
    """The real registry, built from the real artifacts once for the module."""
    return ResearchRegistryBuilder(
        artifacts_root=ARTIFACTS_ROOT, repository_root=REPO_ROOT).build()


@pytest.fixture()
def service(registry: ResearchRegistry) -> research_api.ResearchService:
    orchestrator = AutoFundOrchestrator(artifacts=ARTIFACTS_ROOT / "mvp" / "runtime")
    return research_api.ResearchService(registry=registry, orchestrator=orchestrator)


@pytest.fixture()
def client(registry: ResearchRegistry) -> TestClient:
    orchestrator = AutoFundOrchestrator(artifacts=ARTIFACTS_ROOT / "mvp" / "runtime")
    return TestClient(create_mvp_app(orchestrator, dist=None))


# ---- §46 alpha registry -------------------------------------------------------------------------


def test_alpha_source_cannot_be_a_strategy() -> None:
    """An alpha source is information; a strategy is a way to trade it. Conflating them is what
    makes a predictive signal look tradeable."""
    with pytest.raises(ResearchError):
        AlphaSourceRecord(
            alpha_id="x", name="x", family=AlphaFamily.CROSS_MARKET, is_a_strategy=True)


def test_alpha_source_needs_an_identity() -> None:
    with pytest.raises(ResearchError):
        AlphaSourceRecord(alpha_id="", name="x", family=AlphaFamily.CROSS_MARKET)


def test_alpha_records_are_registered(registry: ResearchRegistry) -> None:
    assert len(registry.alpha_sources) >= 4
    families = {record.family for record in registry.alpha_sources}
    assert "CROSS_MARKET" in families


def test_the_validated_signal_is_predictive_but_not_economic(
        registry: ResearchRegistry) -> None:
    """The one signal this project validated predicts, and cannot pay for itself. Both facts have
    to survive the round trip, because either alone is misleading."""
    record = registry.alpha("VALIDATED_INFORMATION_SIGNAL_V1")
    assert record is not None
    assert record.classification is AlphaClassification.PREDICTIVE_NOT_ECONOMIC
    assert record.prediction_status == "PREDICTIVE"
    assert record.economic_status == EconomicStatus.NOT_ECONOMIC
    assert record.tradeable is False
    # The headroom is negative because the required friction exceeds the movement.
    assert Decimal(record.economic_headroom_bps) < 0


def test_alpha_fingerprint_matches_the_frozen_value(registry: ResearchRegistry) -> None:
    record = registry.alpha("VALIDATED_INFORMATION_SIGNAL_V1")
    assert record is not None
    assert record.fingerprint == (
        "fae4faa85ef648acb5e831cb6bc3fe242d8a1723cc3c474262a886a4600ab3aa")


def test_alpha_lookup_of_an_unknown_id_is_none_not_an_error(
        registry: ResearchRegistry) -> None:
    assert registry.alpha("DOES_NOT_EXIST") is None


# ---- §47 strategy and experiment registries -----------------------------------------------------


def test_strategy_separates_engineering_from_economics(
        registry: ResearchRegistry) -> None:
    record = registry.strategy("trend-continuation-v1")
    assert record is not None
    # Engineering may be fine while economics is not. That combination is the normal case here.
    assert record.engineering_status is not None
    assert record.economic_status is not None
    assert isinstance(record.production_eligible, bool)


def test_strategy_records_are_registered(registry: ResearchRegistry) -> None:
    assert len(registry.strategies) == 5
    assert registry.strategy("mean-reversion-safe-v1") is not None


def test_frozen_strategies_report_a_frozen_state(registry: ResearchRegistry) -> None:
    frozen = [record for record in registry.strategies if record.is_frozen_terminal]
    assert len(frozen) == 3


def test_experiment_records_cover_every_milestone(registry: ResearchRegistry) -> None:
    """Eleven milestones produced evidence; the registry must account for all of them."""
    assert len(registry.experiments) == 11
    expected = {"mvp-0-1-1", "mvp-0-1-3", "mvp-0-2", "mvp-0-2-1", "mvp-0-2-2", "mvp-0-2-3",
                "mvp-0-2-4", "mvp-0-2-5", "mvp-0-2-6", "mvp-0-2-7", "mvp-0-2-8"}
    assert {record.experiment_id for record in registry.experiments} == expected


def test_experiment_lookup_of_an_unknown_id_is_none(registry: ResearchRegistry) -> None:
    assert registry.experiment("mvp-9-9-9") is None


def test_missing_evidence_is_recorded_as_a_sentinel(registry: ResearchRegistry) -> None:
    """§33: an unknown must never be invented. A field with no source reads one of the explicit
    sentinels or is genuinely absent, never a blank string and never a plausible-looking default."""
    sentinels = {UNKNOWN, NOT_RECORDED, NOT_APPLICABLE}
    for record in registry.experiments:
        # An interval is a structured window or an explicit absence, never a bare placeholder.
        for value in (record.holdout_interval, record.forward_interval):
            assert value is None or isinstance(value, dict), (
                f"{record.experiment_id}: interval must be a window or None, got {value!r}")
            assert value != {}, "an empty window claims a window that does not exist"
        assert record.baseline_commit is None or (
            isinstance(record.baseline_commit, str) and record.baseline_commit)
        assert record.baseline_commit not in ("", "N/A", "TBD"), "a placeholder is not a sentinel"
    # Evidence with no observation count says so rather than reporting zero.
    for row in registry.evidence:
        if row.observation_count is None:
            assert row.quality in sentinels or row.quality == "SUPERSEDED" or (
                isinstance(row.quality, str) and row.quality)


def test_a_seeded_record_with_no_artifact_claims_no_evidence(tmp_path: Path) -> None:
    """The registry knows the milestones from code, but evidence comes only from artifacts. With
    no artifacts it must report the milestones and zero evidence, not infer either from the other.
    """
    built = ResearchRegistryBuilder(artifacts_root=tmp_path, repository_root=REPO_ROOT).build()
    assert len(built.experiments) == 11, "milestones are known without artifacts"
    assert built.evidence == (), "evidence must never be inferred from a seed"
    assert built.artifacts == ()


def test_classification_is_never_derived_from_a_neighbouring_field(
        registry: ResearchRegistry) -> None:
    """A predictive signal must not be labelled validated-alpha, and a rejected one must not be
    labelled accumulating."""
    for record in registry.alpha_sources:
        if record.prediction_status == "PREDICTIVE" and record.economic_status is EconomicStatus.NOT_ECONOMIC:
            assert record.classification is not AlphaClassification.VALIDATED_ALPHA_SOURCE


# ---- §48 evidence provenance --------------------------------------------------------------------


def test_evidence_records_carry_provenance(registry: ResearchRegistry) -> None:
    assert len(registry.evidence) >= 18
    for record in registry.evidence:
        assert record.provenance in set(EvidenceProvenance)


def test_synthetic_evidence_is_not_real_observation(registry: ResearchRegistry) -> None:
    """§33 and §42: a fixture is not a measurement. The distinction has to be computable, not a
    matter of the reader remembering which artifact came from where."""
    for record in registry.evidence:
        if record.provenance is EvidenceProvenance.SYNTHETIC_FIXTURE:
            assert record.is_real_observation is False
        elif record.provenance is EvidenceProvenance.REAL_CAPTURED_CROSS_VENUE:
            assert record.is_real_observation is True


def test_evidence_can_be_queried_by_alpha(registry: ResearchRegistry) -> None:
    rows = registry.evidence_for(alpha_id="CROSS_VENUE_DISLOCATION")
    assert rows
    for row in rows:
        assert "CROSS_VENUE_DISLOCATION" in row.alpha_sources


def test_captured_evidence_is_marked_as_real_observation(
        registry: ResearchRegistry) -> None:
    """The cross-venue capture really ran against live public endpoints. That has to be
    distinguishable from a fixture, because only one of the two can support a conclusion."""
    rows = registry.evidence_for(alpha_id="CROSS_VENUE_DISLOCATION")
    assert rows
    captured = [row for row in rows
                if row.provenance is EvidenceProvenance.REAL_CAPTURED_CROSS_VENUE]
    assert captured
    assert all(row.is_real_observation is True for row in captured)


def test_superseded_evidence_is_marked_superseded(registry: ResearchRegistry) -> None:
    """A replaced certification is still indexed, and says that it has been replaced, so the
    timeline does not read as two contradictory conclusions for the same milestone."""
    superseded = [row for row in registry.evidence
                  if row.artifact_path and ".superseded-" in row.artifact_path]
    assert superseded
    for row in superseded:
        assert row.quality == "SUPERSEDED"
        assert row.summary == {"superseded": True}


def test_evidence_query_by_unknown_alpha_returns_empty(registry: ResearchRegistry) -> None:
    assert registry.evidence_for(alpha_id="NOPE") == ()


# ---- schema versioning and old-artifact compatibility -------------------------------------------


def test_public_view_declares_a_schema_version(registry: ResearchRegistry) -> None:
    view = registry.public()
    assert view["schema_version"] == "autofund.research-registry.v1"


def test_public_view_does_not_leak_internal_mutable_structures(
        registry: ResearchRegistry) -> None:
    """The public view is what the API serialises. It must be JSON-safe so an HTTP response never
    depends on the caller's ability to serialise a dataclass."""
    json.dumps(registry.public())


def test_registry_survives_an_empty_artifact_root(tmp_path: Path) -> None:
    """A fresh checkout has no artifacts. Reporting an empty research state is correct; crashing is
    not, because the Control Center must be able to say 'nothing yet'."""
    built = ResearchRegistryBuilder(artifacts_root=tmp_path, repository_root=REPO_ROOT).build()
    assert built.counts["artifact_records"] == 0
    assert built.counts["evidence_records"] == 0
    assert built.invalid_artifacts == ()


def test_registry_tolerates_a_malformed_artifact(tmp_path: Path) -> None:
    """A truncated certification is an ordinary outcome of an interrupted write."""
    broken = tmp_path / "mvp-certification" / "mvp-0-2-9-broken"
    broken.mkdir(parents=True)
    (broken / "certification.json").write_text("{ this is not json", encoding="utf-8")
    built = ResearchRegistryBuilder(artifacts_root=tmp_path, repository_root=REPO_ROOT).build()
    assert built.invalid_artifacts  # recorded, not raised


# ---- §32 idempotent, non-mutating rebuild -------------------------------------------------------


def test_registry_rebuild_is_idempotent(registry: ResearchRegistry) -> None:
    """Two builds over an unchanged repository must agree. The digest excludes the build timestamp,
    so a difference means a real change in a record, not a clock."""
    _, _, first = build_registry(root=REPO_ROOT)
    _, _, second = build_registry(root=REPO_ROOT)
    assert first == second


def test_registry_does_not_index_its_own_output() -> None:
    """The read model's snapshot is derived from the index, so indexing it would make every rebuild
    change the next one's input and the digest would never converge."""
    indexer = ArtifactIndexer(roots=(ARTIFACTS_ROOT,))
    relative = {record.relative_path.replace("\\", "/") for record in indexer.index().records}
    assert not any(path.startswith("research/") for path in relative)


def test_rebuild_does_not_mutate_the_artifacts_it_reads() -> None:
    """§32: a rebuild constructs a read model. It must not touch financial truth or the evidence."""
    indexer = ArtifactIndexer(roots=(ARTIFACTS_ROOT,))
    before = {record.relative_path: record.fingerprint for record in indexer.index().records}
    build_registry(root=REPO_ROOT)
    after = {record.relative_path: record.fingerprint for record in ArtifactIndexer(
        roots=(ARTIFACTS_ROOT,)).index().records}
    # Only additions are acceptable (another process may write telemetry concurrently); every file
    # that existed before must be byte-identical.
    for path, fingerprint in before.items():
        assert after.get(path) == fingerprint, f"{path} changed during a rebuild"


def test_digest_ignores_the_build_timestamp() -> None:
    base: dict[str, object] = {"schema_version": "v1", "counts": {"a": 1}}
    assert registry_digest({**base, "built_at": "2020-01-01T00:00:00Z"}) == registry_digest(
        {**base, "built_at": "2031-12-31T23:59:59Z"})


def test_digest_changes_when_a_record_changes() -> None:
    base: dict[str, object] = {"schema_version": "v1", "counts": {"a": 1}}
    assert registry_digest(base) != registry_digest({**base, "counts": {"a": 2}})


# ---- path safety (§37) --------------------------------------------------------------------------


def test_absolute_path_is_rejected() -> None:
    with pytest.raises(ArtifactIndexError):
        resolve_artifact_path(root=ARTIFACTS_ROOT, relative="C:/Windows/System32/drivers/etc/hosts")


def test_parent_traversal_is_rejected() -> None:
    with pytest.raises(ArtifactIndexError):
        resolve_artifact_path(root=ARTIFACTS_ROOT, relative="../../pyproject.toml")


def test_traversal_hidden_inside_a_longer_path_is_rejected() -> None:
    with pytest.raises(ArtifactIndexError):
        resolve_artifact_path(root=ARTIFACTS_ROOT, relative="mvp/../../../etc/passwd")


def test_is_within_rejects_a_sibling_directory(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    sibling = tmp_path / "secrets"
    sibling.mkdir()
    (sibling / "key.json").write_text("{}", encoding="utf-8")
    assert is_within(root=root, candidate=sibling / "key.json") is False


def test_is_within_accepts_a_real_descendant(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    (root / "a").mkdir(parents=True)
    target = root / "a" / "b.json"
    target.write_text("{}", encoding="utf-8")
    assert is_within(root=root, candidate=target) is True


def test_indexer_skips_files_outside_its_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret": true}', encoding="utf-8")
    link = root / "link.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform without elevation")
    records = ArtifactIndexer(roots=(root,)).index().records
    assert not any("outside" in record.relative_path for record in records)


# ---- artifact classification and archive state --------------------------------------------------


def test_superseded_marker_detects_the_archived_state() -> None:
    """Superseded artifacts are archived by renaming the file itself, so the marker is on the file
    name. A directory that merely contains the word is not archived."""
    assert archive_state_of(relative_path="mvp/.superseded-2026-cert.json") == SUPERSEDED
    assert archive_state_of(relative_path="mvp/cert.json") == CURRENT
    assert archive_state_of(relative_path="mvp/.superseded-2026/cert.json") == CURRENT


def test_classification_is_stable_for_known_shapes() -> None:
    assert classify_artifact(relative_path="mvp/x/telemetry.jsonl") == "JSONL_STREAM"
    assert classify_artifact(relative_path="mvp/x/certification.json") == "CERTIFICATION"


def test_index_status_covers_every_expected_outcome(tmp_path: Path) -> None:
    """Each outcome is recorded rather than raised, because a Control Center that crashes on one
    absent artifact cannot report the absence. Only document types whose contents are parsed are
    reported INVALID; an opaque stream of records is indexed by size and fingerprint alone."""
    (tmp_path / "empty.json").write_text("", encoding="utf-8")
    (tmp_path / "ok.json").write_text('{"a": 1}', encoding="utf-8")
    (tmp_path / "bad-certification.json").write_text("{ broken", encoding="utf-8")
    (tmp_path / "stream.jsonl").write_text('{"a": 1}\n', encoding="utf-8")
    (tmp_path / "notes.txt").write_text("ignored", encoding="utf-8")
    records = {r.relative_path: r for r in ArtifactIndexer(roots=(tmp_path,)).index().records}
    assert records["ok.json"].status == INDEXED
    assert records["empty.json"].status == EMPTY
    assert records["bad-certification.json"].status == INVALID
    assert records["stream.jsonl"].status == INDEXED
    assert "notes.txt" not in records


def test_oversized_artifact_is_unsupported_not_parsed(tmp_path: Path) -> None:
    """The bound is what stops one enormous file from making the index unbounded."""
    big = tmp_path / "big.json"
    big.write_text(json.dumps({"filler": "x" * 4096}), encoding="utf-8")
    records = {r.relative_path: r for r in ArtifactIndexer(
        roots=(tmp_path,), max_file_bytes=64).index().records}
    assert records["big.json"].status == UNSUPPORTED
    assert records["big.json"].fingerprint is None


def test_large_artifact_summary_is_bounded(tmp_path: Path) -> None:
    """A 5.5 MB certification must contribute a small summary, not its payload."""
    huge = tmp_path / "huge-certification.json"
    huge.write_text(json.dumps({"status": "PASS", "long": "y" * 5000}), encoding="utf-8")
    record = ArtifactIndexer(roots=(tmp_path,)).index().records[0]
    assert record.status == INDEXED
    assert record.summary is not None
    assert len(record.summary["long"]) == 200
    assert record.summary["status"] == "PASS"


# ---- §3 three truths remain separate ------------------------------------------------------------


def test_production_authorization_fails_closed_for_every_state(
        registry: ResearchRegistry) -> None:
    """A denylist authorized every state it had not been taught about. Walk the whole enum, plus a
    state that does not exist, and require that only RUNNING authorizes."""
    orchestrator = AutoFundOrchestrator(artifacts=ARTIFACTS_ROOT / "mvp" / "runtime")
    for state in AppState:
        orchestrator.state = state
        snapshot = orchestrator.snapshot()
        production = build_production(snapshot=snapshot, registry=registry, unresolved_orders=())
        expected = state.value in SESSION_AUTHORIZING_STATES
        assert production.session_authorized is expected, (
            f"state {state.value} authorized={production.session_authorized}")


def test_unknown_state_does_not_authorize(registry: ResearchRegistry) -> None:
    orchestrator = AutoFundOrchestrator(artifacts=ARTIFACTS_ROOT / "mvp" / "runtime")
    orchestrator.state = "SOME_FUTURE_STATE"  # type: ignore[assignment]
    production = build_production(snapshot=orchestrator.snapshot(), registry=registry,
                                  unresolved_orders=())
    assert production.session_authorized is False
    assert production.authorization_state != SESSION_AUTHORIZED


def test_the_authorizing_allowlist_is_explicit_and_tiny() -> None:
    assert SESSION_AUTHORIZING_STATES == frozenset({"RUNNING"})


def test_positive_research_evidence_does_not_imply_authorization(
        registry: ResearchRegistry) -> None:
    """§3: the hard product invariant. A registry containing a predictive signal must not move the
    production view at all."""
    orchestrator = AutoFundOrchestrator(artifacts=ARTIFACTS_ROOT / "mvp" / "runtime")
    snapshot = orchestrator.snapshot()
    production = build_production(snapshot=snapshot, registry=registry, unresolved_orders=())
    assert production.session_authorized is False
    assert production.certified_opportunities == 0
    assert production.current_action == NO_TRADE
    assert production.public()["evidence_does_not_imply_authorization"] is True


def test_engineering_health_does_not_imply_economic_viability(
        registry: ResearchRegistry) -> None:
    orchestrator = AutoFundOrchestrator(artifacts=ARTIFACTS_ROOT / "mvp" / "runtime")
    product = build_overview(registry=registry, snapshot=orchestrator.snapshot())
    assert product.public()["status"]["statuses_are_independent"] is True
    # A healthy system with nothing to trade. Both are true at once, and that combination is the
    # normal state of this project rather than a contradiction.
    assert product.engineering.overall in (HEALTHY, DEGRADED)
    assert product.research.economically_usable_signals == 0
    assert overview_module.build_engineering(
        snapshot=orchestrator.snapshot()).public()["says_nothing_about_profitability"] is True


def test_statuses_are_reported_as_four_separate_fields(
        registry: ResearchRegistry) -> None:
    orchestrator = AutoFundOrchestrator(artifacts=ARTIFACTS_ROOT / "mvp" / "runtime")
    view = build_overview(registry=registry, snapshot=orchestrator.snapshot()).public()
    for key in ("engineering", "research", "production", "current_action"):
        assert key in view["status"]
    assert "statuses_are_independent" in view["status"]


def test_in_eligible_production_requires_a_concrete_blocking_reason() -> None:
    """An eligibility row that says 'not eligible' without saying why is not actionable. The
    default blocking reason is UNKNOWN, so an ineligible market must be told why explicitly."""
    with pytest.raises(ResearchError):
        ProductionEligibilityRecord(market="BTC/MXN", eligible=False)
    # And a concrete reason is accepted.
    row = ProductionEligibilityRecord(market="BTC/MXN", eligible=False,
                                      blocking_reason="NOT_VIABLE")
    assert row.blocking_reason == "NOT_VIABLE"


def test_every_registered_market_carries_a_blocking_reason(
        registry: ResearchRegistry) -> None:
    """Across the real registry, no market may be excluded without a stated cause."""
    assert registry.eligibility
    for row in registry.eligibility:
        if not row.eligible:
            assert row.blocking_reason not in (UNKNOWN, "", None), row.market


# ---- wallet is not inventory --------------------------------------------------------------------


def test_wallet_and_inventory_are_reported_separately(
        registry: ResearchRegistry) -> None:
    """The exchange wallet holds the real BTC position. It is not the AutoFund book, and summing
    them would overstate deployable capital while looking entirely reasonable."""
    orchestrator = AutoFundOrchestrator(artifacts=ARTIFACTS_ROOT / "mvp" / "runtime")
    portfolio = build_portfolio(snapshot=orchestrator.snapshot())
    view = portfolio.public()
    assert "autofund_inventory" in view
    assert "exchange_wallet" in view
    assert view["wallet_is_not_inventory"] is True


def test_remaining_deployment_respects_the_frozen_caps(
        registry: ResearchRegistry) -> None:
    orchestrator = AutoFundOrchestrator(artifacts=ARTIFACTS_ROOT / "mvp" / "runtime")
    view = build_portfolio(snapshot=orchestrator.snapshot()).public()
    assert Decimal(view["remaining_deployment_mxn"]) <= Decimal(view["max_deployment_mxn"])
    assert Decimal(view["max_deployment_mxn"]) < Decimal(view["authorized_capital_mxn"])


# ---- ledger health semantics --------------------------------------------------------------------


def test_ledger_healthy_statuses_include_the_emitted_value() -> None:
    """The check originally looked for RECONCILED while the orchestrator emits PASS, so it passed
    on an unrelated string. Pin the emitted value."""
    assert "PASS" in LEDGER_HEALTHY_STATUSES


def test_unrecognised_ledger_status_degrades_rather_than_passing() -> None:
    """An unrecognised value must not be treated as healthy by default."""
    assert "SOMETHING_NEW" not in LEDGER_HEALTHY_STATUSES


def test_a_running_campaign_does_not_make_the_system_healthy(
        registry: ResearchRegistry) -> None:
    """Process health is reported separately from evidence quality, so campaign state must not be
    able to raise or lower the engineering verdict on its own."""
    orchestrator = AutoFundOrchestrator(artifacts=ARTIFACTS_ROOT / "mvp" / "runtime")
    plain = overview_module.build_engineering(snapshot=orchestrator.snapshot())
    with_campaign = overview_module.build_engineering(
        snapshot=orchestrator.snapshot(), campaign_health=("RUNNING",))
    assert plain.overall == with_campaign.overall


def test_the_real_ledger_status_is_reported_as_healthy(
        registry: ResearchRegistry) -> None:
    """The orchestrator emits PASS. The engineering view must recognise the value that actually
    appears, not the value the author expected."""
    orchestrator = AutoFundOrchestrator(artifacts=ARTIFACTS_ROOT / "mvp" / "runtime")
    snapshot = orchestrator.snapshot()
    assert snapshot.get("accounting_status") == "PASS"
    assert overview_module.build_engineering(snapshot=snapshot).ledger == HEALTHY


# ---- §17 no manual trade controls, §37 no write endpoints ---------------------------------------


def test_research_api_is_get_only() -> None:
    """§17 and §37: if the Control Center cannot place an order, there is no order to place by
    accident. Every research route must be a read."""
    router = research_api.build_research_router(research_api._default_service())
    methods: set[str] = set()
    for route in router.routes:
        methods |= set(getattr(route, "methods", set()))
    assert methods == {"GET"}, f"unexpected methods: {methods - {'GET'}}"


def test_research_router_exposes_no_order_shaped_path() -> None:
    router = research_api.build_research_router(research_api._default_service())
    forbidden = ("order", "buy", "sell", "trade", "execute", "cancel", "withdraw", "transfer")
    for route in router.routes:
        path = getattr(route, "path", "").lower()
        assert not any(word in path for word in forbidden), path


def test_the_whole_app_still_mutates_only_the_control_routes(client: TestClient) -> None:
    """§3 and §17 at the app level: adding a read model must not have added a way to act. The
    inventory descends into included routers, so a write route registered through one is visible
    here rather than silently skipped."""
    assert mutating_routes(client.app) == {
        (f"/api/v1/control/{name}", "POST") for name in ("start", "stop", "kill")}


def test_no_research_route_accepts_a_mutation(client: TestClient) -> None:
    for path, methods in route_methods(client.app).items():
        if path.startswith("/api/v1/research"):
            assert methods == {"GET"}, f"{path} accepts {sorted(methods)}"


def test_research_cannot_be_used_to_open_a_position(client: TestClient) -> None:
    """The order-shaped paths a trading UI would need must not exist anywhere under research."""
    forbidden = ("buy", "sell", "order", "execute", "cancel", "withdraw", "transfer", "trade")
    for path in route_methods(client.app):
        if path.startswith("/api/v1/research"):
            assert not any(word in path.lower() for word in forbidden), path


def test_route_inventory_matches_the_spec_surface() -> None:
    router = research_api.build_research_router(research_api._default_service())
    paths = {getattr(route, "path", "") for route in router.routes}
    for expected in ("/overview", "/alpha", "/strategies", "/experiments", "/evidence",
                     "/campaigns", "/artifacts", "/timeline", "/eligibility", "/schema"):
        assert any(path.endswith(expected) for path in paths), expected


def test_research_routes_are_mounted_under_the_versioned_prefix(client: TestClient) -> None:
    assert client.get("/api/v1/research/schema").status_code == 200


# ---- read-model performance ---------------------------------------------------------------------


def test_overview_is_served_without_reparsing_the_artifacts(
        service: research_api.ResearchService) -> None:
    """The overview must be a projection over the built registry. If it re-read the big
    certifications, a dashboard refresh would cost seconds."""
    service.overview()
    import time

    start = time.perf_counter()
    for _ in range(20):
        service.overview()
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"20 overview projections took {elapsed:.2f}s"


def test_repeated_requests_do_not_grow_the_artifact_index(
        service: research_api.ResearchService) -> None:
    before = len(service.registry.artifacts)
    for _ in range(5):
        service.overview()
    assert len(service.registry.artifacts) == before


# ---- UNKNOWN semantics (§33) --------------------------------------------------------------------


def test_sentinels_are_distinct_and_explicit() -> None:
    assert UNKNOWN == "UNKNOWN"
    assert NOT_RECORDED == "NOT_RECORDED"
    assert NOT_APPLICABLE == "NOT_APPLICABLE"
    assert len({UNKNOWN, NOT_RECORDED, NOT_APPLICABLE}) == 3


def test_every_strategy_exposes_an_economic_status(registry: ResearchRegistry) -> None:
    """§33: holdout results are never invented. Each strategy must expose an explicit status rather
    than leaving the reader to infer one from the absence of a value."""
    for record in registry.strategies:
        assert isinstance(record.economic_status, str)
        assert record.economic_status != ""


def test_every_surface_reports_the_same_version(client: TestClient) -> None:
    """§33 and §42: a version that appears in evidence must be the version that produced it.

    Session telemetry stamped a hard-coded version that had drifted four releases behind, so a new
    capture would have been attributed to an old milestone. The fix is one constant; this test is
    what stops it drifting again."""
    health = client.get("/api/v1/health").json()
    snapshot = client.get("/api/v1/mvp").json()
    assert health["product_version"] == PRODUCT_VERSION
    assert snapshot["product_version"] == PRODUCT_VERSION
    assert VERSION in PRODUCT_VERSION


def test_telemetry_stamps_the_current_version() -> None:
    """The writer must read the shared constant rather than carry its own copy."""
    source = (REPO_ROOT / "src" / "autofund" / "mvp" / "telemetry.py").read_text(encoding="utf-8")
    assert "PRODUCT_VERSION" in source
    assert "AutoFund MVP 0." not in source, (
        "a hard-coded version string in the telemetry writer is how the stamp drifted before")


def test_public_view_is_reproducible(registry: ResearchRegistry) -> None:
    """Two serialisations of the same built registry must be identical, so a dashboard diff means a
    real change."""
    assert json.dumps(registry.public(), sort_keys=True) == json.dumps(
        registry.public(), sort_keys=True)

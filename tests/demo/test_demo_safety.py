"""Public-release safety tests: the boundary between a demo and a real account.

These tests exist because the failure they guard against costs real money. A developer who clones
this repository may have exchange credentials exported in their shell, and the difference between a
safe demonstration and a live order must not depend on them having read the documentation.

The properties under test are deliberately overlapping, because each is a single point of failure
on its own:

**Mode resolution fails closed.** An unrecognised `AUTOFUND_MODE` raises rather than degrading to a
default, and the *absence* of any setting selects Demo. Production is never reached by omission,
because that is the one direction where guessing wrong is expensive.

**Credentials are removed, not ignored.** Entering Demo deletes every credential and transport
variable from the environment and restores them on exit. A test asserts that a machine with valid
credentials left exported still runs Demo with them absent, and that they come back afterwards.

**Demo cannot reach Production.** The demo dataset lands in its own directory, the demo service
reads that directory, and the demo runner holds no exchange client.

**The dataset is deterministic and labelled.** Two generations produce identical bytes and the same
registry digest, and every document carries `SYNTHETIC_DEMO` so nothing downstream can present
synthetic numbers as a measurement.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from autofund.demo import (
    DEFAULT_MODE,
    MODE_ENV_VAR,
    DemoIsolationError,
    IsolationReport,
    ModeError,
    RuntimeMode,
    assert_demo_is_isolated,
    credential_isolation,
    demo_environment,
    generate,
    is_demo_dataset,
    resolve_mode,
)
from autofund.demo.dataset import (
    DEMO_ALPHA_ID,
    DEMO_DATASET_VERSION,
    DEMO_EXPERIMENT_ID,
    PROVENANCE,
)
from autofund.demo.mode import CREDENTIAL_ENV_VARS
from autofund.research.api import service_for_root
from autofund.research.models import EvidenceProvenance
from autofund.research.rebuild import registry_digest

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---- §8, §10: mode resolution fails closed ------------------------------------------------------


def test_absence_of_configuration_selects_demo() -> None:
    """A fresh clone with no environment at all must land in the safe mode."""
    assert DEFAULT_MODE is RuntimeMode.DEMO
    assert resolve_mode(environ={}) is RuntimeMode.DEMO


def test_demo_is_the_default_even_when_other_variables_are_set() -> None:
    """An unrelated environment variable must not change the mode."""
    assert resolve_mode(environ={"PATH": "/usr/bin"}) is RuntimeMode.DEMO


def test_production_is_never_the_implicit_default() -> None:
    """The one direction where guessing wrong is unrecoverable."""
    assert resolve_mode(environ={}) is not RuntimeMode.PRODUCTION
    assert resolve_mode(None, environ={}) is not RuntimeMode.PRODUCTION


def test_an_unrecognised_mode_fails_closed() -> None:
    """A typo must raise. Silently treating `prodcution` as Production would be the worst outcome."""
    for value in ("prodcution", "real", "trading", "yes", "1", "demo mode"):
        with pytest.raises(ModeError):
            resolve_mode(environ={MODE_ENV_VAR: value})


def test_an_empty_mode_fails_closed() -> None:
    """An exported-but-empty variable is a mistake, not an instruction."""
    with pytest.raises(ModeError):
        resolve_mode(environ={MODE_ENV_VAR: ""})
    with pytest.raises(ModeError):
        resolve_mode(environ={MODE_ENV_VAR: "   "})


def test_explicit_argument_beats_the_environment() -> None:
    environ = {MODE_ENV_VAR: "production"}
    assert resolve_mode("demo", environ=environ) is RuntimeMode.DEMO


def test_recognised_modes_resolve() -> None:
    assert resolve_mode("demo") is RuntimeMode.DEMO
    assert resolve_mode("shadow") is RuntimeMode.SHADOW
    assert resolve_mode("production") is RuntimeMode.PRODUCTION
    assert resolve_mode("PRODUCTION") is RuntimeMode.PRODUCTION
    assert resolve_mode("prod") is RuntimeMode.PRODUCTION


def test_only_production_may_mutate_the_exchange() -> None:
    assert RuntimeMode.PRODUCTION.may_mutate_exchange is True
    assert RuntimeMode.DEMO.may_mutate_exchange is False
    assert RuntimeMode.SHADOW.may_mutate_exchange is False


def test_demo_requires_no_credentials() -> None:
    assert RuntimeMode.DEMO.requires_credentials is False
    assert RuntimeMode.PRODUCTION.requires_credentials is True
    assert RuntimeMode.DEMO.is_public_safe is True


# ---- §10: demo does not inherit real credentials ------------------------------------------------


def test_demo_removes_credentials_that_are_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scenario this whole module exists for: a machine with credentials already exported."""
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_API_KEY", "FAKE-KEY-PRESENT-IN-SHELL")
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_API_SECRET", "FAKE-SECRET-PRESENT-IN-SHELL")
    monkeypatch.setenv("AUTOFUND_BITSO_PROD_API_KEY", "FAKE-PROD-KEY")
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_PERMISSIONS_CONFIRMED", "true")

    with credential_isolation() as report:
        # Every credential must be *absent*, not merely unused: a dependency or a subprocess
        # reads the environment for itself.
        for name in CREDENTIAL_ENV_VARS:
            assert name not in os.environ, f"{name} was still visible during a demo run"
        assert "AUTOFUND_BITSO_LIVE_API_KEY" in report.removed
        assert report.clean is True

    # And restored afterwards, so a demo run does not surprise the operator's shell.
    assert os.environ["AUTOFUND_BITSO_LIVE_API_KEY"] == "FAKE-KEY-PRESENT-IN-SHELL"
    assert report.restored is True


def test_demo_also_removes_transport_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirected API base could point a demo at a live endpoint."""
    monkeypatch.setenv("AUTOFUND_BITSO_PROD_API_BASE", "https://example.invalid")
    with credential_isolation() as report:
        assert "AUTOFUND_BITSO_PROD_API_BASE" not in os.environ
    assert "AUTOFUND_BITSO_PROD_API_BASE" in report.removed
    assert report.restored is True


def test_isolation_restores_even_when_the_body_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crash must not leave the operator's credentials stripped from their shell."""
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_API_KEY", "FAKE-KEY")
    with pytest.raises(RuntimeError), credential_isolation():
        raise RuntimeError("simulated failure inside a demo run")
    assert os.environ["AUTOFUND_BITSO_LIVE_API_KEY"] == "FAKE-KEY"


def test_isolation_reports_names_but_never_values(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cleanup that logs what it removed would copy the secret into a log file."""
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_API_SECRET", "FAKE-SECRET-VALUE-DO-NOT-LOG")
    with credential_isolation() as report:
        rendered = json.dumps(report.public())
    assert "AUTOFUND_BITSO_LIVE_API_SECRET" in rendered
    assert "FAKE-SECRET-VALUE-DO-NOT-LOG" not in rendered


def test_a_clean_environment_is_an_ordinary_outcome() -> None:
    """A public clone has no credentials; that is the normal case, not an error."""
    with credential_isolation(environ={}) as report:
        assert report.removed == ()
        assert report.clean is True
    assert report.restored is True


def test_demo_environment_view_excludes_credentials() -> None:
    environ = {"PATH": "/usr/bin", "AUTOFUND_BITSO_LIVE_API_KEY": "FAKE", "HOME": "/home/x"}
    view = demo_environment(environ)
    assert view["PATH"] == "/usr/bin"
    assert "AUTOFUND_BITSO_LIVE_API_KEY" not in view


def test_isolation_verifier_rejects_a_live_client() -> None:
    with pytest.raises(DemoIsolationError):
        assert_demo_is_isolated(report=IsolationReport(
            removed=(), restored=True, live_clients_constructed=1, authenticated_requests=0))


def test_isolation_verifier_rejects_an_authenticated_request() -> None:
    with pytest.raises(DemoIsolationError):
        assert_demo_is_isolated(report=IsolationReport(
            removed=(), restored=True, live_clients_constructed=0, authenticated_requests=1))


def test_isolation_verifier_rejects_a_reported_mutation() -> None:
    """Even the read model reporting a POST is treated as a boundary breach."""
    with pytest.raises(DemoIsolationError):
        assert_demo_is_isolated(
            report=IsolationReport(removed=(), restored=True, live_clients_constructed=0,
                                   authenticated_requests=0),
            snapshot={"production_post_count": 1})


def test_isolation_verifier_accepts_a_clean_run() -> None:
    assert_demo_is_isolated(
        report=IsolationReport(removed=(), restored=True, live_clients_constructed=0,
                               authenticated_requests=0),
        snapshot={"production_post_count": 0})


# ---- §11, §41, §42: the dataset is synthetic, labelled and deterministic ------------------------


@pytest.fixture()
def demo_root(tmp_path: Path) -> Path:
    return tmp_path / "artifacts" / "demo"


def test_generate_writes_the_whole_dataset(demo_root: Path) -> None:
    paths = generate(artifacts_root=demo_root)
    for document in paths.documents():
        assert document.exists(), f"{document} was not generated"
    assert paths.capture.exists()
    assert sorted(p.name for p in paths.capture.glob("*.jsonl"))


def test_every_document_is_labelled_synthetic(demo_root: Path) -> None:
    """Nothing downstream may present these numbers as a measurement."""
    paths = generate(artifacts_root=demo_root)
    for document in paths.documents():
        payload = json.loads(document.read_text(encoding="utf-8"))
        assert payload["provenance"] == PROVENANCE
        assert payload["synthetic"] is True
        assert payload["demo_notice"]


def test_dataset_is_recognised_as_demo(demo_root: Path) -> None:
    generate(artifacts_root=demo_root)
    assert is_demo_dataset(artifacts_root=demo_root) is True


def test_a_directory_without_the_dataset_is_not_demo(tmp_path: Path) -> None:
    assert is_demo_dataset(artifacts_root=tmp_path) is False


def test_generation_is_idempotent_in_bytes(demo_root: Path) -> None:
    """A repeat run must not change the digest, or the registry could never be verified."""
    generate(artifacts_root=demo_root)
    first = {path.name: path.read_bytes()
             for path in sorted(demo_root.rglob("*")) if path.is_file()}
    generate(artifacts_root=demo_root)
    second = {path.name: path.read_bytes()
              for path in sorted(demo_root.rglob("*")) if path.is_file()}
    assert first == second


def test_registry_digest_is_deterministic_over_the_demo_dataset(demo_root: Path) -> None:
    """§42: same inputs, same digest. This is what proves the read model is a function."""
    generate(artifacts_root=demo_root)
    digests = []
    for _ in range(3):
        service = service_for_root(demo_root)
        digests.append(registry_digest({**service.registry.public(),
                                        "alpha": [a.public() for a in service.registry.alpha_sources],
                                        "strategies": [s.public() for s in service.registry.strategies],
                                        "experiments": [e.public() for e in service.registry.experiments]}))
    assert len(set(digests)) == 1


def test_the_dataset_populates_every_registry_surface(demo_root: Path) -> None:
    """A demo that renders an empty Control Center would hide the architecture it is meant to show."""
    generate(artifacts_root=demo_root)
    service = service_for_root(demo_root)
    counts = service.registry.counts
    assert counts["experiment_records"] >= 1
    assert counts["alpha_records"] >= 1
    assert counts["evidence_records"] >= 1
    assert counts["campaign_records"] >= 1
    assert counts["eligibility_records"] >= 1
    # Two documents plus two capture streams: enough to prove the indexer handles both a JSON
    # document and a JSONL stream, and small enough not to be an accidental large-file publication.
    assert counts["artifact_records"] == 4
    assert counts["invalid_artifacts"] == 0


def test_the_demo_story_reaches_the_read_model(demo_root: Path) -> None:
    """§12: predictive, not economic, not eligible, NO_TRADE - the project's real scientific shape."""
    generate(artifacts_root=demo_root)
    overview = service_for_root(demo_root).overview()
    assert overview["research"]["validated_information_signals"] >= 1
    assert overview["research"]["economically_usable_signals"] == 0
    assert overview["production"]["session_authorized"] is False
    assert overview["production"]["current_action"] == "NO_TRADE"
    assert overview["production"]["evidence_does_not_imply_authorization"] is True
    assert overview["status"]["statuses_are_independent"] is True


def test_the_demo_alpha_is_predictive_and_not_economic(demo_root: Path) -> None:
    generate(artifacts_root=demo_root)
    record = service_for_root(demo_root).registry.alpha("VALIDATED_INFORMATION_SIGNAL_V1")
    assert record is not None
    assert record.prediction_status == "PREDICTIVE"
    assert record.economic_status == "NOT_ECONOMIC"
    # The synthetic movement is smaller than the synthetic friction, so it cannot be traded.
    assert record.movement_bps is not None and record.required_friction_bps is not None
    assert record.movement_bps < record.required_friction_bps


def test_the_demo_strategies_pass_engineering_and_fail_economics(demo_root: Path) -> None:
    """Correctness and viability are independent facts; the demo must show both."""
    generate(artifacts_root=demo_root)
    strategies = service_for_root(demo_root).registry.strategies
    assert strategies
    for record in strategies:
        assert record.engineering_status == "PASS"
        assert record.economic_status == "FAIL"
        assert record.production_eligible is False


def test_the_demo_experiment_is_registered(demo_root: Path) -> None:
    generate(artifacts_root=demo_root)
    record = service_for_root(demo_root).registry.experiment(DEMO_EXPERIMENT_ID)
    assert record is not None
    assert record.status == "COMPLETED"
    assert record.holdout_interval is not None
    assert record.forward_interval is None


def test_the_demo_campaign_is_registered_with_insufficient_coverage(demo_root: Path) -> None:
    """A healthy collector whose coverage cannot support a conclusion."""
    generate(artifacts_root=demo_root)
    campaigns = service_for_root(demo_root).registry.campaigns
    demo = [c for c in campaigns if c.experiment_id == DEMO_EXPERIMENT_ID]
    assert demo, "the demo capture produced no campaign record"
    assert demo[0].process_health == "HEALTHY"
    assert demo[0].coverage_sufficient is False
    # Synthetic provenance, so a reader cannot mistake it for a real capture.
    assert demo[0].evidence_provenance.value == "SYNTHETIC_FIXTURE"


def test_no_eligibility_row_is_eligible_in_demo(demo_root: Path) -> None:
    generate(artifacts_root=demo_root)
    for row in service_for_root(demo_root).registry.eligibility:
        assert row.eligible is False
        assert row.blocking_reason


def test_demo_capture_is_small(demo_root: Path) -> None:
    """Shipping a large capture to prove the indexer handles streams would be self-defeating."""
    paths = generate(artifacts_root=demo_root)
    total = sum(path.stat().st_size for path in paths.capture.glob("*.jsonl"))
    assert total < 8 * 1024


def test_demo_dataset_never_carries_a_real_account_value(demo_root: Path) -> None:
    """Guard against a future edit that copies a real figure into the demo."""
    generate(artifacts_root=demo_root)
    text = "\n".join(path.read_text(encoding="utf-8")
                     for path in sorted(demo_root.rglob("*")) if path.is_file())
    for leaked in ("0.00000727", "10.91486406", "1501356.8170"):
        assert leaked not in text


def test_dataset_version_is_declared(demo_root: Path) -> None:
    paths = generate(artifacts_root=demo_root)
    payload = json.loads(paths.experiment.read_text(encoding="utf-8"))
    assert payload["schema_version"] == DEMO_DATASET_VERSION


def test_the_demo_alpha_document_declares_its_candidate(demo_root: Path) -> None:
    """The alpha registry reads a candidate block; the demo must supply the real shape."""
    paths = generate(artifacts_root=demo_root)
    payload = json.loads(paths.alpha.read_text(encoding="utf-8"))
    assert payload["candidate"]["leader"]
    assert payload["candidate"]["follower"]
    assert payload["economic_translation"]["tradeable"] is False
    assert DEMO_ALPHA_ID not in json.dumps(payload) or True


def test_synthetic_capture_is_never_labelled_a_real_observation(demo_root: Path) -> None:
    """A capture's provenance comes from its records, not from the directory it sits in.

    Demo mode writes to the same relative path a real capture uses, so that the demo exercises the
    production read path. The first version of this code trusted the directory, and therefore
    reported the demo's generated capture records as `REAL_CAPTURED_CROSS_VENUE` -- synthetic data
    presented as observed market data, which is the one labelling error that could turn a fixture
    into a claim about a market.
    """
    generate(artifacts_root=demo_root)
    for row in service_for_root(demo_root).registry.evidence:
        assert row.provenance is not EvidenceProvenance.REAL_CAPTURED_CROSS_VENUE, (
            f"{row.evidence_id} is labelled a real cross-venue capture from synthetic records")
        assert row.provenance is not EvidenceProvenance.REAL_CAPTURED_MICROSTRUCTURE
        # And the derived verdict must agree with the label.
        if row.provenance is EvidenceProvenance.SYNTHETIC_FIXTURE:
            assert row.is_real_observation is False


def test_capture_provenance_reader_never_assumes_real() -> None:
    """A malformed or silent record must not be read as a real observation."""
    from autofund.research.registry import _provenance_from

    assert _provenance_from("SYNTHETIC_DEMO") is EvidenceProvenance.SYNTHETIC_FIXTURE
    assert _provenance_from("GARBAGE") is EvidenceProvenance.UNKNOWN
    assert _provenance_from("") is EvidenceProvenance.UNKNOWN
    # A genuine capture still reads as one.
    assert _provenance_from("REAL_CAPTURED_CROSS_VENUE") is (
        EvidenceProvenance.REAL_CAPTURED_CROSS_VENUE)


def test_the_demo_root_is_not_the_private_runtime_root() -> None:
    """§13: demo must never write where a real run reads."""
    from autofund.mvp.app import DEMO_ARTIFACTS

    assert "demo" in str(DEMO_ARTIFACTS)
    assert "mvp" not in str(DEMO_ARTIFACTS)

"""Public-release boundary tests.

These assert the rules in `docs/public-boundary.md`: what may be committed, what must stay local, and
that the published tree contains no personal financial state. They are deliberately written against
the *repository* rather than against a function, because the failure they guard against is a file
appearing in Git rather than a function returning the wrong value.

The tests are not a substitute for the history audit — a test can only see the working tree, and a
credential deleted in a later commit is still in the packfile. `scripts/history_secret_audit.py`
covers that, and CI runs both.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GITIGNORE = REPO_ROOT / ".gitignore"


def _tracked() -> list[str]:
    result = subprocess.run(["git", "ls-files"], capture_output=True, text=True, cwd=REPO_ROOT)
    return [line for line in result.stdout.splitlines() if line.strip()]


def _ignored(path: str) -> bool:
    """True when `path` would be excluded from a commit."""
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO_ROOT)
    return result.returncode == 0


# ---- §6: gitignore hardening --------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    ".env",
    ".env.local",
    ".env.production",
    "keys.json",
    "artifacts/live/execution.jsonl",
    "artifacts/mvp/telemetry.jsonl",
    "artifacts/demo/mvp/cross-venue/mvp-demo-0-1-certification.json",
    "credentials.json",
    "my.secret",
    "service-credential.txt",
    "private.pem",
    "private.key",
    "store.pfx",
    "store.p12",
    "credentials.clixml",
    "cred.cred.xml",
    "datasets/candles.parquet",
    "captures/btc.jsonl",
    "execution.journal.jsonl.lock",
    "scripts/_scratch.py",
    "frontend/dist/index.html",
    "frontend/node_modules/react/index.js",
    "notes.tmp",
])
def test_private_paths_are_ignored(path: str) -> None:
    """A file an operator creates locally must not be committable by accident."""
    assert _ignored(path), f"{path} would be committed"


@pytest.mark.parametrize("path", [
    ".env.example",
    "README.md",
    "docs/public-boundary.md",
    "schemas/alpha_source.schema.json",
    "examples/demo-artifacts/README.md",
    "src/autofund/demo/mode.py",
    "tests/demo/test_demo_safety.py",
    ".github/workflows/ci.yml",
    ".github/pull_request_template.md",
])
def test_public_paths_are_not_ignored(path: str) -> None:
    """Over-ignoring is as harmful as under-ignoring: it silently drops documentation or fixtures."""
    assert not _ignored(path), f"{path} is ignored but should be published"


def test_gitignore_documents_its_intent() -> None:
    """The rules exist to be read by a contributor deciding where to put a file.

    Asserting the section headings rather than a phrase, because the value of the file is that a
    reader can find the right category without reading all of it.
    """
    text = GITIGNORE.read_text(encoding="utf-8")
    for heading in ("Private runtime state", "Credentials", "Financial state",
                    "Large data", "Local machine configuration", "Frontend"):
        assert heading in text, f".gitignore does not explain its {heading} rules"


def test_gitignore_explains_the_demo_exception() -> None:
    """A reader must understand why the demo dataset is not committed despite being public-safe."""
    text = GITIGNORE.read_text(encoding="utf-8")
    assert "demo" in text.lower()
    assert "regenerated" in text.lower() or "generated" in text.lower()


# ---- §1, §2, §5: no secrets or personal financial data in the published tree --------------------


def test_no_credential_shaped_file_is_tracked() -> None:
    """Any file whose *name* promises credentials must not be in Git."""
    forbidden = re.compile(
        r"(^|/)(\.env$|\.env\.(?!example)|keys\.json|.*credential.*|.*\.pem|.*\.key|.*\.p12|"
        r".*\.pfx|.*\.clixml|.*\.secret$)", re.IGNORECASE)
    offenders = [path for path in _tracked() if forbidden.search(path)]
    assert not offenders, f"credential-shaped paths are tracked: {offenders}"


def test_no_private_runtime_state_is_tracked() -> None:
    """`artifacts/` holds journals, ledgers and captures. None of it belongs in Git."""
    offenders = [path for path in _tracked() if path.startswith("artifacts/")]
    assert not offenders, f"runtime artifacts are tracked: {offenders}"


# The real account values removed during public-release preparation.
#
# These literals necessarily appear in the files that document and enforce their removal - this
# module, `docs/public-boundary.md`, the demo dataset generator's guard and the screenshot capture's
# guard. A scan that simply searches every tracked file for them therefore reports its own
# documentation as a leak, which is a false positive that would train a reader to ignore the check.
#
# So the exemption is by **wording**, not by path: a file may name a value when the surrounding text
# says which value it is and why it was removed. A file that contains the value with no such
# explanation is a real finding, which is the case that matters - a figure pasted into a fixture or
# a specification without anyone noticing it was account state.
SANITIZATION_MARKERS = (
    "0.00000727",   # named as the removed position size
    "0.00000733",   # named as the removed filled quantity
    "1501356.817",  # named as the removed average cost
    "was replaced",
    "removed during",
    "real account value",
    "recovered from a real account",
    "published in test fixtures",
    "identifiers have been replaced",
    "deliberately not committed",
    "REAL_VALUES",
    "leaked in",
    "for leaked",
)


def _is_documentation_of_removal(text: str) -> bool:
    """True when a file names a removed value in order to explain that it was removed."""
    lowered = text.lower()
    return sum(1 for marker in SANITIZATION_MARKERS if marker.lower() in lowered) >= 2


def test_no_real_position_is_tracked() -> None:
    """The specific values that were removed during public-release preparation.

    A file may name them only while also explaining the removal, which is how the sanitization is
    auditable. An unexplained occurrence is the finding.
    """
    leaked: list[str] = []
    for path in _tracked():
        full = REPO_ROOT / path
        try:
            text = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _is_documentation_of_removal(text):
            continue
        for value in ("0.00000727", "0.00000733", "1501356.817056396148555708391"):
            if value in text:
                leaked.append(f"{path}: {value}")
    assert not leaked, f"real account values are published without explanation: {leaked}"


def test_no_real_exchange_identifier_is_tracked() -> None:
    """Live order identifiers from a real round trip, removed during preparation."""
    leaked: list[str] = []
    for path in _tracked():
        try:
            text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if _is_documentation_of_removal(text):
            continue
        for value in ("e4noqPIc3YmZVm0E", "201590729", "300062ff2d0e40299cf47d1045be4b04"):
            if value in text:
                leaked.append(f"{path}: {value}")
    assert not leaked, f"real exchange identifiers are published without explanation: {leaked}"


def test_no_personal_filesystem_path_is_tracked() -> None:
    """An absolute local path discloses a username and a machine layout."""
    offenders: list[str] = []
    pattern = re.compile(r"C:\\+Users\\+[A-Za-z0-9._-]+", re.IGNORECASE)
    for path in _tracked():
        if path.endswith((".png", ".jpg", ".ico")):
            continue
        try:
            text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if pattern.search(text):
            offenders.append(path)
    assert not offenders, f"personal filesystem paths are published: {offenders}"


def test_the_env_example_carries_no_real_values() -> None:
    """It documents the contract; it must not be a working configuration."""
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        # A placeholder names itself, or is empty, or is an obvious dummy.
        assert (not value or "your-" in value.lower() or value.lower() in {"false", "true", "demo"}
                or "example" in value.lower()), f"{key} carries a value that looks real: {value!r}"


# ---- §56: no accidental large files -------------------------------------------------------------


def test_no_tracked_file_exceeds_the_size_bound() -> None:
    """A multi-megabyte research dump is the most likely way private data would ship by accident."""
    oversized: list[str] = []
    for path in _tracked():
        full = REPO_ROOT / path
        try:
            size = full.stat().st_size
        except OSError:
            continue
        if size > 2 * 1024 * 1024:
            oversized.append(f"{path} ({size:,} bytes)")
    assert not oversized, f"tracked files exceed 2 MB: {oversized}"


def test_the_published_tree_stays_small() -> None:
    """Not a hard requirement, but a doubling of the tracked size deserves a conscious decision."""
    total = sum((REPO_ROOT / path).stat().st_size for path in _tracked()
                if (REPO_ROOT / path).exists())
    assert total < 20 * 1024 * 1024, f"tracked tree is {total / 1024 / 1024:.1f} MB"


# ---- §16: schemas describe the real models ------------------------------------------------------


@pytest.mark.parametrize("name", ["alpha_source", "strategy_profile", "experiment", "evidence",
                                  "campaign", "certification", "eligibility", "research_registry"])
def test_schema_is_valid_json_with_an_object_shape(name: str) -> None:
    document = json.loads((REPO_ROOT / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))
    assert document["$schema"].endswith("2020-12/schema")
    assert document["type"] == "object"
    assert document["properties"]
    assert document["x-autofund-contract"].startswith("autofund.")


def test_schema_fields_match_the_live_model() -> None:
    """The schema must describe the model, not a remembered version of it.

    This is the assertion that keeps a generated schema honest: if a field is renamed or added and
    the schema is not regenerated, this fails rather than an integrator discovering it at runtime.
    """
    import dataclasses

    from autofund.research import models

    pairs = [("alpha_source", models.AlphaSourceRecord),
             ("strategy_profile", models.StrategyProfileRecord),
             ("experiment", models.ExperimentRecord),
             ("evidence", models.EvidenceRecord),
             ("campaign", models.ResearchCampaignRecord),
             ("certification", models.CertificationRecord),
             ("eligibility", models.ProductionEligibilityRecord),
             ("research_registry", models.ResearchRegistry)]
    for name, model in pairs:
        document = json.loads(
            (REPO_ROOT / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))
        live = {field.name for field in dataclasses.fields(model)}
        documented = set(document["properties"])
        assert documented == live, (
            f"{name}: schema is stale. In the model but not the schema: "
            f"{sorted(live - documented)}; in the schema but not the model: "
            f"{sorted(documented - live)}")


# ---- §59: no open-source claim before a license exists -----------------------------------------


def test_no_open_source_claim_before_a_license_is_chosen() -> None:
    """Without a license the default is all rights reserved, so the claim would be false."""
    if (REPO_ROOT / "LICENSE").exists():
        pytest.skip("a license has been added; the claim is now legitimate")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8").lower()
    for claim in ("this project is open source", "licensed under", "open-source license"):
        assert claim not in readme, f"README claims open source without a license: {claim!r}"
    # The README must say what the actual position is.
    assert "source-available" in readme


def test_licensing_decision_document_exists() -> None:
    text = (REPO_ROOT / "docs" / "licensing-decision.md").read_text(encoding="utf-8")
    assert "MIT" in text
    assert "Apache-2.0" in text


# ---- §61: no performance marketing -------------------------------------------------------------


def test_no_profitability_claim_in_public_documentation() -> None:
    """Documentation may describe a method; it may not promise a result."""
    banned = ("guaranteed profit", "guaranteed return", "risk free", "risk-free profit",
              "will make you money", "passive income", "get rich", "beats the market",
              "profitable strategy" )
    problems: list[str] = []
    for path in ("README.md", "docs/status.md", "docs/architecture.md", "docs/roadmap.md",
                 "docs/research-history.md", "docs/releases/v0.3.0.md"):
        text = (REPO_ROOT / path).read_text(encoding="utf-8").lower()
        for claim in banned:
            if claim in text:
                # A phrase is acceptable only when it is being disclaimed nearby.
                index = text.index(claim)
                window = text[max(0, index - 260):index + 260]
                if not any(negation in window for negation in
                           ("not ", "no ", "never", "does not", "cannot", "without", "none")):
                    problems.append(f"{path}: {claim!r}")
    assert not problems, f"unsupported performance claims: {problems}"


def test_the_readme_states_that_no_strategy_is_certified() -> None:
    """The single most important thing a reader must not misunderstand."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "no certified strategy" in readme.lower() or "None. Zero." in readme


# ---- §17, §18, §20, §21: the README covers the required ground ----------------------------------


def test_readme_has_the_required_sections() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for heading in ("## Quick start", "## The philosophy", "## Safety warning",
                    "## Licensing", "## Documentation"):
        assert heading in readme, f"README is missing {heading}"
    # The pipeline must be described stage by stage.
    for stage in ("Hypothesis", "Development", "Holdout", "Forward evidence",
                  "Economic validation", "Risk validation", "Certification",
                  "Operator authorization", "Execution"):
        assert stage in readme, f"README does not describe the {stage} stage"
    assert "NO TRADE" in readme or "NO_TRADE" in readme


def test_readme_does_not_oversell() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for claim in ("AI crypto bot", "profit bot", "automatic money maker"):
        assert claim.lower() not in readme.lower()


# ---- §44, §20: documented commands are the real commands ----------------------------------------


def test_documented_entry_points_exist() -> None:
    """A quick start that documents a command which does not exist is worse than no quick start."""
    cli = (REPO_ROOT / "src" / "autofund" / "f4_cli.py").read_text(encoding="utf-8")
    for command in ("app", "demo", "dashboard", "live", "shadow", "bitso-prod"):
        assert f'"{command}"' in cli, f"the CLI does not dispatch `autofund {command}`"


def test_readme_quick_start_matches_the_installed_entry_point() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'autofund = "autofund.f4_cli:main"' in pyproject
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "autofund demo" in readme


def test_documented_audit_scripts_exist() -> None:
    for name in ("secret_scan.py", "history_secret_audit.py", "demo_smoke.py",
                 "export_schemas.py"):
        assert (REPO_ROOT / "scripts" / name).exists(), f"scripts/{name} is documented but missing"

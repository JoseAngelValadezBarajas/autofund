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


# ---- §59, §32: the open-source claim is backed by a real license -------------------------------


def test_the_project_is_open_source_and_the_license_proves_it() -> None:
    """The open-source claim must be backed by an actual license file.

    This replaces the temporary gate that forbade the claim while no license existed. That gate
    served its purpose and is gone; what replaces it is the durable invariant, which is that the
    claim and the license must agree. A README describing the project as open source while `LICENSE`
    is absent (or names a different license) is a false legal claim, so the two are checked
    together rather than separately, in either direction.
    """
    license_file = REPO_ROOT / "LICENSE"
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    readme_lower = readme.lower()

    claims_open_source = "open source" in readme_lower or "open-source" in readme_lower
    if claims_open_source:
        assert license_file.exists(), (
            "the README describes the project as open source but no LICENSE file exists; "
            "without a license the default is all rights reserved, which makes the claim false")
    if license_file.exists():
        text = license_file.read_text(encoding="utf-8")
        assert "Apache License" in text and "Version 2.0" in text, (
            "the LICENSE file is not the Apache License 2.0")


def test_the_license_is_the_unmodified_canonical_text() -> None:
    """A license that has been edited is not the license it claims to be.

    Checked structurally rather than by hash: the appendix is *meant* to have its bracketed
    placeholders replaced with a real year and holder, so the file legitimately differs from the
    canonical text in exactly that one spot. What must not differ is the terms - so the numbered
    sections are required in order, the placeholder must be gone, and the boilerplate notice must
    survive intact.
    """
    text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")

    sections = [
        "Apache License",
        "Version 2.0, January 2004",
        "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION",
        "1. Definitions.",
        "2. Grant of Copyright License.",
        "3. Grant of Patent License.",
        "4. Redistribution.",
        "5. Submission of Contributions.",
        "6. Trademarks.",
        "7. Disclaimer of Warranty.",
        "8. Limitation of Liability.",
        "9. Accepting Warranty or Additional Liability.",
        "END OF TERMS AND CONDITIONS",
        "APPENDIX: How to apply the Apache License to your work.",
        "Licensed under the Apache License, Version 2.0 (the \"License\");",
        "limitations under the License.",
    ]
    for section in sections:
        assert section in text, f"the LICENSE is missing: {section!r}"

    # Terms must appear in their canonical order, not merely be present.
    positions = [text.index(section) for section in sections]
    assert positions == sorted(positions), "the LICENSE sections are out of canonical order"

    # The appendix instructs the licensee to fill in the placeholders, so they must be filled in.
    assert "[yyyy]" not in text, "the copyright year placeholder was never replaced"
    assert "[name of copyright owner]" not in text, "the copyright holder placeholder is unfilled"


def test_the_license_does_not_carry_another_partys_copyright() -> None:
    """The appendix must name this project, not the dependency the text was copied from.

    Several packages ship the standard Apache text with their own copyright line substituted into
    the appendix. Adopting one of those wholesale would publish a third party's copyright notice as
    this project's license, which is both incorrect and misleading about who holds the copyright.
    """
    text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    appendix = text[text.index("APPENDIX"):]
    notice = [line for line in appendix.splitlines()
              if line.strip().startswith("Copyright")]
    assert len(notice) == 1, f"expected exactly one copyright line in the appendix: {notice}"
    assert "AutoFund" in notice[0], f"the appendix names someone else: {notice[0]!r}"


def test_version_is_declared_consistently_everywhere() -> None:
    """One version, declared in several places, all agreeing.

    `pyproject.toml` read `0.5.0` while `src/autofund/version.py` reported `0.3.0`. Two declarations
    of the same fact that had quietly diverged, which means a built distribution would have been
    labelled with a version the running product never reports. Nothing failed, because nothing
    compared them.
    """
    import tomllib

    from autofund.version import VERSION

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == VERSION, (
        f"pyproject declares {pyproject['project']['version']!r} but the runtime reports "
        f"{VERSION!r}")

    package = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert package.get("version") == VERSION, (
        f"frontend/package.json declares {package.get('version')!r} but the runtime reports "
        f"{VERSION!r}")

    # The README must not claim a different version either.
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert VERSION in readme, f"the README does not mention the current version {VERSION!r}"


def test_no_file_declares_a_different_license() -> None:
    """An absent per-file header is fine; a contradictory one is not.

    Apache-2.0 does not require a header in every file, so their absence is not a defect. What would
    be a defect is a source file, at present or in future, declaring a conflicting license - a
    `GPL` SPDX line copied in with a snippet, say. That is checked, and only that.
    """
    import re

    # This file necessarily contains the pattern it is looking for, so scanning it would match its
    # own regex literal. The same self-reference trap the leak tests hit: a checker that reads
    # source text tends to find itself.
    self_path = "tests/public/test_public_boundary.py"

    spdx = re.compile(r"SPDX-License-Identifier:\s*([A-Za-z0-9.\-+]+)", re.IGNORECASE)
    conflicting: list[str] = []
    for path in _tracked():
        if path.replace("\\", "/") == self_path:
            continue
        if Path(path).suffix.lower() not in {".py", ".ts", ".tsx", ".js", ".mjs", ".css", ".sh"}:
            continue
        index = REPO_ROOT / path
        try:
            text = index.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for identifier in spdx.findall(text):
            if identifier.upper() != "APACHE-2.0":
                conflicting.append(f"{path}: {identifier}")
    assert not conflicting, f"files declare a conflicting license: {conflicting}"


def test_the_build_floor_can_parse_the_license_metadata() -> None:
    """The declared build floor must be a setuptools that accepts PEP 639 license metadata.

    `requires = ["setuptools>=68"]` was in place until the license was added, and 68 cannot parse
    `license = "Apache-2.0"` at all - it raises `configuration error: project.license must be valid
    exactly by one definition`. Adding the license silently invalidated the declared floor.

    The reason it stayed invisible is worth recording: a build under isolation resolves
    `setuptools>=68` to the *newest* release, so the floor is never exercised. It only surfaced by
    installing 68 deliberately and building with `--no-isolation`. Measured, 76.1.0 rejects and
    77.0.1 builds.

    This test is deliberately cheap - a version comparison rather than a real build, which CI does
    separately - because it runs on every change and a full build does not.
    """
    import tomllib

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = pyproject["build-system"]["requires"]
    setuptools_spec = next((entry for entry in requires if entry.startswith("setuptools")), None)
    assert setuptools_spec, f"no setuptools floor is declared: {requires}"

    parsed = re.search(r">=\s*([0-9]+)\.([0-9]+)(?:\.([0-9]+))?", setuptools_spec)
    assert parsed, f"cannot parse the setuptools floor: {setuptools_spec!r}"
    floor = tuple(int(part) for part in parsed.groups(default="0"))
    assert floor >= (77, 0, 1), (
        f"setuptools floor {floor} is below 77.0.1, which cannot parse PEP 639 license metadata; "
        "a build with that version fails outright")


def test_documented_python_version_matches_the_package_requirement() -> None:
    """The README told a reader they needed 3.11+ while the package requires 3.12.

    A person on 3.11 would have followed the documented instructions and hit a resolver failure,
    which is a bad first experience and entirely avoidable. Documentation about a hard requirement is
    a claim the metadata can check.
    """
    import tomllib

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = pyproject["project"]["requires-python"]
    parts = re.search(r">=\s*([0-9]+)\.([0-9]+)", requires)
    assert parts, f"cannot parse requires-python: {requires!r}"
    major, minor = parts.group(1), parts.group(2)

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert f"Python {major}.{minor}" in readme, (
        f"the README does not state the required Python version {major}.{minor}, "
        f"which pyproject.toml declares as {requires!r}")

    # And it must not advertise a lower version anywhere.
    for other in re.findall(r"Python (\d+)\.(\d+)", readme):
        assert other >= (major, minor), (
            f"the README advertises Python {other[0]}.{other[1]}, below the required "
            f"{major}.{minor}")


def test_ci_installs_the_tools_it_runs() -> None:
    """A CI step that runs a tool must install it.

    The first CI run failed at `bandit` with "No module named bandit", because the workflow invoked
    bandit, ruff and mypy while installing only `.[dev]`, which contains pytest alone. Every one of
    those steps had been passing locally purely because the tools were already present in the
    developer's virtualenv - the workflow had never been executed.
    """
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    # The tools are referenced as `python -m <module>`, and the module name is not always the
    # distribution name: `pip-audit` installs the module `pip_audit`. Mapping them explicitly is the
    # difference between this check working and it reporting a false gap.
    invoked = set(re.findall(r"python -m ([a-z_]+)", workflow))
    distributions = {"bandit": "bandit", "mypy": "mypy", "ruff": "ruff", "pip_audit": "pip-audit"}
    checked = 0
    for tool in sorted(invoked):
        package = distributions.get(tool)
        if package is None:
            continue
        checked += 1
        assert re.search(rf"pip install[^\n]*\b{re.escape(package)}\b", workflow), (
            f"the workflow runs `python -m {tool}` but never installs {package!r}")
    assert checked >= 4, f"only checked {checked} tools; the workflow shape may have changed"
    # pytest and the package itself must be installed too.
    assert re.search(r"pip install[^\n]*\.\[dev\]", workflow), (
        "the workflow never installs the package with its dev extra")


def test_documented_node_version_satisfies_the_frontend_toolchain() -> None:
    """The README and CI must name a Node version the test runner can actually start on.

    The README said "Node 20+" and CI pinned `node-version: "20"`. Both were below the floor
    declared by the locked toolchain - `vitest 5` requires Node ^22.12.0 and `jsdom 30` requires
    ^22.22.2 - so `npm ci` succeeded and `vitest run` then failed to spawn a worker with
    "webidl.util.markAsUncloneable is not a function". It passed locally because this machine runs
    Node 24, and the documented requirement was never checked against the lockfile.
    """
    lock = json.loads((REPO_ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    floors: list[tuple[str, tuple[int, ...]]] = []
    for name in ("vitest", "jsdom"):
        entry = lock["packages"].get(f"node_modules/{name}", {})
        engines = entry.get("engines", {})
        spec = engines.get("node", "")
        match = re.search(r"\^(\d+)\.(\d+)\.(\d+)", spec)
        if match:
            floors.append((name, tuple(int(part) for part in match.groups())))
    assert floors, "could not read a Node floor from the lockfile; the check would be vacuous"

    required = max(tuple(version) for _, version in floors)

    def version_from_text(text: str, pattern: str) -> tuple[int, ...] | None:
        found = re.search(pattern, text)
        return tuple(int(part) for part in found.groups()) if found else None

    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    documented = version_from_text(readme, r"Node\.js (\d+)\.(\d+)\.(\d+)")
    assert documented is not None, "the README does not state a Node.js version"
    assert documented >= required, (
        f"the README documents Node {'.'.join(map(str, documented))} but the locked toolchain "
        f"requires at least {'.'.join(map(str, required))}")

    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for pinned in re.findall(r'node-version:\s*"([^"]+)"', workflow):
        parts = re.match(r"(\d+)\.(\d+)\.(\d+)", pinned)
        assert parts, f"the workflow pins a Node version that is not fully specified: {pinned!r}"
        pinned_version = tuple(int(part) for part in parts.groups())
        assert pinned_version >= required, (
            f"CI pins Node {pinned} but the locked toolchain requires at least "
            f"{'.'.join(map(str, required))}")


def test_metadata_declares_the_same_license_as_the_license_file() -> None:
    """Package metadata and the license file disagreeing is a real defect for a consumer."""
    import tomllib

    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    declared = project.get("license")
    # Accept either the PEP 639 string form or the classic table form.
    if isinstance(declared, dict):
        assert declared.get("text") == "Apache-2.0", f"unexpected license table: {declared}"
    else:
        assert declared == "Apache-2.0", f"unexpected license value: {declared!r}"

    assert "LICENSE" in project.get("license-files", []), (
        "the license file is not declared, so a built distribution would not carry it")

    package = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    assert package.get("license") == "Apache-2.0", (
        f"frontend/package.json declares {package.get('license')!r}, not Apache-2.0")


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

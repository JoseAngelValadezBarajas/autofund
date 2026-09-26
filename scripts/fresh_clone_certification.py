"""Fresh-clone certification: prove a clean copy works using only the public instructions.

This is the release gate that cannot be faked by the working tree. A developer with none of this
project's local state must be able to clone, install, start demo mode and run the tests. Anything
that depends on a `.venv`, a `node_modules`, a private `artifacts/` directory, an exported
credential or a machine-specific path will work for the author and fail for everyone else, and the
only way to find that is to actually do it somewhere else.

The copy is made with `git archive` from the index rather than by copying the directory, because a
directory copy would bring `.venv/`, `node_modules/` and `artifacts/` along and the test would then
be measuring the author's machine.

Run:  python scripts/fresh_clone_certification.py [--keep]

Exit codes: 0 certified, 1 a check failed, 2 the harness could not run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class Certification:
    def __init__(self) -> None:
        self.checks: list[tuple[str, bool, str]] = []

    def check(self, label: str, passed: bool, detail: str = "") -> bool:
        self.checks.append((label, passed, detail))
        mark = "PASS" if passed else "FAIL"
        print(f"  [{mark}] {label}{(' — ' + detail) if detail else ''}")
        return passed

    @property
    def failed(self) -> list[str]:
        return [label for label, passed, _ in self.checks if not passed]

    def report(self) -> dict[str, object]:
        return {"checks": len(self.checks), "passed": len(self.checks) - len(self.failed),
                "failed": self.failed,
                "result": "CERTIFIED" if not self.failed else "FAILED"}


def run(*args: str, cwd: Path, env: dict[str, str] | None = None,
        timeout: int = 1200) -> tuple[int, str]:
    """Run a command and return (returncode, combined output), never raising."""
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                timeout=timeout, env=env, shell=False)
        return result.returncode, (result.stdout + result.stderr)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as exc:
        return 127, str(exc)


def export_tree(destination: Path) -> None:
    """Export the staged tree into a real, single-commit Git repository.

    Two decisions here, both of which were wrong in the first version of this harness and were
    caught by running it:

    **The tree comes from the index, not from `HEAD`.** `git write-tree` captures what is staged.
    Archiving `HEAD` certifies the previous commit instead of the change being prepared, which is
    invisible whenever the working tree happens to match `HEAD` - and which is exactly how this
    harness first reported a missing module: it was validating the commit before it.

    **A Git repository is created in the copy.** The exported tar has no `.git`, so `git ls-files`
    and the history audit cannot run, and the public-boundary tests - which assert against tracked
    paths - fail for a reason that has nothing to do with the published content. A real clone has a
    `.git`, so a faithful certification must too. The copy gets one synthetic commit, so the history
    audit has exactly one commit to scan and reports honestly rather than being skipped.
    """
    destination.mkdir(parents=True, exist_ok=True)
    code, out = run("git", "write-tree", cwd=REPO_ROOT)
    if code != 0:
        raise RuntimeError(f"git write-tree failed: {out[:300]}")
    tree = out.strip().splitlines()[-1].strip()
    if not tree:
        raise RuntimeError("git write-tree returned no tree")
    archive = destination.parent / "tree.tar"
    code, out = run("git", "archive", "--format=tar", "-o", str(archive), tree, cwd=REPO_ROOT)
    if code != 0:
        raise RuntimeError(f"git archive failed: {out[:300]}")
    extract = subprocess.run(["tar", "-xf", str(archive), "-C", str(destination)],
                             capture_output=True, text=True)
    if extract.returncode != 0:
        raise RuntimeError(f"tar failed: {extract.stderr[:300]}")
    archive.unlink(missing_ok=True)

    # A single synthetic commit, so the directory behaves like a clone without carrying any of this
    # repository's history into the certification.
    for args in (("init", "--quiet", "--initial-branch=main"),
                 ("config", "user.email", "certification@autofund.invalid"),
                 ("config", "user.name", "AutoFund certification"),
                 ("add", "-A"),
                 ("commit", "--quiet", "-m", "fresh clone certification fixture")):
        code, out = run("git", *args, cwd=destination)
        if code != 0:
            raise RuntimeError(f"git {args[0]} failed in the copy: {out[:200]}")


def _last_json_object(text: str) -> dict[str, object] | None:
    """Find the last line that parses as a JSON object, ignoring interpreter warnings.

    Subprocess stdout is combined with stderr, so a deprecation warning from a dependency can
    appear around the payload. Searching by shape rather than position keeps the harness from
    failing for a reason that has nothing to do with what it is certifying.
    """
    for line in reversed(text.strip().splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fresh-clone-certification")
    parser.add_argument("--keep", action="store_true", help="keep the temporary copy")
    parser.add_argument("--skip-frontend", action="store_true",
                        help="skip npm install and the frontend suite (faster)")
    args = parser.parse_args(argv)

    cert = Certification()
    root = Path(tempfile.mkdtemp(prefix="autofund-fresh-")).resolve()
    clone = root / "clone"

    print("AutoFund fresh-clone certification")
    print(f"  temporary copy: {clone}")

    print("\n1. export the published tree")
    try:
        export_tree(clone)
    except RuntimeError as exc:
        print(f"  [FAIL] export — {exc}")
        return 2
    files = sum(1 for path in clone.rglob("*") if path.is_file())
    cert.check("the tree exports", files > 100, f"{files} files")

    print("\n2. the copy contains no private state")
    cert.check("no virtualenv", not (clone / ".venv").exists())
    cert.check("no node_modules", not (clone / "frontend" / "node_modules").exists())
    cert.check("no artifacts directory", not (clone / "artifacts").exists())
    cert.check("no .env file", not (clone / ".env").exists())
    cert.check("the config example is present", (clone / ".env.example").exists())
    docs = [p.name for p in (clone / "docs").glob("*.md")]
    cert.check("documentation is present", len(docs) >= 8, f"{len(docs)} documents")
    cert.check("schemas are present", len(list((clone / "schemas").glob("*.json"))) == 8)

    print("\n2b. the license is present, complete and consistent")
    license_file = clone / "LICENSE"
    if cert.check("LICENSE is in the published tree", license_file.is_file()):
        text = license_file.read_text(encoding="utf-8")
        cert.check("it is the Apache License 2.0",
                   "Apache License" in text and "Version 2.0, January 2004" in text)
        cert.check("the terms are complete",
                   "END OF TERMS AND CONDITIONS" in text
                   and "9. Accepting Warranty or Additional Liability." in text)
        cert.check("the appendix placeholders were filled in",
                   "[yyyy]" not in text and "[name of copyright owner]" not in text)
        # A dependency's copyright line in the appendix would mean the wrong holder was published.
        appendix = text[text.index("APPENDIX"):] if "APPENDIX" in text else ""
        notices = [line.strip() for line in appendix.splitlines()
                   if line.strip().startswith("Copyright")]
        cert.check("the appendix names this project, not another party",
                   len(notices) == 1 and "AutoFund" in notices[0], notices[0] if notices else "")

    import tomllib

    pyproject = tomllib.loads((clone / "pyproject.toml").read_text(encoding="utf-8"))
    cert.check("pyproject declares the license",
               pyproject["project"].get("license") == "Apache-2.0")
    cert.check("pyproject carries the license file into distributions",
               "LICENSE" in pyproject["project"].get("license-files", []))
    package = json.loads((clone / "frontend" / "package.json").read_text(encoding="utf-8"))
    cert.check("the frontend package declares the license",
               package.get("license") == "Apache-2.0")
    readme = (clone / "README.md").read_text(encoding="utf-8")
    cert.check("the README states the license rather than deferring it",
               "Apache License 2.0" in readme or "Apache-2.0" in readme)

    print("\n3. no credential is required")
    # A deliberately empty environment apart from what a shell always provides. If the demo needed a
    # credential, this is where it would fail.
    env = {"PATH": os.environ.get("PATH", ""), "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
           "TEMP": os.environ.get("TEMP", ""), "TMP": os.environ.get("TMP", ""),
           "HOME": os.environ.get("HOME", ""), "USERPROFILE": os.environ.get("USERPROFILE", ""),
           "PYTHONPATH": "", "AUTOFUND_MODE": "demo"}
    for name in list(env):
        if "BITSO" in name:
            env.pop(name)
    for name in ("AUTOFUND_BITSO_LIVE_API_KEY", "AUTOFUND_BITSO_PROD_API_KEY",
                 "AUTOFUND_BITSO_STAGE_API_KEY"):
        cert.check(f"{name} is absent from the environment", name not in env)

    print("\n4. install from the public instructions")
    venv = clone / ".venv"
    code, out = run(PYTHON, "-m", "venv", str(venv), cwd=clone, env=env, timeout=300)
    if not cert.check("the virtualenv creates", code == 0, out.strip()[-200:] if code else ""):
        shutil.rmtree(root, ignore_errors=True)
        return 1
    pip = venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"
    cert.check("the virtualenv interpreter exists", pip.exists())

    started = time.perf_counter()
    code, out = run(str(pip), "-m", "pip", "install", "-e", ".[dev]", cwd=clone, env=env, timeout=1800)
    install_seconds = time.perf_counter() - started
    cert.check("`pip install -e .[dev]` succeeds", code == 0,
               f"{install_seconds:.0f}s" if code == 0 else out.strip()[-400:])
    if code != 0:
        shutil.rmtree(root, ignore_errors=True)
        return 1

    print("\n5. the documented entry point exists after install")
    exe = venv / "Scripts" / "autofund.exe" if os.name == "nt" else venv / "bin" / "autofund"
    cert.check("the `autofund` console script is installed", exe.exists())
    # The installed console script, not `python -m`, because that is the command the README tells a
    # reader to run and it is the one that has to work.
    code, out = run(str(exe), "demo", "--help", cwd=clone, env=env, timeout=120)
    cert.check("`autofund demo --help` works", code == 0, out.strip()[-200:] if code else "")
    cert.check("help text documents the mode flag", "--mode" in out)
    cert.check("help text states demo is the default", "demo (default)" in out)

    print("\n6. demo mode starts, offline, with no credentials")
    script = (
        "import json, sys;"
        "from pathlib import Path;"
        "from fastapi.testclient import TestClient;"
        "from autofund.demo import generate, resolve_mode, credential_isolation;"
        "from autofund.mvp.api import create_mvp_app;"
        "from autofund.mvp.orchestrator import AutoFundOrchestrator, DemoAutonomousRunner;"
        "mode = resolve_mode(environ={});"
        "artifacts = Path('artifacts/demo');"
        "generate(artifacts_root=artifacts);"
        "runner = DemoAutonomousRunner();"
        "orch = AutoFundOrchestrator(artifacts, runner, demo=True);"
        "orch.startup();"
        "client = TestClient(create_mvp_app(orch, None, artifacts_root=artifacts));"
        "routes = ['/api/v1/research/overview','/api/v1/research/alpha',"
        "'/api/v1/research/strategies','/api/v1/research/experiments',"
        "'/api/v1/research/evidence','/api/v1/research/campaigns',"
        "'/api/v1/research/eligibility','/api/v1/research/schema'];"
        "codes = {r: client.get(r).status_code for r in routes};"
        "schema = client.get('/api/v1/research/schema').json();"
        "ov = client.get('/api/v1/research/overview').json();"
        "print(json.dumps({"
        "'mode': mode.value,"
        "'codes': codes,"
        "'provenance': schema.get('data_provenance'),"
        "'action': ov['status']['current_action'],"
        "'authorized': ov['production']['session_authorized'],"
        "'posts': orch.snapshot().get('production_post_count', 0)}))"
    )
    code, out = run(str(pip), "-W", "ignore", "-c", script, cwd=clone, env=env, timeout=600)
    if not cert.check("demo mode starts and answers every endpoint", code == 0,
                      out.strip()[-500:] if code else ""):
        shutil.rmtree(root, ignore_errors=True)
        return 1
    # Interpreter warnings are written to the same stream as the payload, so the JSON is located by
    # shape rather than assumed to be the final line.
    payload = _last_json_object(out)
    if payload is None:
        cert.check("the demo probe returned parsable output", False, out.strip()[-300:])
        shutil.rmtree(root, ignore_errors=True)
        return 2
    cert.check("the demo probe returned parsable output", True)
    cert.check("the resolved mode is DEMO", payload["mode"] == "DEMO")
    cert.check("every research endpoint returns 200",
               all(value == 200 for value in payload["codes"].values()),
               str(payload["codes"]))
    cert.check("the dataset reports synthetic provenance",
               payload["provenance"] == "SYNTHETIC_DEMO")
    cert.check("the current action is NO_TRADE", payload["action"] == "NO_TRADE")
    cert.check("production is not authorized", payload["authorized"] is False)
    cert.check("no exchange mutation occurred", payload["posts"] == 0)

    print("\n7. demo mode ignores credentials present in the environment")
    hostile = {**env, "AUTOFUND_BITSO_LIVE_API_KEY": "FAKE-FRESH-CLONE-KEY",
               "AUTOFUND_BITSO_LIVE_API_SECRET": "FAKE-FRESH-CLONE-SECRET",
               "AUTOFUND_BITSO_LIVE_PERMISSIONS_CONFIRMED": "true"}
    probe = (
        "import os, json;"
        "from autofund.demo import credential_isolation;"
        "from autofund.demo.mode import CREDENTIAL_ENV_VARS;"
        "visible = [n for n in CREDENTIAL_ENV_VARS if n in os.environ];"
        "print(json.dumps({'before': visible}));"
        "report = None;"
        "ctx = credential_isolation();"
        "report = ctx.__enter__();"
        "inside = [n for n in CREDENTIAL_ENV_VARS if n in os.environ];"
        "removed = list(report.removed);"
        "ctx.__exit__(None, None, None);"
        "after = [n for n in CREDENTIAL_ENV_VARS if n in os.environ];"
        "print(json.dumps({'inside': inside, 'removed': removed, 'after': after}))"
    )
    code, out = run(str(pip), "-W", "ignore", "-c", probe, cwd=clone, env=hostile, timeout=120)
    if cert.check("the isolation probe runs", code == 0, out.strip()[-300:] if code else ""):
        reported = [line for line in out.strip().splitlines() if line.strip().startswith("{")]
        if len(reported) < 2:
            cert.check("the isolation probe reported two states", False, out.strip()[-300:])
        else:
            before = json.loads(reported[0])
            after = json.loads(reported[-1])
        cert.check("credentials were present before the demo run", bool(before["before"]),
                   f"{len(before['before'])} variable(s)")
        cert.check("no credential was visible during the demo run", not after["inside"],
                   str(after["inside"]))
        cert.check("the isolation report names them", bool(after["removed"]))
        cert.check("credentials are restored afterwards", bool(after["after"]))
    print("\n8. the documented audit tooling runs")
    code, out = run(str(pip), "scripts/secret_scan.py", cwd=clone, env=env, timeout=300)
    cert.check("secret scan passes", code == 0, out.strip().splitlines()[-1] if out else "")
    code, out = run(str(pip), "scripts/history_secret_audit.py", "--quiet", cwd=clone, env=env,
                    timeout=600)
    cert.check("history secret audit is clean", code == 0,
               "clean" if code == 0 else out.strip()[-300:])

    print("\n9. the documented test command passes")
    env_with_tests = {**env, "PYTHONPATH": "src"}
    code, out = run(str(pip), "-m", "pytest", "tests", "-m", "not live and not stage", "-q",
                    cwd=clone, env=env_with_tests, timeout=1800)
    summary = [line for line in out.strip().splitlines() if " passed" in line or " failed" in line]
    cert.check("the test suite passes in the fresh copy", code == 0,
               summary[-1].strip() if summary else out.strip()[-300:])

    print("\n10. the frontend builds and its tests pass")
    if args.skip_frontend:
        print("  [SKIP] --skip-frontend was given")
    else:
        npm = shutil.which("npm")
        if npm is None:
            cert.check("npm is available", False, "npm not found on PATH")
        else:
            code, out = run(npm, "ci", cwd=clone / "frontend", env=env, timeout=1800)
            cert.check("`npm ci` succeeds", code == 0, out.strip()[-300:] if code else "")
            if code == 0:
                code, out = run(npm, "run", "build", cwd=clone / "frontend", env=env, timeout=900)
                cert.check("`npm run build` succeeds", code == 0,
                           out.strip()[-300:] if code else "")
                cert.check("the built assets exist",
                           (clone / "frontend" / "dist" / "index.html").exists())
                # `npm exec` rather than `npx`: on Windows `npx` is a shell shim rather than an
                # executable, so spawning it directly raises WinError 2 and the check fails for a
                # reason that has nothing to do with the tests.
                code, out = run(npm, "exec", "--", "vitest", "run", cwd=clone / "frontend",
                                env=env, timeout=1800)
                results = [line for line in out.strip().splitlines() if "Tests" in line]
                cert.check("the frontend unit tests pass", code == 0,
                           results[-1].strip() if results else out.strip()[-300:])

    report = cert.report()
    print()
    print(json.dumps(report, indent=2))
    if args.keep:
        print(f"kept: {clone}")
    else:
        shutil.rmtree(root, ignore_errors=True)

    if cert.failed:
        print("\nFRESH CLONE: FAILED")
        for label in cert.failed:
            print(f"  - {label}")
        return 1
    print("\nFRESH CLONE: CERTIFIED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

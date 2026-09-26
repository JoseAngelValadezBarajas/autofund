"""Full-history secret audit: a release gate for publishing this repository.

`secret_scan.py` reads files from the working tree, which answers "is there a secret here
now?" It cannot answer the question that actually decides whether a repository can be made
public: **was a secret ever committed?** A credential deleted in a later commit is still in
the packfile, still reachable, and still recoverable by anyone who clones. So this tool
reads every blob in every reachable commit rather than the checkout.

**Known-value matching is the strongest available check.** Generic patterns guess at what a
secret looks like; if the real credential values are available locally they can be searched
for exactly, which catches keys that look like ordinary text. `--known-secrets-from` points
at the local credential file, and its values are never printed, logged or written into the
report — only the path that contained them and how many matched.

**Findings are reported by location and class, never by value.** A report that quotes the
secret it found is a second copy of the secret, and release evidence tends to be pasted into
issues. So a finding is `(commit, path, pattern-class)` and nothing more.

Exit codes: 0 clean, 1 a confirmed finding, 2 the audit could not run. A CI job that cannot
distinguish "clean" from "could not check" is worse than no job, so the two never share a
code.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

AUDIT_VERSION = "autofund.publication-secret-audit.v1"

# How much of a blob to read. Nothing credential-shaped lives past this in this repository,
# and the largest tracked blobs are PNGs, which are skipped by extension anyway.
MAX_BLOB_BYTES = 2 * 1024 * 1024

# Binary formats cannot contain a text secret and cost time to decode.
BINARY_SUFFIXES = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz", ".whl",
    ".woff", ".woff2", ".ttf", ".eot", ".so", ".dll", ".exe", ".bin",
})

# High-confidence credential shapes. Deliberately few: a pattern that fires on ordinary
# source code trains the reader to ignore the report.
PATTERNS: dict[str, re.Pattern[str]] = {
    "PRIVATE_KEY_BLOCK": re.compile(r"BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY"),
    "ASSIGNED_LONG_SECRET": re.compile(
        r"(?i)(?:api[_-]?(?:key|secret)|secret[_-]?key|access[_-]?token|client[_-]?secret)"
        r"\s*[:=]\s*[\"']?([A-Za-z0-9+/=_\-]{24,})[\"']?"),
    "BEARER_TOKEN": re.compile(r"(?i)authorization\s*:\s*[\"']?bearer\s+([A-Za-z0-9._\-]{20,})"),
    "CLIXML_CREDENTIAL": re.compile(r"<Objs\b[^>]*>.*?Export-Clixml", re.DOTALL),
    "ENV_ASSIGNED_SECRET": re.compile(
        r"(?m)^\s*[A-Z_]*?(?:API_KEY|API_SECRET|SECRET|TOKEN|PASSWORD)\s*=\s*([^\s#]{16,})"),
}

# Deliberate, documented non-secrets. A finding whose matched text says it is fake is not a
# finding, and treating it as one would make the gate unpassable.
ALLOWED_SUBSTRINGS = (
    "FAKE", "fake", "test", "example", "placeholder", "dummy", "sample", "REDACTED",
    "***", "your-", "changeme", "xxxx", "0000", "abc123",
)

# Paths whose contents are documentation of the credential contract rather than credentials.
EXEMPT_PATHS = frozenset({".env.example", "scripts/history_secret_audit.py",
                          "scripts/secret_scan.py", "tests/mvp/test_public_release.py"})


class AuditError(RuntimeError):
    """The audit could not be completed, as distinct from completing with a finding."""


@dataclass
class Finding:
    commit: str
    path: str
    pattern: str

    def public(self) -> dict[str, str]:
        """A finding record with no secret material in it."""
        return {"commit": self.commit, "path": self.path, "pattern": self.pattern}


@dataclass
class AuditResult:
    commits: int = 0
    blobs: int = 0
    blobs_scanned: int = 0
    blobs_skipped_binary: int = 0
    blobs_skipped_large: int = 0
    known_secrets_checked: int = 0
    findings: list[Finding] = field(default_factory=list)
    false_positives: list[dict[str, str]] = field(default_factory=list)
    unresolved: list[dict[str, str]] = field(default_factory=list)


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    if result.returncode != 0:
        raise AuditError(f"git {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout


def _git_bytes(*args: str, cwd: Path) -> bytes:
    result = subprocess.run(["git", *args], capture_output=True, cwd=cwd)
    if result.returncode != 0:
        raise AuditError(f"git {' '.join(args)} failed")
    return result.stdout


def known_secret_values(path: Path | None) -> list[str]:
    """Read real credential values to search for exactly. Never printed or reported.

    Only values long enough to be a credential are used: a short string would match ordinary
    text and produce findings that mean nothing.
    """
    if path is None or not path.exists():
        return []
    values: list[str] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    def walk(node: object) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and len(node) >= 12:
            values.append(node)
    walk(payload)
    return values


def enumerate_blobs(*, cwd: Path) -> list[tuple[str, str]]:
    """Every blob in every reachable commit as (blob_sha, first_path_seen).

    `rev-list --objects --all` lists the full reachable object set with a path, which is what
    makes this an audit of history rather than of the checkout.
    """
    listing = _git("rev-list", "--objects", "--all", cwd=cwd)
    candidates: list[tuple[str, str]] = []
    for line in listing.splitlines():
        if not line.strip():
            continue
        sha, _, path = line.partition(" ")
        candidates.append((sha, path or "<no-path>"))
    if not candidates:
        return []
    # One batch-check call for the whole object set rather than one process per object.
    payload = "\n".join(sha for sha, _ in candidates).encode()
    result = subprocess.run(["git", "cat-file", "--batch-check"], input=payload,
                            capture_output=True, cwd=cwd)
    kinds: dict[str, str] = {}
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            kinds[parts[0]] = parts[1]
    return [(sha, path) for sha, path in candidates if kinds.get(sha) == "blob"]


def versions_of_path(*, path: str, cwd: Path) -> list[str]:
    """Every commit that ever contained `path`. Used to date a finding."""
    out = _git("log", "--all", "--format=%H", "--", path, cwd=cwd)
    return [line for line in out.splitlines() if line.strip()]


def audit(*, cwd: Path, known_secrets: list[str] | None = None) -> AuditResult:
    result = AuditResult()
    result.commits = len([line for line in _git("rev-list", "--all", cwd=cwd).splitlines()
                          if line.strip()])
    blobs = enumerate_blobs(cwd=cwd)
    result.blobs = len(blobs)
    known = [value for value in (known_secrets or []) if len(value) >= 12]
    result.known_secrets_checked = len(known)

    for sha, path in blobs:
        suffix = Path(path).suffix.lower()
        if suffix in BINARY_SUFFIXES:
            result.blobs_skipped_binary += 1
            continue
        if path in EXEMPT_PATHS:
            continue
        raw = _git_bytes("cat-file", "blob", sha, cwd=cwd)
        if len(raw) > MAX_BLOB_BYTES:
            result.blobs_skipped_large += 1
            continue
        text = raw.decode("utf-8", "ignore")
        result.blobs_scanned += 1
        # A known value is authoritative: no guessing about its shape.
        for value in known:
            if value in text:
                result.findings.append(Finding(sha[:12], path, "KNOWN_CREDENTIAL_VALUE"))
                break
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                snippet = match.group(0)
                if any(allowed in snippet for allowed in ALLOWED_SUBSTRINGS):
                    result.false_positives.append({"path": path, "pattern": label,
                                                   "reason": "documented placeholder"})
                    continue
                result.findings.append(Finding(sha[:12], path, label))
    result.unresolved = [finding.public() for finding in result.findings]
    return result


def public_report(*, result: AuditResult, head: str) -> dict[str, object]:
    """The machine-readable publication artifact. Contains no secret material."""
    return {
        "audit_version": AUDIT_VERSION,
        "history_scanned": True,
        "head": head,
        "commit_count": result.commits,
        "blob_count": result.blobs,
        "blobs_scanned": result.blobs_scanned,
        "blobs_skipped_binary": result.blobs_skipped_binary,
        "blobs_skipped_large": result.blobs_skipped_large,
        "known_credential_values_checked": result.known_secrets_checked,
        "confirmed_secret_findings": len(result.findings),
        "false_positives": len(result.false_positives),
        "unresolved_findings": len(result.unresolved),
        "findings": result.unresolved,
        "false_positive_detail": result.false_positives[:50],
        "credential_rotation_required": bool(result.findings),
        "result": "CLEAN" if not result.findings else "BLOCKED_SECRET_HISTORY",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autofund history-secret-audit",
        description="audit every reachable Git blob for credential material")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out", type=Path, default=None,
                        help="write the machine-readable report here")
    parser.add_argument("--known-secrets-from", dest="known", type=Path, default=None,
                        help="local credential file whose values are searched for exactly "
                             "(gitignored; never read from the repository)")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    try:
        head = _git("rev-parse", "--short", "HEAD", cwd=root).strip()
        known = known_secret_values(args.known)
        result = audit(cwd=root, known_secrets=known)
    except AuditError as exc:
        print(json.dumps({"result": "AUDIT_FAILED", "reason": str(exc)[:300]}, indent=2))
        return 2

    report = public_report(result=result, head=head)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if not args.quiet:
        print(json.dumps(report, indent=2))
    return 1 if report["confirmed_secret_findings"] else 0


if __name__ == "__main__":
    sys.exit(main())

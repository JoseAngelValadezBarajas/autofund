"""Deterministic registry rebuild: `python -m autofund.research.rebuild`.

The registry is a read model over artifacts, so it must be reproducible from those artifacts alone.
This entry point exists so an operator — or a future scheduled job — can rebuild it without the
application running, and so the rebuild is testable as a unit rather than only as a side effect of
startup.

**It never mutates anything it reads.** The only files it writes are the registry snapshot under
its own directory and an optional JSON report to stdout. Financial truth is not touched: the ledger
and journal are not opened for writing, and no code path here can place an order.

**Idempotent by construction.** The input is a fixed set of files and the ordering is deterministic,
so running it twice produces byte-identical output. `--check` asserts that by rebuilding in memory
and comparing against the stored snapshot, which makes a silent change to the read model visible as
a non-zero exit rather than as a dashboard that quietly disagrees with yesterday's.
"""

from __future__ import annotations

import argparse
import json
import sys
from hashlib import sha256
from pathlib import Path

from .artifacts import REGISTRY_OUTPUT_DIR, ArtifactIndexError
from .registry import ResearchRegistryBuilder

REGISTRY_FILE = "research-registry.json"
REPORT_FILE = "research-registry-report.json"


def repository_root() -> Path:
    """The repository this package lives in."""
    return Path(__file__).resolve().parents[3]


def registry_digest(registry_view: dict[str, object]) -> str:
    """A content hash of the read model, excluding the build timestamp.

    The timestamp is excluded deliberately: it changes every run and would make the digest useless
    for detecting a real change. Everything else is included, so any difference in a record — a new
    alpha source, a changed classification, an artifact appearing or vanishing — changes the hash.
    """
    stable = {key: value for key, value in registry_view.items() if key != "built_at"}
    payload = json.dumps(stable, sort_keys=True, default=str)
    return sha256(payload.encode("utf-8")).hexdigest()


def build_registry(*, root: Path) -> tuple[object, dict[str, object], str]:
    """Build the registry and return it with its public view and digest."""
    builder = ResearchRegistryBuilder(artifacts_root=root / "artifacts", repository_root=root)
    registry = builder.build()
    view = registry.public()
    # The digest covers every record, not just the counts, so a change anywhere is detected.
    detailed: dict[str, object] = {
        **view,
        "alpha_sources": [item.public() for item in registry.alpha_sources],
        "strategies": [item.public() for item in registry.strategies],
        "experiments": [item.public() for item in registry.experiments],
        "evidence": [item.public() for item in registry.evidence],
        "campaigns": [item.public() for item in registry.campaigns],
        "eligibility": [item.public() for item in registry.eligibility],
        "certifications": [item.public() for item in registry.certifications],
        "artifacts": list(registry.artifacts),
    }
    return registry, detailed, registry_digest(detailed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="autofund research index",
        description="rebuild the research registry read model from local artifacts")
    parser.add_argument("--root", type=Path, default=None,
                        help="repository root (defaults to the package's own repository)")
    parser.add_argument("--out", type=Path, default=None,
                        help="where to write the registry snapshot")
    parser.add_argument("--check", action="store_true",
                        help="rebuild and compare against the stored snapshot without writing")
    parser.add_argument("--quiet", action="store_true", help="suppress the JSON report")
    args = parser.parse_args(argv)

    root = (args.root or repository_root()).resolve(strict=False)
    out_dir = args.out or (root / "artifacts" / REGISTRY_OUTPUT_DIR)
    try:
        registry, detailed, digest = build_registry(root=root)
    except ArtifactIndexError as exc:
        print(json.dumps({"status": "FAILED", "reason": str(exc)}, indent=2))
        return 2

    snapshot = out_dir / REGISTRY_FILE
    if args.check:
        if not snapshot.exists():
            print(json.dumps({"status": "NO_SNAPSHOT", "digest": digest}, indent=2))
            return 1
        stored = json.loads(snapshot.read_text(encoding="utf-8"))
        stored_digest = stored.get("digest")
        consistent = stored_digest == digest
        print(json.dumps({"status": "CONSISTENT" if consistent else "CHANGED",
                          "stored_digest": stored_digest, "rebuilt_digest": digest},
                         indent=2))
        return 0 if consistent else 1

    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(
        json.dumps({"digest": digest, "registry": detailed}, indent=2, default=str) + "\n",
        encoding="utf-8")

    counts = registry.counts  # type: ignore[attr-defined]
    report = {"status": "OK", "digest": digest, "root": str(root),
              "snapshot": str(snapshot.relative_to(root)), "counts": counts}
    if not args.quiet:
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

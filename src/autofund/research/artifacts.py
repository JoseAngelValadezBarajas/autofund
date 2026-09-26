"""A safe, bounded index over the existing research artifacts.

The registry needs to reference the evidence on disk, and the temptation with a task like that is
to walk the filesystem and read everything. Two things make that a bad idea here:

**The artifacts are large.** The biggest certification is 5.5 MB. Deserialising several of those on
every dashboard request is the precise performance failure the spec names, so the index records a
file's size, modification time and a fingerprint computed from a streamed read, and never keeps
the payload in memory beyond what a single summary needs.

**Paths are the boundary.** A registry that will read a path a caller supplies can be pointed at
anything on the machine, so every path is resolved and checked to be inside a declared root before
a byte is read. `resolve()` runs first, which collapses `..` and symlinks, so a traversal attempt
fails the containment check rather than being normalised into a legitimate-looking path. This is
the one place in the milestone where a mistake would be a security bug rather than a wrong number.

**Missing and broken files are ordinary outcomes.** A milestone may have been cleaned up, a write
may have been interrupted, a schema may be from a future version. Each is recorded with a status
rather than raised, because a Control Center that crashes when one artifact is absent is worse than
one that reports the absence.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

INDEX_VERSION = "autofund.artifact-index.v1"

# How much of a file to read when computing a fingerprint. A full read of every artifact would
# cost seconds of I/O for a value used only to detect change, and the artifact sizes here run to
# megabytes. The head and tail plus the size are enough to notice a rewrite in practice, and the
# size is already part of the identity.
FINGERPRINT_HEAD_BYTES = 64 * 1024
FINGERPRINT_TAIL_BYTES = 16 * 1024

# ---- index statuses ----
INDEXED = "INDEXED"
MISSING = "MISSING"
EMPTY = "EMPTY"
INVALID = "INVALID"
UNSUPPORTED = "UNSUPPORTED"

# ---- archive states ----
CURRENT = "CURRENT"
SUPERSEDED = "SUPERSEDED"
ARCHIVED = "ARCHIVED"

# A superseded artifact is one the project itself archived by renaming, so the naming convention is
# the signal and it is already established by the certification scripts.
SUPERSEDED_MARKER = ".superseded-"

# A read model must not index its own output. If it does, the first rebuild adds one file to the
# input set, the next build adds a slightly different one, and the digest never converges — which
# would look exactly like the repository changing on every refresh and destroy the value of having
# a digest at all. These are directory prefixes, relative to an artifact root, that are derived
# from the index rather than being source evidence for it.
DERIVED_OUTPUT_PREFIXES: tuple[str, ...] = ("research",)

# Where the registry snapshot is written, relative to an artifact root. Kept beside the exclusion
# above so the writer and the indexer cannot disagree about the location.
REGISTRY_OUTPUT_DIR = "research"


class ArtifactIndexError(ValueError):
    """The index was asked to do something outside its declared boundary."""


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """One file in the index, described without holding its contents."""

    relative_path: str
    artifact_type: str
    status: str
    archive_state: str = CURRENT
    size_bytes: int | None = None
    modified_at: str | None = None
    fingerprint: str | None = None
    experiment_id: str | None = None
    summary: dict[str, Any] | None = None
    error: str | None = None

    def public(self) -> dict[str, Any]:
        return {"relative_path": self.relative_path, "artifact_type": self.artifact_type,
                "status": self.status, "archive_state": self.archive_state,
                "size_bytes": self.size_bytes, "modified_at": self.modified_at,
                "fingerprint": self.fingerprint, "experiment_id": self.experiment_id,
                "summary": self.summary, "error": self.error}


def is_within(*, root: Path, candidate: Path) -> bool:
    """Whether `candidate` lies inside `root`, after resolving both.

    Resolution happens before the comparison, so `root/../../etc/passwd` resolves outside and is
    rejected. Checking the unresolved string would let a traversal through, because the text still
    starts with the root's prefix.
    """
    try:
        resolved_root = root.resolve(strict=False)
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return resolved == resolved_root or resolved_root in resolved.parents


def resolve_artifact_path(*, root: Path, relative: str) -> Path:
    """Turn a caller-supplied relative path into an absolute path, or refuse.

    Refusing is the point. Every artifact reference in the registry comes from a scan this module
    performed, so a path arriving from outside that set is either a bug or an attack, and both
    deserve an exception rather than a best-effort read.
    """
    if not relative or relative.strip() == "":
        raise ArtifactIndexError("an artifact path is required")
    # An absolute path is rejected outright rather than joined, because `Path(root) / "/etc/passwd"`
    # silently discards the root.
    if Path(relative).is_absolute():
        raise ArtifactIndexError(f"refusing an absolute artifact path: {relative}")
    if ".." in Path(relative).parts:
        raise ArtifactIndexError(f"refusing a path traversal: {relative}")
    candidate = root / relative
    if not is_within(root=root, candidate=candidate):
        raise ArtifactIndexError(f"path escapes the artifact root: {relative}")
    return candidate


def _fingerprint(path: Path, *, size: int) -> str:
    """A cheap content fingerprint: size plus the head and tail of the file."""
    digest = hashlib.sha256()
    digest.update(str(size).encode("ascii"))
    with path.open("rb") as handle:
        digest.update(handle.read(FINGERPRINT_HEAD_BYTES))
        if size > FINGERPRINT_HEAD_BYTES + FINGERPRINT_TAIL_BYTES:
            handle.seek(-FINGERPRINT_TAIL_BYTES, os.SEEK_END)
            digest.update(handle.read(FINGERPRINT_TAIL_BYTES))
    return digest.hexdigest()


def classify_artifact(*, relative_path: str) -> str:
    """What kind of artifact a path represents, from its name.

    Naming conventions rather than content inspection, because the project's certification scripts
    already write predictably named files and reading every candidate to classify it would defeat
    the point of an index.
    """
    name = Path(relative_path).name.lower()
    if name.endswith(".jsonl"):
        return "JSONL_STREAM"
    if name.endswith(".json"):
        if "certification" in name:
            return "CERTIFICATION"
        if "manifest" in name:
            return "MANIFEST"
        if "report" in name:
            return "REPORT"
        return "JSON_DOCUMENT"
    if name.endswith(".log") or name.endswith(".txt"):
        return "LOG"
    if name.endswith(".md"):
        return "DOCUMENT"
    return "OTHER"


def archive_state_of(*, relative_path: str) -> str:
    """Whether the project archived this artifact by renaming it."""
    return SUPERSEDED if SUPERSEDED_MARKER in Path(relative_path).name else CURRENT


@dataclass(frozen=True, slots=True)
class ArtifactIndex:
    """The indexed set of artifacts under one or more declared roots."""

    records: tuple[ArtifactRecord, ...]
    roots: tuple[str, ...]
    built_at: str

    def public(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for record in self.records:
            by_status[record.status] = by_status.get(record.status, 0) + 1
            by_type[record.artifact_type] = by_type.get(record.artifact_type, 0) + 1
        return {
            "version": INDEX_VERSION, "built_at": self.built_at, "roots": list(self.roots),
            "artifact_count": len(self.records), "by_status": by_status, "by_type": by_type,
            "total_bytes": sum(record.size_bytes or 0 for record in self.records),
            "superseded_count": sum(1 for record in self.records
                                    if record.archive_state == SUPERSEDED),
            "invalid_count": sum(1 for record in self.records
                                 if record.status in (INVALID, UNSUPPORTED)),
        }


class ArtifactIndexer:
    """Scans declared roots and produces artifact records.

    Bounded on three axes: an explicit root list, a maximum file size to inspect, and a hard cap on
    how many files are listed. A research repository grows, and an indexer without limits eventually
    becomes the reason the dashboard is slow.
    """

    def __init__(self, *, roots: tuple[Path, ...], max_file_bytes: int = 32 * 1024 * 1024,
                 max_records: int = 5000,
                 include_patterns: tuple[str, ...] = (".json", ".jsonl"),
                 exclude_prefixes: tuple[str, ...] = DERIVED_OUTPUT_PREFIXES) -> None:
        if not roots:
            raise ArtifactIndexError("at least one artifact root is required")
        self.roots = tuple(root.resolve(strict=False) for root in roots)
        self.max_file_bytes = max_file_bytes
        self.max_records = max_records
        self.include_patterns = include_patterns
        self.exclude_prefixes = tuple(
            prefix.replace("\\", "/").strip("/") for prefix in exclude_prefixes)

    def _candidates(self) -> Iterator[tuple[Path, Path]]:
        """Yield (root, file) pairs, skipping anything outside the declared roots."""
        produced = 0
        for root in self.roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*")):
                if produced >= self.max_records:
                    return
                if not path.is_file():
                    continue
                if path.suffix.lower() not in self.include_patterns:
                    continue
                relative = str(path.relative_to(root)).replace("\\", "/")
                if self._is_derived_output(relative):
                    continue
                # A symlink pointing outside the root is skipped rather than followed: following
                # it would read a file the index is not scoped to.
                if not is_within(root=root, candidate=path):
                    continue
                produced += 1
                yield root, path

    def _is_derived_output(self, relative: str) -> bool:
        """True when a path is this read model's own output rather than source evidence."""
        return any(relative == prefix or relative.startswith(prefix + "/")
                   for prefix in self.exclude_prefixes)

    def index(self) -> ArtifactIndex:
        records: list[ArtifactRecord] = []
        for root, path in self._candidates():
            relative = str(path.relative_to(root))
            record = self._describe(root=root, path=path, relative=relative)
            records.append(record)
        return ArtifactIndex(records=tuple(records),
                             roots=tuple(str(root) for root in self.roots),
                             built_at=datetime.now(UTC).isoformat())

    def _describe(self, *, root: Path, path: Path, relative: str) -> ArtifactRecord:
        artifact_type = classify_artifact(relative_path=relative)
        archive_state = archive_state_of(relative_path=relative)
        try:
            stat = path.stat()
        except OSError as exc:
            return ArtifactRecord(relative_path=relative, artifact_type=artifact_type,
                                  status=MISSING, archive_state=archive_state,
                                  error=type(exc).__name__)
        if stat.st_size == 0:
            return ArtifactRecord(relative_path=relative, artifact_type=artifact_type,
                                  status=EMPTY, archive_state=archive_state, size_bytes=0,
                                  modified_at=_iso(stat.st_mtime))
        if stat.st_size > self.max_file_bytes:
            return ArtifactRecord(relative_path=relative, artifact_type=artifact_type,
                                  status=UNSUPPORTED, archive_state=archive_state,
                                  size_bytes=stat.st_size, modified_at=_iso(stat.st_mtime),
                                  error="FILE_EXCEEDS_INDEX_BOUND")
        try:
            fingerprint = _fingerprint(path, size=stat.st_size)
        except OSError as exc:
            return ArtifactRecord(relative_path=relative, artifact_type=artifact_type,
                                  status=UNSUPPORTED, archive_state=archive_state,
                                  size_bytes=stat.st_size, modified_at=_iso(stat.st_mtime),
                                  error=type(exc).__name__)
        summary: dict[str, Any] | None = None
        status = INDEXED
        error: str | None = None
        if artifact_type == "CERTIFICATION":
            summary, status, error = self._summarise(path)
        return ArtifactRecord(relative_path=relative, artifact_type=artifact_type, status=status,
                              archive_state=archive_state, size_bytes=stat.st_size,
                              modified_at=_iso(stat.st_mtime), fingerprint=fingerprint,
                              summary=summary, error=error)

    @staticmethod
    def _summarise(path: Path) -> tuple[dict[str, Any] | None, str, str | None]:
        """Read a certification's small top-level scalars without keeping the whole document.

        The document is still parsed — there is no streaming JSON parser in the standard library —
        but only the scalar top level is retained, so a 5.5 MB artifact contributes a few dozen
        bytes to the index and its large nested arrays are released immediately.
        """
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return None, INVALID, type(exc).__name__
        if not isinstance(payload, dict):
            return None, INVALID, "NOT_A_JSON_OBJECT"
        scalars = {key: value for key, value in payload.items()
                   if isinstance(value, (str, int, float, bool)) or value is None}
        # Truncate long strings so a summary cannot smuggle a large payload into the read model.
        summary = {key: (value[:200] if isinstance(value, str) else value)
                   for key, value in scalars.items()}
        summary["_top_level_keys"] = len(payload)
        return summary, INDEXED, None


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()

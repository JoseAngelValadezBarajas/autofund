"""Single-writer append-only journal: fsync before any external write.

Wallet is rebuilt from committed fill records, so a crash between journal
append and Wallet application cannot double-apply a fill on restart.
"""

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from autofund.replay.serialization import canonical_json

from .errors import ExchangeInvariantError
from .parsing import obj


class ExecutionJournal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lockfile: BinaryIO = open(str(path) + ".lock", "a+b")
        self._closed = False
        try:
            self._lockfile.seek(0)
            if os.name == "nt":
                import msvcrt

                if self._lockfile.read(1) == b"":
                    self._lockfile.write(b"0")
                    self._lockfile.flush()
                self._lockfile.seek(0)
                # `msvcrt` exists only on Windows and `fcntl` only on POSIX, but mypy checks the
                # union of both branches, so each line is flagged on the platform that lacks its
                # module. A bare `attr-defined` ignore then becomes an "unused ignore" error on the
                # other platform under --strict, which is how this passed locally on Windows and
                # failed in CI on Linux. Both codes are therefore required, on both lines.
                msvcrt.locking(self._lockfile.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined, unused-ignore]
            else:
                import fcntl

                fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined, unused-ignore]
        except OSError:
            self._lockfile.close()
            raise ExchangeInvariantError("execution journal already in use") from None
        self.records: list[dict[str, object]] = []
        self._hash = "0" * 64
        try:
            if path.exists():
                data = path.read_bytes()
                if data and not data.endswith(b"\n"):
                    raise ExchangeInvariantError(
                        "incomplete journal tail; manual recovery required"
                    )
                for line in data.splitlines():
                    envelope = obj(json.loads(line))
                    event = obj(envelope.get("event"))
                    expected = self._digest(event)
                    if (
                        envelope.get("hash") != expected
                        or event.get("sequence") != len(self.records) + 1
                    ):
                        raise ExchangeInvariantError("journal integrity failure")
                    self._hash = expected
                    self.records.append(event)
        except Exception:
            self.close()
            raise ExchangeInvariantError("journal cannot be recovered safely") from None

    def _digest(self, event: dict[str, object]) -> str:
        return hashlib.sha256((self._hash + canonical_json(event)).encode()).hexdigest()

    def append(self, kind: str, data: object) -> None:
        if self._closed:
            raise ExchangeInvariantError("journal is closed")
        event = obj(
            json.loads(
                canonical_json(
                    {
                        "sequence": len(self.records) + 1,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "kind": kind,
                        "data": data,
                    }
                )
            )
        )
        digest = self._digest(event)
        try:
            with self.path.open("ab") as output:
                output.write(
                    (canonical_json({"event": event, "hash": digest}) + "\n").encode()
                )
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            self.close()
            raise ExchangeInvariantError(
                "durable journal append failed; execution stopped"
            ) from None
        self._hash = digest
        self.records.append(event)

    def close(self) -> None:
        if not self._closed:
            self._lockfile.close()
            self._closed = True

    def __enter__(self) -> "ExecutionJournal":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

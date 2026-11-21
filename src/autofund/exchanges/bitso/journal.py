"""Single-writer append-only journal: fsync before any external write.

Wallet is rebuilt from committed fill records, so a crash between journal
append and Wallet application cannot double-apply a fill on restart.

Integrity, in two layers
------------------------
1. **Hash chain** (always). Each record commits to the previous one, so accidental corruption and
   any edit that is not recomputed end to end is detected.
2. **Authentication** (when a ``mac_key`` is supplied). Each record also carries an HMAC of its chain
   hash under a secret the journal file does not contain. Someone who can edit the file can no longer
   recompute a valid chain, because they cannot produce the MACs. The first authenticated record
   authenticates everything before it too (its hash commits to the whole prefix), so a journal that
   predates this feature upgrades in place: nothing is rewritten, and once authentication has begun
   an unauthenticated record is rejected as a downgrade.

A signed **anchor** (``<journal>.anchor``) records the latest sequence number and hash. It is what
detects a *truncated* journal: dropping the tail leaves a valid chain, but the anchor would then
point past the end. An attacker who can roll back the journal and the anchor together to an older
consistent pair is not detected by any local mechanism; that needs a witness outside this machine.
"""

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from autofund.replay.serialization import canonical_json

from .errors import ExchangeInvariantError
from .parsing import obj


class ExecutionJournal:
    def __init__(self, path: Path, *, mac_key: bytes | None = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._anchor_path = Path(str(path) + ".anchor")
        self._mac_key = mac_key
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
        self._hashes: list[str] = []
        self._hash = "0" * 64
        #: True once every record from the first authenticated one onward verified under the key.
        self.authenticated = False
        try:
            if path.exists():
                self._load(path.read_bytes())
            self._verify_anchor()
        except Exception:
            self.close()
            raise ExchangeInvariantError("journal cannot be recovered safely") from None

    # ---- integrity ----

    def _digest(self, event: dict[str, object]) -> str:
        return hashlib.sha256((self._hash + canonical_json(event)).encode()).hexdigest()

    def _mac(self, text: str) -> str:
        if self._mac_key is None:  # explicit, so it still holds under python -O
            raise ExchangeInvariantError("journal has no authentication key")
        return hmac.new(self._mac_key, text.encode(), hashlib.sha256).hexdigest()

    def _load(self, data: bytes) -> None:
        if data and not data.endswith(b"\n"):
            raise ExchangeInvariantError("incomplete journal tail; manual recovery required")
        mac_started = False
        for line in data.splitlines():
            envelope = obj(json.loads(line))
            event = obj(envelope.get("event"))
            expected = self._digest(event)
            if envelope.get("hash") != expected or event.get("sequence") != len(self.records) + 1:
                raise ExchangeInvariantError("journal integrity failure")
            mac = envelope.get("mac")
            if self._mac_key is not None:
                if mac is not None:
                    if not isinstance(mac, str) or not hmac.compare_digest(mac, self._mac(expected)):
                        raise ExchangeInvariantError("journal authentication failure")
                    mac_started = True
                elif mac_started:
                    raise ExchangeInvariantError("journal authentication downgrade")
            self._hash = expected
            self._hashes.append(expected)
            self.records.append(event)
        self.authenticated = self._mac_key is not None and mac_started

    def _anchor_text(self, sequence: int, chain_hash: str) -> str:
        return f"anchor|{sequence}|{chain_hash}"

    def _verify_anchor(self) -> None:
        if self._mac_key is None:
            return
        try:
            raw = self._anchor_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # A journal that has authenticated records but lost its anchor could have been truncated.
            # One record is allowed: a crash between the first append and the first anchor write.
            if self.authenticated and len(self.records) > 1:
                raise ExchangeInvariantError("journal anchor missing") from None
            return
        row = obj(json.loads(raw))
        sequence, chain_hash, mac = row.get("sequence"), row.get("hash"), row.get("mac")
        if not isinstance(sequence, int) or not isinstance(chain_hash, str) or not isinstance(mac, str):
            raise ExchangeInvariantError("journal anchor malformed")
        if not hmac.compare_digest(mac, self._mac(self._anchor_text(sequence, chain_hash))):
            raise ExchangeInvariantError("journal anchor authentication failure")
        if sequence > len(self.records) or (sequence > 0 and self._hashes[sequence - 1] != chain_hash):
            raise ExchangeInvariantError("journal truncated or rewritten")
        if sequence > 0 and not self.authenticated:
            # A signed anchor proves the key was in use, so a journal with no MACs left was stripped.
            raise ExchangeInvariantError("journal authentication downgrade")

    def _write_anchor(self) -> None:
        if self._mac_key is None:
            return
        sequence = len(self.records)
        body = canonical_json({
            "sequence": sequence, "hash": self._hash,
            "mac": self._mac(self._anchor_text(sequence, self._hash)),
        })
        tmp = Path(str(self._anchor_path) + ".tmp")
        with tmp.open("wb") as output:
            output.write(body.encode())
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, self._anchor_path)

    # ---- writing ----

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
        envelope: dict[str, object] = {"event": event, "hash": digest}
        if self._mac_key is not None:
            envelope["mac"] = self._mac(digest)
        try:
            with self.path.open("ab") as output:
                output.write((canonical_json(envelope) + "\n").encode())
                output.flush()
                os.fsync(output.fileno())
        except OSError:
            self.close()
            raise ExchangeInvariantError(
                "durable journal append failed; execution stopped"
            ) from None
        self._hash = digest
        self._hashes.append(digest)
        self.records.append(event)
        if self._mac_key is not None:
            self.authenticated = True
            try:
                self._write_anchor()
            except OSError:
                self.close()
                raise ExchangeInvariantError(
                    "journal anchor write failed; execution stopped"
                ) from None

    def close(self) -> None:
        if not self._closed:
            self._lockfile.close()
            self._closed = True

    def __enter__(self) -> "ExecutionJournal":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

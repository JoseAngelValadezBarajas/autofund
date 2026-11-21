"""The execution journal resists tampering, not just corruption."""

import hashlib
import json
from pathlib import Path

import pytest

from autofund.exchanges.bitso.errors import ExchangeInvariantError
from autofund.exchanges.bitso.journal import ExecutionJournal
from autofund.live.journal import LiveExecutionJournal
from autofund.replay.serialization import canonical_json

KEY = b"k" * 32
OTHER = b"o" * 32


def fill(path: Path, n: int, key: bytes | None) -> None:
    with ExecutionJournal(path, mac_key=key) as journal:
        for i in range(n):
            journal.append("EVENT", {"i": i, "note": "order"})


def lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def rewrite_chain(path: Path, rows: list[dict], *, keep_macs: bool) -> None:
    """What an attacker with file access but no key can do: recompute every hash."""
    previous, out = "0" * 64, []
    for row in rows:
        digest = hashlib.sha256((previous + canonical_json(row["event"])).encode()).hexdigest()
        envelope = {"event": row["event"], "hash": digest}
        if keep_macs and "mac" in row:
            envelope["mac"] = row["mac"]  # the old MAC, which no longer matches the new hash
        out.append(canonical_json(envelope))
        previous = digest
    path.write_text("\n".join(out) + "\n")


def test_authenticated_journal_round_trips(tmp_path):
    path = tmp_path / "j.jsonl"
    fill(path, 4, KEY)
    assert all("mac" in r for r in lines(path))
    with ExecutionJournal(path, mac_key=KEY) as journal:
        assert journal.authenticated and len(journal.records) == 4
        journal.append("EVENT", {"i": 4})
    with ExecutionJournal(path, mac_key=KEY) as journal:
        assert len(journal.records) == 5


def test_a_wrong_key_cannot_open_it(tmp_path):
    path = tmp_path / "j.jsonl"
    fill(path, 3, KEY)
    with pytest.raises(ExchangeInvariantError):
        ExecutionJournal(path, mac_key=OTHER)


def test_editing_an_event_and_recomputing_the_chain_is_caught(tmp_path):
    path = tmp_path / "j.jsonl"
    fill(path, 4, KEY)
    rows = lines(path)
    rows[1]["event"]["data"]["note"] = "forged"
    rewrite_chain(path, rows, keep_macs=True)  # a perfect chain, but the MACs cannot be redone
    with pytest.raises(ExchangeInvariantError):
        ExecutionJournal(path, mac_key=KEY)


def test_stripping_every_mac_is_a_downgrade_not_a_free_pass(tmp_path):
    path = tmp_path / "j.jsonl"
    fill(path, 4, KEY)
    rows = lines(path)
    # A valid chain with no MACs at all: the event hashes are unchanged, so only the signed anchor,
    # which proves the key was in use, can tell that the authentication was removed.
    rewrite_chain(path, rows, keep_macs=False)
    with pytest.raises(ExchangeInvariantError):
        ExecutionJournal(path, mac_key=KEY)


def test_removing_a_mac_from_one_record_after_authentication_began_is_rejected(tmp_path):
    path = tmp_path / "j.jsonl"
    fill(path, 4, KEY)
    rows = lines(path)
    del rows[2]["mac"]
    path.write_text("\n".join(canonical_json(r) for r in rows) + "\n")
    with pytest.raises(ExchangeInvariantError):
        ExecutionJournal(path, mac_key=KEY)


def test_truncating_the_tail_is_detected_by_the_anchor(tmp_path):
    path = tmp_path / "j.jsonl"
    fill(path, 5, KEY)
    kept = path.read_text().splitlines()[:3]
    path.write_text("\n".join(kept) + "\n")  # a perfectly valid, shorter chain
    with pytest.raises(ExchangeInvariantError):
        ExecutionJournal(path, mac_key=KEY)


def test_deleting_the_anchor_to_enable_truncation_is_detected(tmp_path):
    path = tmp_path / "j.jsonl"
    fill(path, 5, KEY)
    Path(str(path) + ".anchor").unlink()
    with pytest.raises(ExchangeInvariantError):
        ExecutionJournal(path, mac_key=KEY)


def test_a_forged_anchor_is_rejected(tmp_path):
    path = tmp_path / "j.jsonl"
    fill(path, 3, KEY)
    anchor_path = Path(str(path) + ".anchor")
    anchor = json.loads(anchor_path.read_text())
    anchor["sequence"] = 99
    anchor_path.write_text(canonical_json(anchor))
    with pytest.raises(ExchangeInvariantError):
        ExecutionJournal(path, mac_key=KEY)


def test_a_legacy_journal_upgrades_in_place_and_the_prefix_becomes_authenticated(tmp_path):
    path = tmp_path / "legacy.jsonl"
    fill(path, 3, None)  # written before authentication existed
    before = path.read_bytes()
    with ExecutionJournal(path, mac_key=KEY) as journal:
        assert not journal.authenticated  # nothing authenticated yet
        journal.append("EVENT", {"i": 3})
        assert journal.authenticated
    assert path.read_bytes().startswith(before)  # the old records are untouched, never rewritten
    # Rewriting the legacy prefix now breaks the MAC on the new record, which covers the prefix.
    rows = lines(path)
    rows[0]["event"]["data"]["note"] = "forged legacy record"
    rewrite_chain(path, rows, keep_macs=True)
    with pytest.raises(ExchangeInvariantError):
        ExecutionJournal(path, mac_key=KEY)


def test_a_keyless_open_of_an_authenticated_journal_reads_but_does_not_claim_authentication(tmp_path):
    path = tmp_path / "j.jsonl"
    fill(path, 3, KEY)
    with ExecutionJournal(path) as journal:
        assert len(journal.records) == 3 and not journal.authenticated


def test_a_crash_before_the_first_anchor_is_not_a_false_alarm(tmp_path):
    path = tmp_path / "j.jsonl"
    fill(path, 1, KEY)
    Path(str(path) + ".anchor").unlink()
    with ExecutionJournal(path, mac_key=KEY) as journal:
        assert len(journal.records) == 1


def test_the_live_journal_is_authenticated_by_default_with_a_stored_key(tmp_path):
    path = tmp_path / "live.jsonl"
    with LiveExecutionJournal(path) as journal:
        journal.append("LIVE_TEST", {"x": 1})
        assert journal.authenticated
    with LiveExecutionJournal(path) as journal:  # the same stored key reopens it
        assert journal.authenticated and len(journal.records) == 1
    assert "mac" in lines(path)[0]


def test_the_live_journal_still_refuses_non_live_events(tmp_path):
    from autofund.live.models import LiveError
    with LiveExecutionJournal(tmp_path / "live.jsonl") as journal, pytest.raises(LiveError):
        journal.append("NOT_LIVE", {})

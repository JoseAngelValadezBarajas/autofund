"""Stripping the authentication and the anchor must not make a journal look 'old'.

This is the attack the automated review found: with only file access, remove every MAC and delete the
anchor. The file then resembles a journal from before authentication existed. The credential store
remembers that this journal *was* authenticated, which the attacker cannot undo from the files.
"""

import hashlib
import json
from pathlib import Path

import pytest

from autofund import secrets_cli
from autofund.live.journal import LiveExecutionJournal
from autofund.live.models import LiveError
from autofund.replay.serialization import canonical_json


def write_live(path: Path, n: int) -> None:
    with LiveExecutionJournal(path) as journal:
        for i in range(n):
            journal.append("LIVE_TEST", {"i": i})


def lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()]


def strip_macs_keep_valid_chain(path: Path) -> None:
    """Same events, same hashes, no MACs: a perfectly valid unauthenticated chain."""
    rows = [{"event": r["event"], "hash": r["hash"]} for r in lines(path)]
    path.write_text("\n".join(canonical_json(r) for r in rows) + "\n")


def anchor(path: Path) -> Path:
    return Path(str(path) + ".anchor")


def test_a_new_live_journal_is_authenticated_from_its_first_record_and_remembered(tmp_path):
    from autofund import secrets_store
    path = tmp_path / "live.jsonl"
    assert not secrets_store.journal_was_authenticated(path)
    write_live(path, 2)
    assert secrets_store.journal_was_authenticated(path)
    with LiveExecutionJournal(path) as journal:
        assert journal.authenticated and len(journal.records) == 2


def test_stripping_the_macs_AND_deleting_the_anchor_is_still_caught(tmp_path):
    path = tmp_path / "live.jsonl"
    write_live(path, 4)
    strip_macs_keep_valid_chain(path)
    anchor(path).unlink()
    # The file now looks exactly like a pre-authentication journal. Only the remembered state knows.
    with pytest.raises(Exception) as info:
        LiveExecutionJournal(path)
    assert "recovered safely" in str(info.value) or "downgrade" in str(info.value)


def test_deleting_only_the_anchor_is_caught(tmp_path):
    path = tmp_path / "live.jsonl"
    write_live(path, 4)
    anchor(path).unlink()
    with pytest.raises(Exception):
        LiveExecutionJournal(path)


def test_truncating_to_one_record_and_deleting_the_anchor_is_caught(tmp_path):
    path = tmp_path / "live.jsonl"
    write_live(path, 5)
    path.write_text(path.read_text().splitlines()[0] + "\n")
    anchor(path).unlink()  # the "one record, no anchor" crash allowance must not be an escape hatch
    with pytest.raises(Exception):
        LiveExecutionJournal(path)


def test_deleting_the_whole_journal_is_caught_not_silently_restarted(tmp_path):
    path = tmp_path / "live.jsonl"
    write_live(path, 3)
    path.unlink()
    anchor(path).unlink()
    with pytest.raises(Exception):
        LiveExecutionJournal(path)


def test_a_forged_legacy_journal_cannot_replace_an_authenticated_one(tmp_path):
    path = tmp_path / "live.jsonl"
    write_live(path, 3)
    # Forge a plausible unauthenticated history from scratch and drop it in place.
    previous, rows = "0" * 64, []
    for i in range(3):
        event = {"sequence": i + 1, "timestamp": "2026-01-01T00:00:00+00:00", "kind": "LIVE_TEST", "data": {"i": i}}
        digest = hashlib.sha256((previous + canonical_json(event)).encode()).hexdigest()
        rows.append(canonical_json({"event": event, "hash": digest}))
        previous = digest
    path.write_text("\n".join(rows) + "\n")
    anchor(path).unlink()
    with pytest.raises(Exception):
        LiveExecutionJournal(path)


# ---- a journal that genuinely predates authentication ----

def write_legacy(path: Path, n: int) -> None:
    with LiveExecutionJournal(path, authenticate=False) as journal:
        for i in range(n):
            journal.append("LIVE_OLD", {"i": i})


def test_a_legacy_journal_is_not_upgraded_silently(tmp_path):
    path = tmp_path / "legacy.jsonl"
    write_legacy(path, 3)
    with pytest.raises(LiveError, match="LIVE_JOURNAL_NEEDS_MIGRATION"):
        LiveExecutionJournal(path)
    assert "mac" not in lines(path)[0]  # and nothing was written by the refusal


def test_migration_is_an_explicit_confirmed_operator_step(tmp_path, capsys):
    from autofund import secrets_store
    path = tmp_path / "legacy.jsonl"
    write_legacy(path, 3)
    assert secrets_cli.main(["migrate-journal", str(path), "--confirm", "wrong phrase"]) == 1
    assert not secrets_store.journal_was_authenticated(path)
    assert secrets_cli.main(["migrate-journal", str(path), "--confirm", secrets_cli.MIGRATE_PHRASE]) == 0
    out = capsys.readouterr().out
    assert "3 records" in out and "now authenticated" in out
    assert secrets_store.journal_was_authenticated(path)
    with LiveExecutionJournal(path) as journal:  # opens normally now
        assert journal.authenticated and len(journal.records) == 4
        assert journal.records[-1]["kind"] == "LIVE_JOURNAL_AUTHENTICATION_STARTED"
    assert "mac" not in lines(path)[0] and "mac" in lines(path)[-1]  # old records untouched, new signed


def test_after_migration_the_downgrade_attack_is_caught_too(tmp_path):
    path = tmp_path / "legacy.jsonl"
    write_legacy(path, 3)
    secrets_cli.main(["migrate-journal", str(path), "--confirm", secrets_cli.MIGRATE_PHRASE])
    strip_macs_keep_valid_chain(path)
    anchor(path).unlink()
    with pytest.raises(Exception):
        LiveExecutionJournal(path)


def test_migrating_an_already_authenticated_journal_is_a_no_op(tmp_path, capsys):
    path = tmp_path / "live.jsonl"
    write_live(path, 2)
    assert secrets_cli.main(["migrate-journal", str(path)]) == 0
    assert "already authenticated" in capsys.readouterr().out


def test_a_missing_journal_has_nothing_to_migrate(tmp_path, capsys):
    assert secrets_cli.main(["migrate-journal", str(tmp_path / "none.jsonl")]) == 0
    assert "authenticated from its first record" in capsys.readouterr().out


def test_deliberately_starting_a_fresh_journal_needs_an_explicit_reset(tmp_path):
    path = tmp_path / "live.jsonl"
    write_live(path, 2)
    path.unlink()
    anchor(path).unlink()
    with pytest.raises(Exception):
        LiveExecutionJournal(path)  # not silently allowed
    assert secrets_cli.main(["reset-journal", str(path), "--confirm", "nope"]) == 1
    assert secrets_cli.main(["reset-journal", str(path), "--confirm", secrets_cli.RESET_PHRASE]) == 0
    write_live(path, 1)  # a genuinely new journal, authenticated afresh
    with LiveExecutionJournal(path) as journal:
        assert journal.authenticated


def test_a_crash_between_the_first_append_and_the_marker_is_recovered_not_fatal(tmp_path):
    from autofund import secrets_store
    path = tmp_path / "live.jsonl"
    write_live(path, 2)
    secrets_store.clear_journal_marker(path)  # as if the marker write never happened
    with LiveExecutionJournal(path) as journal:  # the MACs and anchor are valid: remember and carry on
        assert journal.authenticated
    assert secrets_store.journal_was_authenticated(path)


# ---- the operator is told the real reason ----

@pytest.mark.parametrize("message,expected", [
    ("LIVE_JOURNAL_NEEDS_MIGRATION", "LIVE_JOURNAL_NEEDS_MIGRATION"),
    ("execution journal already in use", "EXECUTION_JOURNAL_IN_USE"),
    ("journal cannot be recovered safely", "EXECUTION_JOURNAL_INTEGRITY_FAILURE"),
    ("journal authentication downgrade", "EXECUTION_JOURNAL_INTEGRITY_FAILURE"),
])
def test_each_journal_failure_gets_its_own_blocker_with_guidance(message, expected):
    from autofund.mvp.runners import journal_blocker_for
    from autofund.mvp.session_types import BLOCKER_GUIDANCE
    blocker = journal_blocker_for(RuntimeError(message))
    assert blocker == expected and BLOCKER_GUIDANCE[blocker]


def test_the_migration_guidance_names_the_command():
    from autofund.mvp.session_types import BLOCKER_GUIDANCE
    assert "autofund secrets migrate-journal" in BLOCKER_GUIDANCE["LIVE_JOURNAL_NEEDS_MIGRATION"]

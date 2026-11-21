"""Live-only semantic boundary around the certified locking/fsync journal."""

import os
from pathlib import Path

from autofund.exchanges.bitso.journal import ExecutionJournal, JournalMigrationRequired

from .models import LiveError

DEFAULT_JOURNAL = Path(__file__).parents[3] / "artifacts" / "live" / "execution.jsonl"


def journal_path_is_redirected(path: Path) -> bool:
    """True if the journal, or any directory above it, is a symlink or a Windows junction.

    A link in that position lets someone point the application at a different journal than the one
    the operator configured. Refusing links outright is simpler and safer than trying to decide which
    ones are benign.
    """
    current = os.path.abspath(path)
    while True:
        if os.path.islink(current) or os.path.isjunction(current):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


class _StoredAuthState:
    """Remembers, in the credential store, that this journal has been authenticated."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def is_set(self) -> bool:
        from autofund.secrets_store import journal_was_authenticated

        return journal_was_authenticated(self._path)

    def set(self) -> None:
        from autofund.secrets_store import mark_journal_authenticated

        mark_journal_authenticated(self._path)


class LiveExecutionJournal(ExecutionJournal):
    def __enter__(self) -> "LiveExecutionJournal":
        return self

    def __init__(self, path: Path = DEFAULT_JOURNAL, *, authenticate: bool = True,
                 migrate: bool = False) -> None:
        # The live journal is the record of real orders, so it is authenticated by default with a
        # key kept in the credential store (see exchanges/bitso/journal.py). A journal that already
        # holds records from before authentication existed is NOT upgraded silently: someone who
        # could edit the file could otherwise present a forged one as "old". The operator migrates it
        # on purpose with `autofund secrets migrate-journal`, which passes migrate=True.
        from autofund.secrets_store import journal_key

        if journal_path_is_redirected(path):
            raise LiveError("LIVE_JOURNAL_PATH_REDIRECTED")
        try:
            super().__init__(path, mac_key=journal_key() if authenticate else None,
                             auth_state=_StoredAuthState(path) if authenticate else None,
                             allow_upgrade=migrate)
        except JournalMigrationRequired:
            raise LiveError("LIVE_JOURNAL_NEEDS_MIGRATION") from None
        if any(not str(row.get("kind", "")).startswith("LIVE_") for row in self.records):
            self.close()
            raise LiveError("NOT_A_LIVE_EXECUTION_JOURNAL")

    def append(self, kind: str, data: object) -> None:
        if not kind.startswith("LIVE_"):
            raise LiveError("INVALID_LIVE_JOURNAL_EVENT")
        super().append(kind, data)

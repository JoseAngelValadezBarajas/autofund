"""Live-only semantic boundary around the certified locking/fsync journal."""

from pathlib import Path

from autofund.exchanges.bitso.journal import ExecutionJournal

from .models import LiveError

DEFAULT_JOURNAL = Path(__file__).parents[3] / "artifacts" / "live" / "execution.jsonl"


class LiveExecutionJournal(ExecutionJournal):
    def __enter__(self) -> "LiveExecutionJournal":
        return self

    def __init__(self, path: Path = DEFAULT_JOURNAL, *, authenticate: bool = True) -> None:
        # The live journal is the record of real orders, so it is authenticated by default with a
        # key kept in the credential store (see exchanges/bitso/journal.py). `authenticate=False`
        # exists only to open a journal in its legacy, hash-chain-only form.
        from autofund.secrets_store import journal_key

        super().__init__(path, mac_key=journal_key() if authenticate else None)
        if any(not str(row.get("kind", "")).startswith("LIVE_") for row in self.records):
            self.close()
            raise LiveError("NOT_A_LIVE_EXECUTION_JOURNAL")

    def append(self, kind: str, data: object) -> None:
        if not kind.startswith("LIVE_"):
            raise LiveError("INVALID_LIVE_JOURNAL_EVENT")
        super().append(kind, data)

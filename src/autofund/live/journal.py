"""Live-only semantic boundary around the certified locking/fsync journal."""

from pathlib import Path

from autofund.exchanges.bitso.journal import ExecutionJournal

from .models import LiveError

DEFAULT_JOURNAL = Path(__file__).parents[3] / "artifacts" / "live" / "execution.jsonl"


class LiveExecutionJournal(ExecutionJournal):
    def __enter__(self) -> "LiveExecutionJournal":
        return self

    def __init__(self, path: Path = DEFAULT_JOURNAL) -> None:
        super().__init__(path)
        if any(not str(row.get("kind", "")).startswith("LIVE_") for row in self.records):
            self.close()
            raise LiveError("NOT_A_LIVE_EXECUTION_JOURNAL")

    def append(self, kind: str, data: object) -> None:
        if not kind.startswith("LIVE_"):
            raise LiveError("INVALID_LIVE_JOURNAL_EVENT")
        super().append(kind, data)

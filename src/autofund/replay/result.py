from dataclasses import dataclass, field, fields
from datetime import datetime
from pathlib import Path

from .config import ReplayConfig
from .data import HistoricalDataset
from .metrics import PerformanceMetrics
from .records import ReplayTrace
from .serialization import canonical_json, fingerprint
from .strategy import StrategyIdentity


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    dataset: HistoricalDataset
    config: ReplayConfig
    strategy: StrategyIdentity
    trace: ReplayTrace
    metrics: PerformanceMetrics
    schema_version: str = field(default="autofund.benchmark.v1", init=False)
    market: str = field(init=False)
    start_timestamp: datetime = field(init=False)
    end_timestamp: datetime = field(init=False)
    candle_count: int = field(init=False)
    run_id: str = field(init=False)
    dataset_fingerprint: str = field(init=False)
    config_fingerprint: str = field(init=False)
    strategy_fingerprint: str = field(init=False)
    result_fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "market", self.dataset.market)
        object.__setattr__(self, "start_timestamp", self.dataset.candles[0].timestamp)
        object.__setattr__(self, "end_timestamp", self.dataset.candles[-1].timestamp)
        object.__setattr__(self, "candle_count", len(self.dataset.candles))
        object.__setattr__(self, "dataset_fingerprint", self.dataset.fingerprint)
        object.__setattr__(self, "config_fingerprint", self.config.fingerprint)
        object.__setattr__(self, "strategy_fingerprint", self.strategy.fingerprint)
        run_key = {
            "schema": "autofund.run.v1",
            "dataset": self.dataset_fingerprint,
            "config": self.config_fingerprint,
            "strategy": self.strategy_fingerprint,
        }
        object.__setattr__(self, "run_id", "run-" + fingerprint(run_key))
        object.__setattr__(
            self, "result_fingerprint", fingerprint(self.fingerprint_payload())
        )

    def fingerprint_payload(self) -> dict[str, object]:
        return {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name not in ("run_id", "result_fingerprint")
        }

    def to_json(self, path: str | Path | None = None) -> str:
        """Export full dataset/config/strategy, trace, metrics and all identities."""
        text = canonical_json(self)
        if path is not None:
            Path(path).write_text(text + "\n", encoding="utf-8")
        return text

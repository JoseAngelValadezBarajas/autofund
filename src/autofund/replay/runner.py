from dataclasses import dataclass, field

from .config import ReplayConfig
from .data import HistoricalDataset
from .engine import ReplayEngine
from .errors import StrategyContractError
from .metrics import calculate_metrics
from .result import BenchmarkResult
from .strategy import Strategy, StrategyIdentity


@dataclass(frozen=True, slots=True)
class ReplayRunner:
    config: ReplayConfig = field(default_factory=ReplayConfig)

    def run(self, *, dataset: HistoricalDataset, strategy: Strategy) -> BenchmarkResult:
        identity = strategy.identity
        if not isinstance(identity, StrategyIdentity):
            raise StrategyContractError("strategy.identity must be a StrategyIdentity")
        trace = ReplayEngine(self.config).run(dataset, strategy)
        if strategy.identity != identity:
            raise StrategyContractError("strategy identity changed during replay")
        return BenchmarkResult(
            dataset,
            self.config,
            identity,
            trace,
            calculate_metrics(self.config.initial_equity, trace),
        )

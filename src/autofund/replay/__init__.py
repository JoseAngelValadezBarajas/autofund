"""F1 deterministic local replay, layered above the frozen F0 financial core."""

from .clock import ReplayClock
from .config import ReplayConfig
from .data import Candle, HistoricalDataset, load_csv
from .engine import ReplayEngine
from .errors import DatasetValidationError, ReplayValidationError, StrategyContractError
from .records import (
    ClosedTrade,
    EquityPhase,
    EquityPoint,
    IntentOutcome,
    IntentStatus,
    ReplayTrace,
    SignalRecord,
    TradeRecord,
)
from .result import BenchmarkResult
from .runner import ReplayRunner
from .serialization import canonical_json, fingerprint
from .strategy import (
    OrderIntent,
    PortfolioSnapshot,
    SimpleMeanReversionV0,
    Strategy,
    StrategyContext,
    StrategyIdentity,
)

__all__ = [
    "BenchmarkResult",
    "Candle",
    "ClosedTrade",
    "DatasetValidationError",
    "EquityPhase",
    "EquityPoint",
    "HistoricalDataset",
    "IntentOutcome",
    "IntentStatus",
    "OrderIntent",
    "PortfolioSnapshot",
    "ReplayClock",
    "ReplayConfig",
    "ReplayEngine",
    "ReplayRunner",
    "ReplayTrace",
    "ReplayValidationError",
    "SignalRecord",
    "SimpleMeanReversionV0",
    "Strategy",
    "StrategyContext",
    "StrategyContractError",
    "StrategyIdentity",
    "TradeRecord",
    "canonical_json",
    "fingerprint",
    "load_csv",
]

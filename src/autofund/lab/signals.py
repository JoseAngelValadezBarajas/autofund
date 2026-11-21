"""Strategy catalog and signal computation for the lab.

A signal is the same trailing-return rule the research harness evaluated: long when the latest
closed bar is above the close ``lookback`` bars earlier, otherwise cash. Signals are computed from
*closed* daily bars only (the request ends yesterday), so they never use an unfinished bar.

Whether a strategy is certified is not decided here. The catalog only reports what the private
research record says, and ``UNCERTIFIED`` is the default.
"""

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.decimal_utils import ZERO
from autofund.exchanges.alpaca.client import AlpacaDataClient
from autofund.exchanges.alpaca.errors import AlpacaSubscriptionError
from autofund.exchanges.alpaca.models import AlpacaBarSeries

DEFAULT_UNIVERSE: tuple[str, ...] = ("SPY", "QQQ", "TLT", "GLD")
MAX_LAG_DAYS = 4
HISTORY_DAYS = 460  # calendar days: enough closed bars for a 252-bar lookback plus slack


@dataclass(frozen=True, slots=True)
class Strategy:
    id: str
    label: str
    lookback: int
    universe: tuple[str, ...] = DEFAULT_UNIVERSE
    rebalance_every: int = 21

    @property
    def description(self) -> str:
        if self.lookback == 0:
            return ("Control: hold every ETF at equal weight, always. This is what the trend filters "
                    "have to beat, and in the research so far they did not.")
        return (f"Hold each ETF long while its close is above the close {self.lookback} trading days "
                f"earlier, otherwise cash. Equal weight, reviewed every {self.rebalance_every} trading days.")


STRATEGIES: dict[str, Strategy] = {
    s.id: s for s in (
        Strategy("buy-hold", "Control: equal-weight buy and hold", 0),
        Strategy("momentum-126", "Trend filter, 126 days", 126),
        Strategy("momentum-189", "Trend filter, 189 days", 189),
        Strategy("momentum-252", "Trend filter, 252 days", 252),
    )
}


@dataclass(frozen=True, slots=True)
class SignalRow:
    symbol: str
    as_of: str
    last_close: Decimal
    reference_close: Decimal
    trailing_return: Decimal
    position: int

    def public(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "as_of": self.as_of, "last_close": str(self.last_close),
                "reference_close": str(self.reference_close),
                "trailing_return_pct": f"{self.trailing_return * 100:.2f}",
                "position": self.position, "action": "HOLD LONG" if self.position else "CASH"}


@dataclass(frozen=True, slots=True)
class SignalSet:
    strategy_id: str
    as_of: str
    rows: tuple[SignalRow, ...]
    bars_since: Callable[[date | None], int]

    def position(self, symbol: str) -> int:
        return next(r.position for r in self.rows if r.symbol == symbol)


class SignalError(Exception):
    pass


def compute_signals(strategy: Strategy, series: dict[str, AlpacaBarSeries]) -> SignalSet:
    rows: list[SignalRow] = []
    dates: set[date] = set()
    for symbol in strategy.universe:
        s = series.get(symbol)
        if s is None:
            raise SignalError(f"no bars for {symbol}")
        closes = [c.close for c in s.candles]
        if len(closes) <= strategy.lookback:
            raise SignalError(f"{symbol} has {len(closes)} bars; {strategy.lookback + 1} are needed")
        last, ref = closes[-1], closes[-1 - strategy.lookback]
        always_long = strategy.lookback == 0
        dates.update(c.timestamp.date() for c in s.candles)
        rows.append(SignalRow(symbol, s.candles[-1].timestamp.date().isoformat(), last, ref,
                              (last / ref - 1) if ref else ZERO, 1 if always_long or last > ref else 0))
    ordered = sorted(dates)

    def bars_since(day: date | None) -> int:
        return len(ordered) if day is None else sum(1 for d in ordered if d > day)

    return SignalSet(strategy.id, max(r.as_of for r in rows), tuple(rows), bars_since)


class SignalProvider:
    """Fetches closed daily bars (cached briefly) and turns them into signals."""

    def __init__(self, data_client: Callable[[], AlpacaDataClient], *, ttl_seconds: float = 300,
                 today: Callable[[], date] = lambda: datetime.now(UTC).date(),
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self._client, self._ttl, self._today, self._mono = data_client, ttl_seconds, today, monotonic
        self._cache: dict[tuple[str, ...], tuple[float, dict[str, AlpacaBarSeries]]] = {}
        self._lock = threading.Lock()

    def bars(self, universe: tuple[str, ...]) -> dict[str, AlpacaBarSeries]:
        with self._lock:
            hit = self._cache.get(universe)
            if hit and self._mono() - hit[0] < self._ttl:
                return hit[1]
            # The free plan refuses very recent SIP data. Ask for yesterday's close first and step
            # back a day at a time; the bars returned say exactly which day they run to.
            last_error: AlpacaSubscriptionError | None = None
            for lag in range(1, MAX_LAG_DAYS + 1):
                end = self._today() - timedelta(days=lag)
                try:
                    fetched = self._client().get_daily_bars(
                        list(universe), end - timedelta(days=HISTORY_DAYS), end,
                        adjustment="all", feed="sip")
                except AlpacaSubscriptionError as exc:
                    last_error = exc
                    continue
                self._cache[universe] = (self._mono(), fetched)
                return fetched
            raise SignalError("the data plan refused every recent window") from last_error

    def signals(self, strategy_id: str) -> SignalSet:
        strategy = STRATEGIES.get(strategy_id)
        if strategy is None:
            raise SignalError("unknown strategy")
        return compute_signals(strategy, self.bars(strategy.universe))


def research_catalog(root: Path) -> list[dict[str, Any]]:
    """Read-only summary of private evaluation results. Missing or unreadable files are skipped."""
    out: list[dict[str, Any]] = []
    results = root / "results"
    if not results.is_dir():
        return out
    for path in sorted(results.glob("*evaluation*.json")):
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            out.append({
                "id": d["registration_id"], "decision": d["decision"],
                "selected_lookback": d.get("selected_lookback"),
                "holdout_net_sharpe": d["holdout"]["net_sharpe"],
                "benchmark_net_sharpe": d["benchmark_equal_weight_buy_and_hold"]["net_sharpe"],
                "timing_control_p": d["timing_control"]["p_value"],
                "trials_counted": d.get("trials_counted"),
            })
        except (KeyError, ValueError, OSError):
            continue
    return out


def certification_status(catalog: list[dict[str, Any]]) -> str:
    """``CERTIFIED_FOR_PAPER`` only if some registered hypothesis passed; never anything stronger."""
    return "CERTIFIED_FOR_PAPER" if any(e["decision"] == "PASS_TO_PAPER" for e in catalog) else "UNCERTIFIED"

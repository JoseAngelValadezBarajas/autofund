"""Historical candle sources for profile evaluation.

Two sources, clearly separated because they support different claims:

**Captured market data** (``load_captured_candles``) reconstructs 1-minute closes
from AutoFund's own recorded sessions. These are real observed BTC/MXN prices, so
replay over them is evidence about the real market. The reconstruction is
deliberately conservative: only genuinely observed closes are used, gaps are never
interpolated, and the resulting dataset carries a fingerprint so a result can be
tied to the exact series that produced it.

**Deterministic synthetic series** (``synthetic_candles``) exist for tests and for
demonstrating the architecture. They prove that the machinery admits a viable
opportunity and refuses an unviable one. They are explicitly labelled SYNTHETIC and
are *not* evidence about profitability. Conflating the two would be the exact
overclaiming this milestone must avoid.

No floats: every price is a Decimal parsed from the recorded string.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from autofund.decimal_utils import ZERO, financial
from autofund.replay.data import Candle, HistoricalDataset
from autofund.replay.serialization import fingerprint

DATA_SOURCE_VERSION = "autofund.candle-source.v1"

CAPTURED = "CAPTURED_MARKET_DATA"
SYNTHETIC = "SYNTHETIC_FIXTURE"


@dataclass(frozen=True, slots=True)
class CandleSeries:
    """A candle series with its provenance, so claims can be scoped correctly."""

    market: str
    candles: tuple[Candle, ...]
    source: str
    source_detail: str
    gaps: int = 0

    @property
    def is_real_market_data(self) -> bool:
        return self.source == CAPTURED

    @property
    def fingerprint(self) -> str:
        return fingerprint({"schema": DATA_SOURCE_VERSION, "market": self.market,
                            "source": self.source,
                            "candles": [[c.timestamp.isoformat(), str(c.close)] for c in self.candles]})

    def telemetry(self) -> dict[str, Any]:
        return {"version": DATA_SOURCE_VERSION, "market": self.market, "source": self.source,
                "source_detail": self.source_detail, "candles": len(self.candles),
                "gaps": self.gaps, "is_real_market_data": self.is_real_market_data,
                "dataset_fingerprint": self.fingerprint,
                "first_at": self.candles[0].timestamp.isoformat() if self.candles else None,
                "last_at": self.candles[-1].timestamp.isoformat() if self.candles else None}


def _parse_timestamp(value: str) -> datetime:
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@financial
def load_captured_candles(*, path: str | Path, market: str = "BTC/MXN",
                          limit: int | None = None) -> CandleSeries:
    """Reconstruct 1-minute closes from an AutoFund session telemetry log.

    Only close prices that AutoFund actually recorded are used. OHLC is not
    recovered, so `open`/`high`/`low` are set to the observed close and the series
    is explicitly marked as close-only. That keeps the ATR and range statistics
    conservative and honest: they see the closes that were observed rather than
    invented intrabar extremes.
    """
    rows: list[tuple[datetime, Decimal]] = []
    seen: set[datetime] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") != "STRATEGY_EVALUATED":
            continue
        if str(row.get("market", "")).upper().replace("_", "/") != market:
            continue
        raw_close = (row.get("features") or {}).get("close_mxn")
        stamp = row.get("timestamp_utc")
        if not raw_close or not stamp:
            continue
        try:
            observed = _parse_timestamp(str(stamp))
            price = Decimal(str(raw_close))
        except Exception:
            continue
        if price <= ZERO:
            continue
        minute = observed.replace(second=0, microsecond=0)
        if minute in seen:
            continue
        seen.add(minute)
        rows.append((minute, price))

    rows.sort(key=lambda item: item[0])
    if limit is not None:
        rows = rows[-limit:]
    candles = tuple(Candle(stamp, price, price, price, price, ZERO) for stamp, price in rows)
    gaps = 0
    for previous, current in pairwise(rows):
        if (current[0] - previous[0]) > timedelta(minutes=1):
            gaps += 1
    return CandleSeries(market=market, candles=candles, source=CAPTURED,
                        source_detail=str(path), gaps=gaps)


def session_telemetry_files(root: str | Path) -> tuple[Path, ...]:
    """Every recorded session telemetry log under `root`, newest last."""
    base = Path(root)
    if not base.exists():
        return ()
    return tuple(sorted(base.rglob("telemetry.jsonl"),
                        key=lambda p: p.stat().st_mtime if p.exists() else 0))


@financial
def synthetic_candles(*, prices: list[str], start: datetime | None = None,
                      market: str = "BTC/MXN", half_range_bps: Decimal = Decimal("20"),
                      volume: Decimal = Decimal("1")) -> CandleSeries:
    """Deterministic fixture series from explicit closes.

    `half_range_bps` gives each candle a symmetric high/low around its close, so
    true-range statistics are exercised without inventing asymmetric extremes.
    Strictly a fixture: it demonstrates the architecture and never claims real
    profitability.
    """
    begin = start or datetime(2026, 1, 1, tzinfo=UTC)
    candles: list[Candle] = []
    for index, raw in enumerate(prices):
        close = Decimal(raw)
        half = close * half_range_bps / Decimal("10000")
        candles.append(Candle(begin + timedelta(minutes=index), close, close + half,
                              close - half, close, volume))
    return CandleSeries(market=market, candles=tuple(candles), source=SYNTHETIC,
                        source_detail="deterministic fixture")


def to_dataset(series: CandleSeries) -> HistoricalDataset:
    """Wrap a series as the replay layer's dataset type."""
    return HistoricalDataset(market=series.market, candles=series.candles)

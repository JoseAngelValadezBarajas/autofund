import csv
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from autofund.decimal_utils import ZERO, decimal, market_name
from autofund.errors import InvalidFinancialInput

from .errors import DatasetValidationError, ReplayValidationError
from .serialization import fingerprint, utc_timestamp

_FIELDS = ("timestamp", "open", "high", "low", "close", "volume")
_ISO = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


@dataclass(frozen=True, slots=True)
class Candle:
    """Timestamp is the UTC candle label; open/close are ordered event phases."""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "timestamp", utc_timestamp(self.timestamp))
            for name in _FIELDS[1:]:
                decimal(getattr(self, name), name)
            if min(self.open, self.high, self.low, self.close) <= ZERO:
                raise DatasetValidationError("OHLC prices must be positive")
            if self.volume < ZERO:
                raise DatasetValidationError("volume must be nonnegative")
            if (
                not self.low <= self.open <= self.high
                or not self.low <= self.close <= self.high
            ):
                raise DatasetValidationError("OHLC values outside low/high bounds")
        except (InvalidFinancialInput, ReplayValidationError) as exc:
            raise DatasetValidationError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class HistoricalDataset:
    market: str
    candles: tuple[Candle, ...]
    schema_version: str = field(default="autofund.candles.v1", init=False)

    def __post_init__(self) -> None:
        try:
            market_name(self.market)
        except InvalidFinancialInput as exc:
            raise DatasetValidationError(str(exc)) from exc
        if not isinstance(self.candles, tuple) or not self.candles:
            raise DatasetValidationError("candles must be a nonempty immutable tuple")
        previous = None
        for candle in self.candles:
            if not isinstance(candle, Candle):
                raise DatasetValidationError("dataset contains a non-Candle")
            if previous is not None and candle.timestamp <= previous:
                raise DatasetValidationError(
                    "timestamps must be strictly increasing; duplicates forbidden"
                )
            previous = candle.timestamp

    @property
    def fingerprint(self) -> str:
        return fingerprint(self)


def load_csv(path: str | Path, *, market: str) -> HistoricalDataset:
    candles = []
    try:
        with Path(path).open(encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream, strict=True)
            if next(reader, None) != list(_FIELDS):
                raise DatasetValidationError(
                    "CSV header must be timestamp,open,high,low,close,volume"
                )
            for line, row in enumerate(reader, 2):
                if len(row) != len(_FIELDS):
                    raise DatasetValidationError(f"row {line}: expected six fields")
                if not _ISO.fullmatch(row[0]):
                    raise DatasetValidationError(
                        f"row {line}: timestamp must be aware ISO-8601"
                    )
                if any(not _NUMBER.fullmatch(value) for value in row[1:]):
                    raise DatasetValidationError(f"row {line}: invalid decimal text")
                try:
                    candles.append(
                        Candle(
                            datetime.fromisoformat(row[0]),
                            *(Decimal(value) for value in row[1:]),
                        )
                    )
                except (ValueError, InvalidOperation, DatasetValidationError) as exc:
                    raise DatasetValidationError(f"row {line}: {exc}") from exc
    except (csv.Error, UnicodeError) as exc:
        raise DatasetValidationError("invalid UTF-8 CSV") from exc
    return HistoricalDataset(market, tuple(candles))

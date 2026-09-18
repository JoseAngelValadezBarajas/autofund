from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal as D
from pathlib import Path

import pytest

from autofund.replay import DatasetValidationError, HistoricalDataset, load_csv

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.parametrize(
    "name", ["flat_market", "uptrend_market", "mean_reversion_market"]
)
def test_parse_synthetic_fixtures(name):
    data = load_csv(FIXTURES / f"{name}.csv", market="BTC/MXN")
    assert len(data.candles) == 24
    assert all(c.timestamp.tzinfo is UTC for c in data.candles)
    assert all(
        isinstance(getattr(c, key), D)
        for c in data.candles
        for key in ("open", "high", "low", "close", "volume")
    )
    assert len(data.fingerprint) == 64


@pytest.mark.parametrize("name", ["invalid_duplicate_timestamp", "invalid_ohlc"])
def test_invalid_fixtures(name):
    with pytest.raises(DatasetValidationError):
        load_csv(FIXTURES / f"{name}.csv", market="BTC/MXN")


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", D("0")),
        ("high", D("-1")),
        ("low", D("0")),
        ("close", D("102")),
        ("open", D("98")),
        ("volume", D("-0.01")),
        ("open", 100.0),
        ("close", 100),
        ("volume", D("NaN")),
        ("close", D("Infinity")),
    ],
)
def test_invalid_candle_values(mean_dataset, field, value):
    with pytest.raises(DatasetValidationError):
        replace(mean_dataset.candles[0], **{field: value})


def test_zero_volume_allowed(mean_dataset):
    assert replace(mean_dataset.candles[0], volume=D("0")).volume == D("0")


def test_naive_timestamp_rejected(mean_dataset):
    with pytest.raises(DatasetValidationError):
        replace(mean_dataset.candles[0], timestamp=datetime(2026, 1, 1))


def test_offsets_normalize_to_utc(mean_dataset):
    candle = mean_dataset.candles[0]
    alternative = replace(
        candle,
        timestamp=datetime(2025, 12, 31, 18, tzinfo=timezone(timedelta(hours=-6))),
    )
    assert alternative == candle
    assert alternative.timestamp.tzinfo is UTC


@pytest.mark.parametrize("rows", [(), "reversed", "duplicate", "list"])
def test_dataset_requires_nonempty_ordered_immutable_rows(mean_dataset, rows):
    candles = {
        "reversed": tuple(reversed(mean_dataset.candles)),
        "duplicate": (mean_dataset.candles[0],) * 2,
        "list": list(mean_dataset.candles),
    }.get(rows, rows)
    with pytest.raises(DatasetValidationError):
        HistoricalDataset("BTC/MXN", candles)


def test_dataset_immutable(mean_dataset):
    with pytest.raises(FrozenInstanceError):
        mean_dataset.candles[0].close = D("900")
    with pytest.raises(FrozenInstanceError):
        mean_dataset.market = "ETH/MXN"


def test_fingerprint_path_independent_and_semantically_canonical(
    tmp_path, mean_dataset
):
    copied = tmp_path / "renamed.csv"
    text = (FIXTURES / "mean_reversion_market.csv").read_text()
    copied.write_text(
        text.replace(",100,", ",100.000,").replace("Z,", "+00:00,"), encoding="utf-8"
    )
    assert load_csv(copied, market="BTC/MXN").fingerprint == mean_dataset.fingerprint
    assert (
        replace(mean_dataset, market="ETH/MXN").fingerprint != mean_dataset.fingerprint
    )
    first = replace(mean_dataset.candles[0], close=D("100.5"))
    assert (
        replace(mean_dataset, candles=(first, *mean_dataset.candles[1:])).fingerprint
        != mean_dataset.fingerprint
    )


@pytest.mark.parametrize(
    "row",
    [
        "2026-01-01T00:00:00,100,101,99,100,10",
        "2026-01-01T00:00:00Z,100,101,99,100",
        "2026-01-01T00:00:00Z,100,101,99,100,10,extra",
        "2026-01-01T00:00:00Z,NaN,101,99,100,10",
        "2026-01-01T00:00:00Z,1_00,101,99,100,10",
        "2026-13-01T00:00:00Z,100,101,99,100,10",
        "2026-01-01T00:00:00.1234567Z,100,101,99,100,10",
        "",
    ],
)
def test_strict_csv_rejects_malformed_rows(tmp_path, row):
    path = tmp_path / "invalid.csv"
    path.write_text("timestamp,open,high,low,close,volume\n" + row + "\n")
    with pytest.raises(DatasetValidationError):
        load_csv(path, market="BTC/MXN")


def test_wrong_header(tmp_path):
    path = tmp_path / "wrong.csv"
    path.write_text("open,high,low,close,volume,timestamp\n")
    with pytest.raises(DatasetValidationError):
        load_csv(path, market="BTC/MXN")

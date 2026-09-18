import asyncio
import json
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from autofund.market import (
    BinancePublicMarketDataSource,
    LiveMarketRunner,
    RecordedMarketDataSource,
    replay_capture,
)
from autofund.market.errors import (
    CaptureIntegrityError,
    ConfigurationError,
    UnsupportedQuote,
)
from autofund.market.models import QualityStatus
from autofund.replay import ReplayRunner, SimpleMeanReversionV0, canonical_json

FIXTURES = Path(__file__).parents[1] / "fixtures" / "binance"


def capture(path, transport_factory, fake_clock, messages):
    transport = transport_factory([messages])
    source = BinancePublicMarketDataSource(
        "BTCMXN", transport=transport, clock=fake_clock
    )
    result = asyncio.run(
        LiveMarketRunner(clock=fake_clock).run(source, output=path, closed_candles=6)
    )
    assert transport.closed_connections == 1
    return result


def test_live_normalized_recorded_events_and_signals_exact(
    tmp_path, transport_factory, fake_clock, messages
):
    path = tmp_path / "session.jsonl"
    live = capture(path, transport_factory, fake_clock, messages)
    replay = asyncio.run(replay_capture(path))
    assert live.events == replay.events
    assert live.candles == replay.candles
    assert live.decisions == replay.decisions
    assert live.normalized_session_fingerprint == replay.normalized_session_fingerprint
    assert live.signals_fingerprint == replay.signals_fingerprint
    assert canonical_json(live.events) == canonical_json(replay.events)
    assert live.raw_messages == 13
    assert live.closed_candles == 6
    assert live.quality.duplicates == 1
    assert live.strategy_signal_count == 2
    assert all(not hasattr(decision, "budget_mxn") for decision in live.decisions)
    assert live.quality.status is QualityStatus.VALID


def test_capture_contains_both_raw_and_normalized(
    tmp_path, transport_factory, fake_clock, messages
):
    path = tmp_path / "session.jsonl"
    live = capture(path, transport_factory, fake_clock, messages)
    normal = [json.loads(line) for line in path.read_text().splitlines()]
    raw = [
        json.loads(line)
        for line in path.with_suffix(".raw.jsonl").read_text().splitlines()
    ]
    assert normal[0]["schema_version"] == "autofund.capture.v1"
    assert len(raw) == 15  # raw header + 13 frames + footer
    assert raw[1]["payload_text"] == messages[0]
    values = [row["event"] for row in normal[1:-1] if row["event"]]
    assert values[0]["close"] == "1000000"
    assert values[0]["is_closed"] is False
    assert values[0]["source_event_time"] != normal[1]["received_at"]
    manifest = json.loads(path.with_suffix(".manifest.json").read_text())
    assert (
        manifest["report"]["normalized_session_fingerprint"]
        == live.normalized_session_fingerprint
    )
    assert len(manifest["capture_file_fingerprint"]) == 64


def test_paths_receipt_times_and_session_ids_do_not_affect_semantic_hash(
    tmp_path, transport_factory, fake_clock, messages
):
    first = capture(tmp_path / "first.jsonl", transport_factory, fake_clock, messages)
    fake_clock.advance(12345)
    second = capture(tmp_path / "second.jsonl", transport_factory, fake_clock, messages)
    assert first.session_id != second.session_id
    assert first.started_at != second.started_at
    assert first.normalized_session_fingerprint == second.normalized_session_fingerprint
    assert first.decisions == second.decisions
    left = json.loads((tmp_path / "first.manifest.json").read_text())
    right = json.loads((tmp_path / "second.manifest.json").read_text())
    assert left["capture_file_fingerprint"] != right["capture_file_fingerprint"]
    destination = tmp_path / "copy"
    destination.mkdir()
    for suffix in (".jsonl", ".raw.jsonl", ".manifest.json"):
        shutil.copy(tmp_path / ("first" + suffix), destination / ("renamed" + suffix))
    replay = asyncio.run(replay_capture(destination / "renamed.jsonl"))
    assert first.normalized_session_fingerprint == replay.normalized_session_fingerprint
    assert first.events == replay.events


@pytest.mark.parametrize("suffix", [".jsonl", ".raw.jsonl"])
def test_tamper_and_truncation_detected(
    tmp_path, transport_factory, fake_clock, messages, suffix
):
    path = tmp_path / "session.jsonl"
    capture(path, transport_factory, fake_clock, messages)
    target = path.with_suffix(suffix)
    target.write_bytes(target.read_bytes()[:-5])
    with pytest.raises(CaptureIntegrityError):
        RecordedMarketDataSource(path)


def test_manifest_semantic_hash_and_counts_verified(
    tmp_path, transport_factory, fake_clock, messages
):
    path = tmp_path / "session.jsonl"
    capture(path, transport_factory, fake_clock, messages)
    manifest_path = path.with_suffix(".manifest.json")
    original = json.loads(manifest_path.read_text())
    modified = json.loads(manifest_path.read_text())
    modified["report"]["normalized_session_fingerprint"] = "0" * 64
    manifest_path.write_text(json.dumps(modified))
    with pytest.raises(CaptureIntegrityError):
        RecordedMarketDataSource(path)
    original["report"]["closed_candles"] = 999
    manifest_path.write_text(json.dumps(original))
    with pytest.raises(CaptureIntegrityError):
        asyncio.run(replay_capture(path))


def test_existing_capture_not_overwritten(
    tmp_path, transport_factory, fake_clock, messages
):
    path = tmp_path / "session.jsonl"
    capture(path, transport_factory, fake_clock, messages)
    before = path.read_bytes()
    with pytest.raises(CaptureIntegrityError):
        capture(path, transport_factory, fake_clock, messages)
    assert path.read_bytes() == before


def test_closed_candles_export_to_f1_without_modifying_f1(
    tmp_path, transport_factory, fake_clock, messages
):
    result = capture(
        tmp_path / "session.jsonl", transport_factory, fake_clock, messages
    )
    dataset = result.to_f1_dataset()
    assert dataset.candles == result.candles
    runner = ReplayRunner()
    assert runner.run(dataset=dataset, strategy=SimpleMeanReversionV0()) == runner.run(
        dataset=dataset, strategy=SimpleMeanReversionV0()
    )
    assert dataset.market == "BTC/MXN"
    non_mxn = replace(result, market="BTC/USDT")
    with pytest.raises(UnsupportedQuote):
        non_mxn.to_f1_dataset()
    invalid = replace(
        result, quality=replace(result.quality, status=QualityStatus.INVALID)
    )
    with pytest.raises(ConfigurationError):
        invalid.to_f1_dataset(allow_degraded=True)
    degraded = replace(
        result, quality=replace(result.quality, status=QualityStatus.DEGRADED)
    )
    with pytest.raises(ConfigurationError):
        degraded.to_f1_dataset()
    assert degraded.to_f1_dataset(allow_degraded=True).candles == result.candles


def test_f2_golden_parity(tmp_path, transport_factory, fake_clock, messages):
    expected = json.loads((FIXTURES / "golden_expected.json").read_text())
    live = capture(tmp_path / "golden.jsonl", transport_factory, fake_clock, messages)
    replay = asyncio.run(replay_capture(tmp_path / "golden.jsonl"))
    assert live.raw_messages == expected["raw_messages"]
    assert live.normalized_updates == expected["normalized_updates"]
    assert live.closed_candles == expected["closed_candles"]
    assert live.quality.duplicates == expected["duplicates"]
    assert live.quality.gaps == expected["gaps"]
    assert live.strategy_signal_count == expected["signals"]
    assert live.normalized_session_fingerprint == expected["normalized_fingerprint"]
    assert live.events == replay.events
    assert live.decisions == replay.decisions

import asyncio
import json

import pytest

from autofund.market import (
    BinancePublicMarketDataSource,
    LiveMarketRunner,
    NetworkPolicy,
    QualityStatus,
    StaleMonitor,
    replay_capture,
)
from autofund.market.errors import ConfigurationError, NetworkUnavailable
from autofund.market.transport import PublicReply


def test_bounded_backoff_and_staleness_with_fake_clock(fake_clock):
    policy = NetworkPolicy(initial_delay=2, maximum_delay=9, multiplier=2)
    assert [policy.delay(i) for i in range(7)] == [2, 4, 8, 9, 9, 9, 9]
    monitor = StaleMonitor(fake_clock, 10)
    fake_clock.advance(9)
    assert not monitor.is_stale
    fake_clock.advance(1)
    assert monitor.is_stale
    monitor.seen()
    assert monitor.remaining == 10


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stale_after": 0},
        {"initial_delay": 0},
        {"maximum_delay": 0},
        {"multiplier": 0},
        {"max_reconnects": -1},
        {"metadata_attempts": 0},
        {"stale_after": 1.0},
    ],
)
def test_invalid_policy_fails_fast(kwargs):
    with pytest.raises(ConfigurationError):
        NetworkPolicy(**kwargs)


@pytest.mark.parametrize(
    "first",
    [OSError("reset"), PublicReply(503, "temporary"), PublicReply(429, "rate", 5)],
)
def test_metadata_transient_retry_respects_delay(transport_factory, fake_clock, first):
    transport = transport_factory([], replies=[first])
    source = BinancePublicMarketDataSource(
        "BTCMXN", transport=transport, clock=fake_clock
    )
    info = asyncio.run(source.get_market_info())
    assert info.symbol == "BTCMXN"
    assert len(transport.metadata_calls) == 2
    assert fake_clock.sleeps == (
        [5] if isinstance(first, PublicReply) and first.status == 429 else [1]
    )
    assert asyncio.run(source.get_market_info()) == info
    assert len(transport.metadata_calls) == 2  # Metadata cached for this session.


@pytest.mark.parametrize(
    "reply",
    [
        PublicReply(400, '{"code":-1121,"msg":"Invalid symbol."}'),
        PublicReply(403, "blocked"),
        PublicReply(418, "banned", 120),
        PublicReply(451, "restricted"),
        PublicReply(200, "{}"),
    ],
)
def test_metadata_permanent_errors_not_retried(transport_factory, fake_clock, reply):
    transport = transport_factory([], replies=[reply])
    with pytest.raises(ConfigurationError):
        asyncio.run(
            BinancePublicMarketDataSource(
                "BTCMXN", transport=transport, clock=fake_clock
            ).get_market_info()
        )
    assert len(transport.metadata_calls) == 1
    assert fake_clock.sleeps == []


def test_server_cooldown_is_not_shortened(transport_factory, fake_clock):
    transport = transport_factory([], replies=[PublicReply(429, "rate", 120)])
    with pytest.raises(NetworkUnavailable):
        asyncio.run(
            BinancePublicMarketDataSource(
                "BTCMXN", transport=transport, clock=fake_clock
            ).get_market_info()
        )
    assert fake_clock.sleeps == []
    assert len(transport.metadata_calls) == 1


def test_connect_failure_then_reconnect(
    tmp_path, transport_factory, fake_clock, messages
):
    transport = transport_factory([OSError("initial failure"), messages])
    source = BinancePublicMarketDataSource(
        "BTCMXN", transport=transport, clock=fake_clock
    )
    result = asyncio.run(
        LiveMarketRunner(clock=fake_clock).run(
            source, output=tmp_path / "capture.jsonl", closed_candles=6
        )
    )
    assert result.closed_candles == 6
    assert result.quality.reconnects == 1
    assert fake_clock.sleeps == [1]
    assert len(transport.connect_calls) == 2
    assert transport.closed_connections == 1
    assert result.quality.status is QualityStatus.DEGRADED


def test_disconnect_reconnect_and_duplicate(
    tmp_path, transport_factory, fake_clock, messages
):
    transport = transport_factory(
        [[*messages[:2], OSError("network reset")], [messages[1], *messages[2:]]]
    )
    source = BinancePublicMarketDataSource(
        "BTCMXN", transport=transport, clock=fake_clock
    )
    path = tmp_path / "capture.jsonl"
    result = asyncio.run(
        LiveMarketRunner(clock=fake_clock).run(source, output=path, closed_candles=6)
    )
    assert result.closed_candles == 6
    assert result.quality.duplicates == 2
    assert result.quality.reconnects == 1
    assert transport.closed_connections == 2
    replay = asyncio.run(replay_capture(path))
    assert result.events == replay.events
    assert result.decisions == replay.decisions
    assert result.quality == replay.quality


def test_idle_stream_timeout_reconnect(
    tmp_path, transport_factory, fake_clock, messages
):
    transport = transport_factory([[TimeoutError()], messages])
    source = BinancePublicMarketDataSource(
        "BTCMXN", transport=transport, clock=fake_clock
    )
    result = asyncio.run(
        LiveMarketRunner(clock=fake_clock).run(
            source, output=tmp_path / "capture.jsonl", closed_candles=6
        )
    )
    assert result.quality.stale_events == 1
    assert result.quality.reconnects == 1
    assert result.closed_candles == 6
    assert fake_clock.elapsed >= 15


def test_server_shutdown_control_message_is_captured(
    tmp_path, transport_factory, fake_clock, messages
):
    transport = transport_factory(
        [[json.dumps({"e": "serverShutdown", "E": 1767225600000})], messages]
    )
    source = BinancePublicMarketDataSource(
        "BTCMXN", transport=transport, clock=fake_clock
    )
    result = asyncio.run(
        LiveMarketRunner(clock=fake_clock).run(
            source, output=tmp_path / "capture.jsonl", closed_candles=6
        )
    )
    assert result.raw_messages == 14
    assert result.normalized_updates == 13
    assert result.quality.invalid_messages == 0
    assert result.quality.reconnects == 1


def test_retry_budget_is_finite(tmp_path, transport_factory, fake_clock):
    transport = transport_factory([OSError("reset")] * 3)
    source = BinancePublicMarketDataSource(
        "BTCMXN",
        transport=transport,
        clock=fake_clock,
        policy=NetworkPolicy(max_reconnects=2),
    )
    result = asyncio.run(
        LiveMarketRunner(clock=fake_clock).run(
            source, output=tmp_path / "capture.jsonl", closed_candles=1
        )
    )
    assert len(transport.connect_calls) == 3
    assert fake_clock.sleeps == [1, 2]
    assert result.quality.status is QualityStatus.INVALID
    assert result.quality.reconnects == 2


def test_isolated_invalid_message_recorded_then_continue(
    tmp_path, transport_factory, fake_clock, messages, caplog
):
    transport = transport_factory([["malformed", *messages]])
    source = BinancePublicMarketDataSource(
        "BTCMXN", transport=transport, clock=fake_clock
    )
    result = asyncio.run(
        LiveMarketRunner(clock=fake_clock).run(
            source, output=tmp_path / "capture.jsonl", closed_candles=6
        )
    )
    assert result.closed_candles == 6
    assert result.raw_messages == 14 and result.normalized_updates == 13
    assert result.quality.invalid_messages == 1
    assert "invalid_message reason=invalid JSON" in caplog.text
    assert result.quality.status is QualityStatus.DEGRADED


def test_invalid_message_threshold_stops(
    tmp_path, transport_factory, fake_clock, messages
):
    transport = transport_factory([["bad"] * 3 + messages])
    source = BinancePublicMarketDataSource(
        "BTCMXN", transport=transport, clock=fake_clock
    )
    result = asyncio.run(
        LiveMarketRunner(clock=fake_clock).run(
            source, output=tmp_path / "capture.jsonl", closed_candles=6
        )
    )
    assert result.raw_messages == 3
    assert result.closed_candles == 0
    assert result.quality.status is QualityStatus.INVALID
    assert transport.closed_connections == 1


def test_wrong_symbol_is_immediately_fatal(
    tmp_path, transport_factory, fake_clock, messages
):
    payload = json.loads(messages[0])
    payload["s"] = "ETHMXN"
    transport = transport_factory([[json.dumps(payload), *messages]])
    result = asyncio.run(
        LiveMarketRunner(clock=fake_clock).run(
            BinancePublicMarketDataSource(
                "BTCMXN", transport=transport, clock=fake_clock
            ),
            output=tmp_path / "wrong.jsonl",
            closed_candles=6,
        )
    )
    assert result.raw_messages == 1
    assert result.quality.status is QualityStatus.INVALID


def test_cancelled_task_flushes_valid_partial_bundle(
    tmp_path, transport_factory, fake_clock, messages
):
    transport = transport_factory([[*messages[:2], asyncio.CancelledError()]])
    source = BinancePublicMarketDataSource(
        "BTCMXN", transport=transport, clock=fake_clock
    )
    path = tmp_path / "partial.jsonl"
    result = asyncio.run(
        LiveMarketRunner(clock=fake_clock).run(source, output=path, closed_candles=6)
    )
    assert result.stop_reason == "interrupted"
    assert result.closed_candles == 1
    assert transport.closed_connections == 1
    replay = asyncio.run(replay_capture(path))
    assert replay.events == result.events
    assert replay.decisions == result.decisions
    assert replay.quality == result.quality
    for file in (path, path.with_suffix(".raw.jsonl")):
        for line in file.read_text().splitlines():
            json.loads(line)


def test_unknown_interval_before_network(transport_factory, fake_clock):
    transport = transport_factory([])
    with pytest.raises(ConfigurationError):
        BinancePublicMarketDataSource(
            "BTCMXN", "1M", transport=transport, clock=fake_clock
        )
    assert transport.metadata_calls == transport.connect_calls == []


def test_repeated_old_updates_cannot_hide_staleness(
    tmp_path, transport_factory, fake_clock, messages
):
    # One coherent close, then re-emission of that same exchange event forever.
    transport = transport_factory([[messages[1]] * 150])
    source = BinancePublicMarketDataSource(
        "BTCMXN",
        transport=transport,
        clock=fake_clock,
        policy=NetworkPolicy(stale_after=1, max_reconnects=0),
    )
    result = asyncio.run(
        LiveMarketRunner(clock=fake_clock).run(
            source, output=tmp_path / "stalled.jsonl", closed_candles=2
        )
    )
    assert result.quality.stale_events == 1
    assert result.raw_messages < 150

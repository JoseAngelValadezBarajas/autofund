"""Opt-in only: pytest -m live. No keys and no private endpoints."""

import asyncio

import pytest

from autofund.market import (
    BinancePublicMarketDataSource,
    LiveMarketRunner,
    QualityStatus,
    replay_capture,
)
from autofund.market.errors import ConfigurationError, NetworkUnavailable

pytestmark = [pytest.mark.integration, pytest.mark.live]


def test_live_public_metadata():
    try:
        info = asyncio.run(BinancePublicMarketDataSource("BTCMXN").get_market_info())
    except (NetworkUnavailable, ConfigurationError) as exc:
        pytest.skip(f"public endpoint/symbol unavailable: {exc}")
    assert info.market == "BTC/MXN"
    assert info.status == "TRADING"


def test_live_five_closed_candles_and_offline_parity(tmp_path):
    async def certify():
        source = BinancePublicMarketDataSource("BTCMXN", "1s")
        try:
            await source.get_market_info()
        except (NetworkUnavailable, ConfigurationError) as exc:
            pytest.skip(f"public endpoint/symbol unavailable: {exc}")
        path = tmp_path / "live.jsonl"
        live = await LiveMarketRunner().run(
            source, output=path, closed_candles=5, max_seconds=45
        )
        if live.closed_candles == 0 and any(
            issue.kind in ("disconnect", "configuration_error", "retry_exhausted")
            for issue in live.quality.issues
        ):
            pytest.skip("public WebSocket not accessible")
        replay = await replay_capture(path)
        assert live.closed_candles == 5
        assert live.quality.status is not QualityStatus.INVALID
        assert live.events == replay.events
        assert live.candles == replay.candles
        assert live.decisions == replay.decisions
        assert (
            live.normalized_session_fingerprint == replay.normalized_session_fingerprint
        )

    asyncio.run(certify())

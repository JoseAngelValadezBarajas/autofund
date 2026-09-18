from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from autofund.observer.errors import MarketDataInvalid
from autofund.observer.models import PublicTrade
from autofund.shadow.aggregation import TradeCandleAggregator


def test_golden_A_ohlcv_and_stable_id_tie_break(start):
    agg = TradeCandleAggregator("btc_mxn", start)
    values = tuple(
        PublicTrade(
            "btc_mxn", tid, start + timedelta(seconds=second), D(price), D("0.1"), "buy"
        )
        for tid, second, price in [
            (3, 20, "110"),
            (2, 10, "90"),
            (1, 10, "100"),
            (4, 50, "105"),
        ]
    )
    agg.ingest(values)
    assert not agg.close_until(start + timedelta(seconds=59))
    candle = agg.close_until(start + timedelta(minutes=1))[0]
    assert (candle.open, candle.high, candle.low, candle.close, candle.volume) == tuple(
        map(D, ("100", "110", "90", "105", "0.4"))
    )
    assert candle.timestamp == start and agg.out_of_order > 0


def test_empty_minutes_record_gap_never_fabricate(start):
    agg = TradeCandleAggregator("btc_mxn", start)
    assert agg.close_until(start + timedelta(minutes=2)) == ()
    assert agg.gaps == [start, start + timedelta(minutes=1)]


def test_golden_G_duplicate_and_contradiction(start):
    agg = TradeCandleAggregator("btc_mxn", start)
    trade = PublicTrade("btc_mxn", 1, start, D("100"), D("1"), "buy")
    agg.ingest((trade, trade))
    assert agg.duplicates == 1
    assert agg.close_until(start + timedelta(minutes=1))[0].volume == D("1")
    agg.ingest((trade,))
    with pytest.raises(MarketDataInvalid, match="contradictory"):
        agg.ingest((replace(trade, price=D("101")),))


def test_late_unique_trade_cannot_rewrite_closed_minute(start):
    agg = TradeCandleAggregator("btc_mxn", start)
    agg.close_until(start + timedelta(minutes=1))
    with pytest.raises(MarketDataInvalid, match="already closed"):
        agg.ingest((PublicTrade("btc_mxn", 1, start, D("100"), D("1"), "buy"),))


def test_partial_start_minute_is_excluded(start):
    agg = TradeCandleAggregator("btc_mxn", start)
    agg.ingest(
        (
            PublicTrade(
                "btc_mxn", 1, start - timedelta(seconds=1), D("100"), D("1"), "buy"
            ),
        )
    )
    assert not agg.pending


def test_quality_invalid_disables_benchmark(session, frames):
    session.process(frames[0])
    frame = replace(frames[1], trades=(replace(frames[0].trades[0], price=D("5")),))
    session.process(frame)
    assert session.quality == "INVALID" and session.halt_reason == "MARKET_DATA_HALT"
    assert session.result()["benchmark_candidate"] is False

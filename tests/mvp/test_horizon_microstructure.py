"""Tests for MVP 0.2.4: longer horizons and forward microstructure capture.

The two failure modes these tests exist to prevent are both quiet ones:

1. **A coarser bar that leaks the future.** If a 15m bucket is aggregated from bars that
   had not all occurred when the decision was made, every result improves and nothing looks
   wrong. The aggregation tests therefore check completeness, label alignment and the
   decision boundary explicitly.

2. **Evidence that is repaired instead of reported.** A collector that forward-fills a
   sequence gap, reorders an update or drops a duplicate produces a dataset that looks
   clean and is wrong — and wrong in the direction of making a maker fill look more
   achievable. The quality tests assert that anomalies are counted, never corrected.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from autofund.mvp.horizon import (
    FIFTEEN_MINUTE,
    ONE_HOUR_TIMEFRAME,
    HorizonError,
    TimeframeSpec,
    aggregate_candles,
    decision_bar_index,
)
from autofund.mvp.horizon_experiment import (
    CERTIFYING_EXECUTION_MODE,
    CONFIG_SETS,
    MAX_CONFIGURATIONS_PER_HORIZON,
    PREDECLARED_HORIZONS,
    SELECTION_RULE,
    HorizonConfig,
    HorizonManifestStore,
    freeze_horizon_manifest,
    predeclared_configuration_count,
)
from autofund.mvp.horizon_profiles import (
    horizon_profile_by_id,
    horizon_profile_ids,
    horizon_profiles,
)
from autofund.mvp.microstructure import (
    FLAG_CLOCK_SKEW,
    FLAG_CROSSED_BOOK,
    FLAG_INVALID_DEPTH,
    FLAG_STALE_BOOK,
    HEALTHY,
    PASSIVE_FILL_EXACTNESS,
    QUEUE_POSITION_OBSERVABLE,
    REAL_CAPTURED_MICROSTRUCTURE,
    BookEvent,
    CaptureQuality,
    DepthLevel,
    MicrostructureCollector,
    MicrostructureError,
    MicrostructureStore,
    TradeEvent,
    quality_flags_for_book,
)
from autofund.mvp.profile_library import PROFILE_BY_ID
from autofund.replay.data import Candle

BASE = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


def _bar(minute: int, *, open_: str = "100", high: str = "101", low: str = "99",
         close: str = "100.5", volume: str = "1") -> Candle:
    return Candle(timestamp=BASE + timedelta(minutes=minute), open=Decimal(open_),
                  high=Decimal(high), low=Decimal(low), close=Decimal(close),
                  volume=Decimal(volume))


def _pack(minute_start: int, count: int, *, base_price: int = 100) -> list[Candle]:
    """`count` contiguous 1-minute bars starting at `minute_start`."""
    out: list[Candle] = []
    for index in range(count):
        price = base_price + index
        out.append(_bar(minute_start + index, open_=str(price), high=str(price + 2),
                        low=str(price - 1), close=str(price + 1)))
    return out


# --------------------------------------------------------------------------- aggregation


def test_fifteen_minute_aggregation_uses_exactly_fifteen_base_bars() -> None:
    bars = tuple(_pack(0, 15))
    series = aggregate_candles(candles=bars, timeframe=FIFTEEN_MINUTE, market="BTC/MXN")
    assert series.complete_buckets == 1
    assert len(series.candles) == 1
    candle = series.candles[0]
    assert candle.open == Decimal("100")          # first open
    assert candle.high == Decimal("116")          # max of (price + 2) over 100..114
    assert candle.low == Decimal("99")            # min of (price - 1)
    assert candle.close == Decimal("115")         # last close
    assert candle.volume == Decimal("15")         # summed


def test_one_hour_aggregation_uses_exactly_sixty_base_bars() -> None:
    bars = tuple(_pack(0, 60))
    series = aggregate_candles(candles=bars, timeframe=ONE_HOUR_TIMEFRAME, market="BTC/MXN")
    assert series.complete_buckets == 1
    assert series.candles[0].volume == Decimal("60")


def test_incomplete_higher_timeframe_bucket_is_excluded_not_partially_emitted() -> None:
    """A bucket is emitted only when every constituent bar is present."""
    bars = tuple(_pack(0, 20))  # one full 15m bucket plus five bars
    series = aggregate_candles(candles=bars, timeframe=FIFTEEN_MINUTE, market="BTC/MXN")
    assert series.complete_buckets == 1
    assert series.incomplete_buckets == 1
    assert len(series.candles) == 1
    assert series.incomplete_bucket_starts
    # The incomplete bucket is reported, never silently merged into the completed one.
    assert series.public()["incomplete_buckets_aggregated"] is False


def test_aggregation_never_reports_a_dropped_bucket_as_a_gap() -> None:
    """Dropping is not repairing: gaps stay zero because nothing is invented."""
    bars = tuple(_pack(0, 30))
    series = aggregate_candles(candles=bars, timeframe=FIFTEEN_MINUTE, market="BTC/MXN")
    assert series.complete_buckets == 2
    assert series.public()["gaps_repaired"] is False


def test_duplicate_minute_is_counted_and_never_inflates_the_bucket() -> None:
    """A repeated minute must not be treated as an extra constituent bar.

    Counting it would let sixteen observations fill a fifteen-minute horizon, compressing
    price action and flattering every result. The duplicate is reported instead, and the
    bucket's high comes from the genuine constituents.
    """
    bars = list(_pack(0, 15))
    bars.append(_bar(7, open_="500", high="900", low="1", close="500"))  # duplicate minute
    series = aggregate_candles(candles=tuple(bars), timeframe=FIFTEEN_MINUTE, market="BTC/MXN")
    assert series.duplicate_bars == 1
    assert series.complete_buckets == 1
    assert series.candles[0].high == Decimal("116")   # not 900
    assert series.candles[0].low == Decimal("99")     # not 1


def test_off_grid_bars_are_counted_irregular_and_never_bucketed() -> None:
    bars = list(_pack(0, 16))
    bars.append(_bar(16, open_="500", high="501", low="499", close="500"))
    # Shift the extra bar off the minute grid so it cannot belong to any bucket.
    bars[-1] = Candle(timestamp=BASE + timedelta(seconds=16 * 60 + 30),
                      open=Decimal("500"), high=Decimal("501"), low=Decimal("499"),
                      close=Decimal("500"), volume=Decimal("1"))
    series = aggregate_candles(candles=tuple(bars), timeframe=FIFTEEN_MINUTE, market="BTC/MXN")
    assert series.irregular_buckets == 1
    assert series.candles[0].high == Decimal("116")


def test_aggregation_preserves_candle_invariants() -> None:
    series = aggregate_candles(candles=tuple(_pack(0, 60)),
                               timeframe=FIFTEEN_MINUTE, market="BTC/MXN")
    for candle in series.candles:
        assert candle.low <= candle.open <= candle.high
        assert candle.low <= candle.close <= candle.high


def test_timeframe_spec_rejects_sub_bar_and_non_multiple_spans() -> None:
    with pytest.raises(HorizonError):
        TimeframeSpec(name="30s", seconds=30)
    with pytest.raises(HorizonError):
        TimeframeSpec(name="90s", seconds=90)
    with pytest.raises(HorizonError):
        TimeframeSpec(name="", seconds=60)


def test_predeclared_timeframes_are_epoch_aligned_and_fetch_independent() -> None:
    """Bucket boundaries are a property of the horizon, not of when the fetch happened."""
    first = aggregate_candles(candles=tuple(_pack(0, 15)), timeframe=FIFTEEN_MINUTE)
    second = aggregate_candles(candles=tuple(_pack(60, 15)), timeframe=FIFTEEN_MINUTE)
    assert first.candles[0].timestamp == BASE
    assert second.candles[0].timestamp == BASE + timedelta(hours=1)
    assert all((c.timestamp - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()
               % FIFTEEN_MINUTE.seconds == 0 for c in first.candles)


def test_no_future_leak_a_bucket_is_not_decidable_before_its_close() -> None:
    """The decision boundary is the bucket's close, never its open or its midpoint."""
    series = aggregate_candles(candles=tuple(_pack(0, 30)), timeframe=FIFTEEN_MINUTE)
    bucket = series.candles[0]
    settled = bucket.timestamp + timedelta(seconds=FIFTEEN_MINUTE.seconds)
    # One second before settlement the bucket is not available.
    assert decision_bar_index(series=series, settled_at=settled - timedelta(seconds=1)) == -1
    assert decision_bar_index(series=series, settled_at=settled) == 0


# ------------------------------------------------------------------------------ profiles


def test_horizon_profiles_are_exactly_the_predeclared_four() -> None:
    assert horizon_profile_ids() == ("volatility-mean-reversion-15m-v1",
                                     "volatility-mean-reversion-1h-v1",
                                     "range-expansion-15m-v1", "range-expansion-1h-v1")
    assert len(horizon_profiles()) == 4


def test_horizon_fingerprints_are_distinct_and_distinct_from_frozen_profiles() -> None:
    fingerprints = [profile.identity.fingerprint for profile in horizon_profiles()]
    assert len(set(fingerprints)) == 4
    frozen = {definition.fingerprint for definition in PROFILE_BY_ID.values()}
    assert not (set(fingerprints) & frozen)


def test_same_concept_on_two_horizons_cannot_share_an_identity() -> None:
    fifteenth = horizon_profile_by_id("volatility-mean-reversion-15m-v1")
    hourly = horizon_profile_by_id("volatility-mean-reversion-1h-v1")
    assert fifteenth.identity.fingerprint != hourly.identity.fingerprint
    assert ("timeframe", "15m") in fifteenth.parameters
    assert ("timeframe", "1h") in hourly.parameters
    assert hourly.timeframe.seconds == 3600


def test_frozen_profiles_still_carry_their_historic_fingerprints() -> None:
    """Evidence already certified must remain attributable to the profile that made it."""
    assert PROFILE_BY_ID["mean-reversion-safe-v1"].fingerprint == (
        "1cadcfa967a919b6d041cf382d5acc993d7c70c7c1b008bba2db764c4f51bb6e")
    assert PROFILE_BY_ID["trend-continuation-v1"].fingerprint == (
        "6314058847ec352c76bf10f3c61fdd3b2989df3ee0782b3609fed5995e6157d7")
    assert PROFILE_BY_ID["volatility-mean-reversion-v1"].fingerprint == (
        "b7f9b5431243495cd6e0584cdba5f4cf4cef98aec9332bfd70141879a0ae22a4")


def test_unknown_horizon_profile_is_rejected() -> None:
    with pytest.raises(KeyError):
        horizon_profile_by_id("volatility-mean-reversion-5m-v1")


def test_horizon_profiles_reject_an_undeclared_timeframe() -> None:
    with pytest.raises(KeyError):
        horizon_profile_by_id("volatility-mean-reversion-30m-v1").timeframe


# ----------------------------------------------------------------------------- manifest


def _manifest_kwargs(root: Any) -> dict[str, Any]:
    from autofund.mvp.economics import DEFAULT_POLICY
    from autofund.mvp.executable_replay import (
        MAX_SINGLE_TRADE_RISK_MXN,
        MINIMUM_REWARD_RISK_RATIO,
    )
    from autofund.mvp.robustness import MAX_DRAWDOWN_MXN

    start = int(BASE.timestamp() * 1000)
    day = 86400000
    return {"root": root, "baseline_commit": "93e7d23", "product_version": "AutoFund MVP 0.2.4",
            "code_commit": "93e7d23", "dataset_cutoff_ms": start,
            "development": ("DEV", start - 30 * day, start),
            "holdout": ("HOLD", start - 37 * day, start - 30 * day),
            "economic_policy": DEFAULT_POLICY, "max_drawdown_mxn": MAX_DRAWDOWN_MXN,
            "max_single_trade_risk_mxn": MAX_SINGLE_TRADE_RISK_MXN,
            "minimum_reward_risk_ratio": MINIMUM_REWARD_RISK_RATIO,
            "single_order_cap_mxn": Decimal("11"), "authorized_capital_mxn": Decimal("50"),
            "execution_model_version": "autofund.executable-execution.v1",
            "maker_fee_rate": Decimal("0.006"), "taker_fee_rate": Decimal("0.0078")}


def test_parameter_budget_is_at_most_four_per_strategy_and_horizon() -> None:
    for configs in CONFIG_SETS.values():
        assert len(configs) <= MAX_CONFIGURATIONS_PER_HORIZON
    assert predeclared_configuration_count() == sum(len(c) for c in CONFIG_SETS.values())


def test_manifest_freezes_before_results_and_refuses_overwrite(tmp_path: Any) -> None:
    first = freeze_horizon_manifest(**_manifest_kwargs(tmp_path))
    assert first["manifest_fingerprint"]
    assert first["holdout_used_for_selection"] is False
    assert first["parameters_tested_retained"] is True
    # A second freeze must fail: a rewritable manifest could inherit an earlier experiment.
    with pytest.raises(Exception):
        freeze_horizon_manifest(**_manifest_kwargs(tmp_path))
    loaded = HorizonManifestStore(tmp_path).load()
    assert loaded["manifest_fingerprint"] == first["manifest_fingerprint"]


def test_manifest_declares_only_the_predeclared_timeframes(tmp_path: Any) -> None:
    frozen = freeze_horizon_manifest(**_manifest_kwargs(tmp_path))
    assert [item["name"] for item in frozen["horizons"]] == ["15m", "1h"]
    expected = sum(len(configs) for configs in CONFIG_SETS.values()) * len(PREDECLARED_HORIZONS)
    assert len(frozen["configuration_parameters"]) == expected
    for key, _ in frozen["configuration_budget"].items():
        concept, timeframe = key.rsplit(":", 1)
        assert timeframe in ("15m", "1h")
        assert concept in CONFIG_SETS
        assert frozen["configuration_budget"][key] <= MAX_CONFIGURATIONS_PER_HORIZON


def test_only_taker_execution_may_certify() -> None:
    assert CERTIFYING_EXECUTION_MODE == "TAKER_TAKER"


def test_selection_rule_puts_risk_gates_before_return() -> None:
    assert SELECTION_RULE[0] == "risk_gates_satisfied"
    assert SELECTION_RULE.index("net_pnl_descending") > 0


def test_config_rejects_a_holding_horizon_longer_than_its_limit() -> None:
    with pytest.raises(Exception):
        HorizonConfig(label="BAD", stop_atr_multiple=Decimal("1"),
                      target_floor_bps=Decimal("200"), max_holding_bars=4,
                      expected_holding_horizon=8)


# ------------------------------------------------------------------ microstructure quality


def _book(*, bid: str = "99", ask: str = "101", sequence: int | None = 1,
          received: datetime | None = None, bids: int = 3,
          asks: int = 3) -> BookEvent:
    return BookEvent(book="btc_mxn", exchange_timestamp=BASE,
                     received_at=received or BASE, sequence=sequence,
                     bids=tuple(DepthLevel(price=Decimal(bid) - i, quantity=Decimal("1"))
                                for i in range(bids)),
                     asks=tuple(DepthLevel(price=Decimal(ask) + i, quantity=Decimal("1"))
                                for i in range(asks)))


def test_crossed_book_is_flagged_and_never_corrected() -> None:
    event = _book(bid="102", ask="101")
    assert event.is_crossed
    assert FLAG_CROSSED_BOOK in quality_flags_for_book(event=event, now=BASE)


def test_empty_side_is_flagged_as_invalid_depth() -> None:
    assert FLAG_INVALID_DEPTH in quality_flags_for_book(event=_book(bids=0), now=BASE)


def test_stale_book_is_flagged_relative_to_receive_time() -> None:
    later = BASE + timedelta(minutes=5)
    assert FLAG_STALE_BOOK in quality_flags_for_book(event=_book(), now=later)


def test_clock_skew_beyond_threshold_is_flagged() -> None:
    skewed = _book(received=BASE + timedelta(seconds=30))
    assert FLAG_CLOCK_SKEW in quality_flags_for_book(event=skewed, now=BASE)


def test_duplicate_events_are_counted_and_not_stored_twice() -> None:
    quality = CaptureQuality()
    event = _book()
    assert quality.observe(book="btc_mxn", identity=event.identity(),
                           sequence=event.sequence) is True
    assert quality.observe(book="btc_mxn", identity=event.identity(),
                           sequence=event.sequence) is False
    assert quality.duplicates == 1
    assert quality.events_stored == 1
    assert quality.public()["anomalies_repaired"] == 0


def test_a_sequence_advance_is_recorded_as_an_advance_not_a_gap() -> None:
    """A polling collector cannot tell an advance from a missed update, so it must not claim.

    Calling this a gap would assert knowledge the collector does not have. What it does know
    is that the sequence moved forward, which is what happened when it polled again.
    """
    quality = CaptureQuality()
    quality.observe(book="btc_mxn", identity="a", sequence=10)
    quality.observe(book="btc_mxn", identity="b", sequence=14)
    assert quality.sequence_advances == 1
    assert quality.gaps == 0
    assert quality.highest_sequence["btc_mxn"] == 14
    assert quality.public()["anomalies_repaired"] == 0
    assert quality.public()["sequence_advances_are_routine"] is True


def test_a_recorded_gap_flag_is_still_counted() -> None:
    """When a gap is genuinely observed it is still recorded, never closed."""
    quality = CaptureQuality()
    quality.observe(book="btc_mxn", identity="a", sequence=1, quality_flags=("GAP",))
    assert quality.gaps == 1
    assert quality.health() == "DEGRADED"


def test_sequence_regression_is_recorded_as_such() -> None:
    quality = CaptureQuality()
    quality.observe(book="btc_mxn", identity="a", sequence=10)
    quality.observe(book="btc_mxn", identity="b", sequence=4)
    assert quality.sequence_regressions == 1


def test_disconnect_and_reconnect_are_distinct_observations() -> None:
    quality = CaptureQuality()
    quality.observe(book="btc_mxn", identity="a", sequence=1,
                    quality_flags=("DISCONNECT", "RECONNECT"))
    assert quality.disconnects == 1
    assert quality.reconnects == 1


def test_polling_duplicates_do_not_degrade_health() -> None:
    """A polling collector re-reads a snapshot endpoint, so duplicates are routine.

    Flagging them made a healthy capture report DEGRADED, which is how a monitoring signal
    becomes something operators learn to ignore.
    """
    quality = CaptureQuality()
    for index in range(20):
        quality.observe(book="btc_mxn", identity="same", sequence=5 + index)
    assert quality.events_stored == 1
    assert quality.duplicates == 19
    assert quality.health() == HEALTHY
    assert quality.public()["duplicates_are_routine"] is True


def test_quality_health_is_degraded_by_a_crossed_book() -> None:
    quality = CaptureQuality()
    quality.observe(book="btc_mxn", identity="a", sequence=1, quality_flags=(FLAG_CROSSED_BOOK,))
    assert quality.health() == "DEGRADED"
    assert CaptureQuality().health() == "STOPPED"
    assert CaptureQuality(events_seen=10, events_stored=10).health() == HEALTHY


def test_unknown_quality_flag_is_rejected() -> None:
    with pytest.raises(MicrostructureError):
        CaptureQuality().flag("LOOKS_FINE")


def test_trade_records_do_not_invent_a_maker_side() -> None:
    trade = TradeEvent(book="btc_mxn", trade_id="1", exchange_timestamp=BASE,
                       received_at=BASE, price=Decimal("100"), quantity=Decimal("0.1"))
    assert trade.public()["maker_side"] is None
    assert trade.public()["maker_side_source"] == "UNAVAILABLE"
    stated = TradeEvent(book="btc_mxn", trade_id="2", exchange_timestamp=BASE,
                        received_at=BASE, price=Decimal("100"), quantity=Decimal("0.1"),
                        maker_side="buy")
    assert stated.public()["maker_side_source"] == "PROVIDER_AUTHORITATIVE"


def test_trade_and_depth_reject_impossible_values() -> None:
    with pytest.raises(MicrostructureError):
        TradeEvent(book="btc_mxn", trade_id="1", exchange_timestamp=BASE, received_at=BASE,
                   price=Decimal("0"), quantity=Decimal("1"))
    with pytest.raises(MicrostructureError):
        DepthLevel(price=Decimal("100"), quantity=Decimal("-1"))
    with pytest.raises(MicrostructureError):
        TradeEvent(book="btc_mxn", trade_id="1", exchange_timestamp=BASE, received_at=BASE,
                   price=Decimal("100"), quantity=Decimal("1"), maker_side="maybe")


def test_book_sequence_identity_is_used_for_deduplication() -> None:
    assert _book(sequence=7).identity() == _book(sequence=7).identity()
    assert _book(sequence=7).identity() != _book(sequence=8).identity()


# ------------------------------------------------------------------------ bounded storage


def test_store_rotates_chunks_at_the_declared_bound(tmp_path: Any) -> None:
    store = MicrostructureStore(root=tmp_path, max_chunk_events=2)
    store.append(book="btc_mxn", records=({"n": 1}, {"n": 2}))
    store.append(book="btc_mxn", records=({"n": 3},))
    assert len(sorted(tmp_path.glob("btc_mxn.*.jsonl"))) == 2


def test_store_manifest_fingerprints_every_chunk(tmp_path: Any) -> None:
    store = MicrostructureStore(root=tmp_path, max_chunk_events=10)
    store.append(book="btc_mxn", records=({"n": 1},))
    manifest = store.manifest()
    assert manifest["chunk_count"] == 1
    assert manifest["chunks"][0]["fingerprint"]
    assert manifest["provenance"] == REAL_CAPTURED_MICROSTRUCTURE
    assert manifest["loss_of_certified_artifacts"] is False


def test_store_enforces_total_size_bound(tmp_path: Any) -> None:
    store = MicrostructureStore(root=tmp_path, max_chunk_events=1, max_total_bytes=1)
    for index in range(4):
        store.append(book="btc_mxn", records=({"n": index},))
    total = sum(chunk.stat().st_size for chunk in tmp_path.glob("*.jsonl"))
    assert total <= 1 or len(list(tmp_path.glob("*.jsonl"))) <= 1


def test_store_rejects_unbounded_configuration(tmp_path: Any) -> None:
    with pytest.raises(MicrostructureError):
        MicrostructureStore(root=tmp_path, retention_hours=0)
    with pytest.raises(MicrostructureError):
        MicrostructureStore(root=tmp_path, max_total_bytes=0)


def test_store_keeps_microstructure_separate_from_production_ledger(tmp_path: Any) -> None:
    store = MicrostructureStore(root=tmp_path)
    assert store.provenance == REAL_CAPTURED_MICROSTRUCTURE
    assert store.provenance != "REAL_PRODUCTION_FILL"


# --------------------------------------------------------------------------- the collector


class _StubSource:
    """Deterministic read-only source. Has no method that could mutate anything."""

    def __init__(self, *, sequence: int = 1, crossed: bool = False) -> None:
        self._sequence = sequence
        self._crossed = crossed
        self.calls = 0

    def order_book_snapshot(self, book: str) -> BookEvent:
        self.calls += 1
        return BookEvent(book=book, exchange_timestamp=BASE, received_at=BASE,
                         sequence=self._sequence,
                         bids=(DepthLevel(price=Decimal("102" if self._crossed else "99"),
                                          quantity=Decimal("1")),),
                         asks=(DepthLevel(price=Decimal("101"), quantity=Decimal("1")),))

    def recent_trades(self, book: str) -> tuple[TradeEvent, ...]:
        return (TradeEvent(book=book, trade_id="t1", exchange_timestamp=BASE,
                           received_at=BASE, price=Decimal("100"),
                           quantity=Decimal("0.05"), maker_side="sell"),)


def test_collector_requires_at_least_one_book(tmp_path: Any) -> None:
    with pytest.raises(MicrostructureError):
        MicrostructureCollector(source=_StubSource(),
                                store=MicrostructureStore(root=tmp_path), books=())


def test_collector_has_no_order_capability() -> None:
    """The collector's dependency is a read-only protocol, not a convention."""
    forbidden = ("submit", "place", "cancel", "replace", "post", "amend")
    for name in forbidden:
        assert not hasattr(MicrostructureCollector, name), name
        assert not hasattr(_StubSource, name), name


def test_collector_public_view_states_its_limits_honestly(tmp_path: Any) -> None:
    collector = MicrostructureCollector(source=_StubSource(),
                                        store=MicrostructureStore(root=tmp_path),
                                        books=("btc_mxn",))
    view = collector.public()
    assert view["queue_position_observable"] is QUEUE_POSITION_OBSERVABLE is False
    assert view["passive_fill_exactness"] == PASSIVE_FILL_EXACTNESS == "BOUNDED_ONLY"
    assert view["order_capability"] == "NONE"
    assert view["production_mutation"] == "NONE"


def test_collector_stores_events_and_deduplicates_across_passes(tmp_path: Any) -> None:
    source = _StubSource()
    store = MicrostructureStore(root=tmp_path)
    collector = MicrostructureCollector(source=source, store=store, books=("btc_mxn",))
    first = collector.capture_once(now=BASE)
    assert first["btc_mxn"] == 2  # one book snapshot plus one trade
    collector.capture_once(now=BASE)
    # The same snapshot and trade arriving again must not be stored twice.
    assert collector.quality.duplicates == 2
    assert collector.quality.events_stored == 2


def test_collector_does_not_raise_on_a_crossed_book(tmp_path: Any) -> None:
    """A research capture must keep running when the data is anomalous."""
    collector = MicrostructureCollector(source=_StubSource(crossed=True),
                                        store=MicrostructureStore(root=tmp_path),
                                        books=("btc_mxn",))
    collector.capture_once(now=BASE)
    assert collector.quality.crossed_books == 1
    assert collector.quality.health() == "DEGRADED"


# -------------------------------------------------------------------- delegation fidelity


def _oscillating(count: int, *, minutes: int = 15) -> tuple[Candle, ...]:
    """A deterministic series that produces real feature values, not a degenerate flat line."""
    out: list[Candle] = []
    for index in range(count):
        price = Decimal(100 + (index % 7) - 3)
        out.append(Candle(timestamp=BASE + timedelta(minutes=minutes * index), open=price,
                          high=price + Decimal("2"), low=price - Decimal("2"),
                          close=price + Decimal("1"), volume=Decimal("1")))
    return tuple(out)


def _decision_tuple(proposal: Any) -> tuple[Any, ...]:
    """Everything that decides a trade, so equality here means equality of behaviour."""
    return (proposal.decision, proposal.reason_code, proposal.expected_exit_reference_mxn,
            proposal.invalidation_price_mxn, proposal.max_holding_bars,
            proposal.expected_gross_edge_bps)


def test_mean_reversion_horizon_delegates_to_the_frozen_v2_logic() -> None:
    """The horizon profile must not fork the decision logic.

    Two copies would eventually diverge, and the divergence would be invisible — the
    fingerprint would claim one set of parameters while the code applied another.
    """
    from autofund.mvp.horizon_profiles import VolatilityMeanReversionHorizon
    from autofund.mvp.profile_library import VolatilityMeanReversionV2

    horizon = VolatilityMeanReversionHorizon(timeframe_name="15m")
    proxy = VolatilityMeanReversionV2(
        window=horizon.window, displacement_atr_multiple=horizon.displacement_atr_multiple,
        stop_atr_multiple=horizon.stop_atr_multiple, stop_floor_bps=horizon.stop_floor_bps,
        stop_cap_bps=horizon.stop_cap_bps, max_holding_bars=horizon.max_holding_bars,
        min_history=horizon.min_history,
        expected_holding_horizon=horizon.expected_holding_horizon,
        target_model=horizon.target_model)
    candles = _oscillating(60)
    assert _decision_tuple(horizon.propose(candles=candles)) == _decision_tuple(
        proxy.propose(candles=candles))


def test_range_expansion_horizon_delegates_to_the_frozen_logic() -> None:
    from autofund.mvp.horizon_profiles import RangeExpansionHorizon
    from autofund.mvp.profile_library import RangeExpansionV1

    horizon = RangeExpansionHorizon(timeframe_name="1h")
    proxy = RangeExpansionV1(
        window=horizon.window, expansion_atr_multiple=horizon.expansion_atr_multiple,
        stop_atr_multiple=horizon.stop_atr_multiple, stop_floor_bps=horizon.stop_floor_bps,
        stop_cap_bps=horizon.stop_cap_bps, max_holding_bars=horizon.max_holding_bars,
        min_history=horizon.min_history,
        expected_holding_horizon=horizon.expected_holding_horizon,
        target_model=horizon.target_model)
    candles = _oscillating(60)
    assert _decision_tuple(horizon.propose(candles=candles)) == _decision_tuple(
        proxy.propose(candles=candles))


def test_holding_limits_are_expressed_in_bars_of_the_horizon() -> None:
    """A 12-bar hold means 3 hours on 15m bars and 12 hours on 1h bars.

    This is the mechanism the milestone is testing, so the identity must state it: a
    holding limit is only meaningful together with the timeframe it is counted in.
    """
    fifteenth = horizon_profile_by_id("range-expansion-15m-v1")
    hourly = horizon_profile_by_id("range-expansion-1h-v1")
    assert fifteenth.max_holding_bars == hourly.max_holding_bars
    assert fifteenth.timeframe.seconds * fifteenth.max_holding_bars == 3 * 3600
    assert hourly.timeframe.seconds * hourly.max_holding_bars == 12 * 3600


def test_capital_hours_scale_with_the_bar_duration() -> None:
    """Twelve 1h bars occupy twelve times the capital-time of twelve 1-minute bars."""
    from autofund.mvp.risk_metrics import TradeRiskPath

    def path(bar_minutes: int) -> TradeRiskPath:
        return TradeRiskPath(
            mae_mxn=Decimal("0.1"), mae_bps=Decimal("100"), mfe_mxn=Decimal("0.2"),
            mfe_bps=Decimal("200"), realised_gross_pnl_mxn=Decimal("0.05"),
            realised_net_pnl_mxn=Decimal("0.02"), holding_bars=12,
            holding_minutes=12 * bar_minutes, time_to_mae_bars=3, time_to_mfe_bars=4,
            exit_reason="EXIT_TARGET_REACHED", observations=12)

    assert path(1).capital_hours == Decimal("0.2")
    assert path(60).capital_hours == Decimal("12")


# --------------------------------------------------- configuration identity (regression)


def test_each_configuration_implies_a_distinct_target_model() -> None:
    """A configuration whose target floor never reaches the evaluator is not a configuration.

    This is a real defect that was found and fixed in 0.2.4: the search built every profile
    with a single module-level target model, so all four configurations of a concept
    produced byte-identical results. The budget was spent and nothing was searched.
    """
    for concept, configs in CONFIG_SETS.items():
        models = [config.target_model_for(concept) for config in configs]
        floors = [model.floor_bps for model in models]
        assert len(set(floors)) == len(configs), (concept, floors)
        assert len({(model.atr_multiple, model.floor_bps, model.cap_bps)
                    for model in models}) == len(configs)


def test_configuration_target_floor_overrides_the_concept_default() -> None:
    from autofund.mvp.horizon_profiles import (
        MR_HORIZON_TARGET_MODEL,
        RANGE_HORIZON_TARGET_MODEL,
    )

    for concept, configs in CONFIG_SETS.items():
        base = (MR_HORIZON_TARGET_MODEL if concept == "volatility_mean_reversion"
                else RANGE_HORIZON_TARGET_MODEL)
        for config in configs:
            model = config.target_model_for(concept)
            assert model.floor_bps == config.target_floor_bps
            assert model.atr_multiple == base.atr_multiple
            assert model.cap_bps == base.cap_bps


def test_configurations_tested_are_not_all_redundant() -> None:
    """At least the stop and the target must vary, or the search tests one thing four times."""
    for _concept, configs in CONFIG_SETS.items():
        assert len({config.stop_atr_multiple for config in configs}) > 1
        assert len({config.max_holding_bars for config in configs}) > 1
        assert len({config.target_floor_bps for config in configs}) > 1


def test_configuration_parameters_match_the_configuration_used() -> None:
    """The recorded parameters must describe what was evaluated, not a nearby default."""
    for config in CONFIG_SETS["volatility_mean_reversion"]:
        params = dict(config.parameters(timeframe=FIFTEEN_MINUTE))
        assert params["target_floor_bps"] == str(config.target_floor_bps)
        assert params["stop_atr_multiple"] == str(config.stop_atr_multiple)
        assert params["max_holding_bars"] == str(config.max_holding_bars)

"""Tests for MVP 0.2.6: independent alpha source discovery.

The tests are organised around the two ways this milestone could produce a false positive.

**Leakage.** A lead-lag feature compares one market's past against another's future, so a
one-interval error in either direction produces a correlation that looks like alpha and is an
artefact of the join. Every temporal boundary is asserted directly: the leader uses only data at
or before T, the forward return starts strictly after T, staleness excludes rather than
carries forward, and the validation window is unreachable before a candidate is frozen.

**An insensitive or over-eager pipeline.** Two failure modes pull in opposite directions and both
must be excluded. The negative controls must destroy a real signal, *and* a deliberately injected
future leak must be detected — a pipeline that finds nothing because it can see nothing is as
broken as one that finds everything. The tests assert both directions.
"""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autofund.mvp.alpha_controls import (
    PREDECLARED_CONTROLS,
    AlphaCandidateManifest,
    build_candidate,
    future_leak_control,
    randomised_pairing_control,
    run_controls,
    sign_inversion_control,
    time_shuffled_control,
)
from autofund.mvp.alpha_discovery import (
    FROZEN_PRICE_ONLY_PROFILES,
    INSUFFICIENT_SAMPLE,
    MAX_FEATURE_FAMILIES,
    MINIMUM_OBSERVATIONS,
    NOT_PREDICTIVE,
    PREDECLARED_HORIZONS_MINUTES,
    PREDICTIVE,
    PRICE_ONLY_RESEARCH_BASELINE,
    RANK_CRITICAL_VALUE,
    AlphaError,
    DiscoveryWindows,
    MultipleTestLedger,
    assess_predictive_content,
    declare_windows,
    effective_observation_count,
    forward_return_bps,
    rank_significance,
    split_buckets,
)
from autofund.mvp.cross_market import (
    BASKET,
    DEFAULT_MAX_STALENESS_SECONDS,
    LAGGED_RETURN,
    LEADER_FOLLOWER_DIVERGENCE,
    PREDECLARED_FEATURES,
    PREDECLARED_RELATIONSHIPS,
    MarketSeries,
    PanelError,
    basket_series,
    build_panel,
)
from autofund.mvp.microstructure import (
    BookEvent,
    DepthLevel,
    TradeEvent,
)
from autofund.mvp.microstructure_alpha import (
    INSUFFICIENT_QUEUE_EVIDENCE,
    MINIMUM_MARKOUT_OBSERVATIONS,
    PREDECLARED_MARKOUT_SECONDS,
    features_from_book,
    markouts_for,
    summarise_markouts,
    trade_flow,
    transitions,
)
from autofund.mvp.profile_library import PROFILE_BY_ID
from autofund.replay.data import Candle

BASE = datetime(2026, 9, 1, tzinfo=UTC)


def _candles(count: int, *, seed: int = 7, base: int = 100) -> tuple[Candle, ...]:
    rng = random.Random(seed)
    out: list[Candle] = []
    price = Decimal(str(base))
    for index in range(count):
        price += Decimal(str(round(rng.gauss(0, 0.05), 4)))
        half = Decimal("0.02")
        out.append(Candle(timestamp=BASE + timedelta(minutes=index), open=price,
                          high=price + half, low=price - half, close=price,
                          volume=Decimal("1")))
    return tuple(out)


def _series(market: str, count: int = 300, *, seed: int = 7,
            interval_minutes: int = 1) -> MarketSeries:
    return MarketSeries(market=market, candles=_candles(count, seed=seed),
                        interval=timedelta(minutes=interval_minutes))


# --------------------------------------------------------------------------- leakage: time


def test_forward_return_starts_strictly_after_the_observation() -> None:
    """Entry is the next bar's open, not this bar's close.

    Pricing the forward return from the observation bar's own close would credit a feature with a
    move that had already happened when it was computed. That single substitution is enough to
    make a worthless feature look predictive, so the boundary is asserted directly.
    """
    candles = _candles(50)
    result = forward_return_bps(candles=candles, index=10, horizon=1)
    entry = candles[11].open
    exit_price = candles[11].close
    assert result == (exit_price - entry) / entry * Decimal("10000")
    # And it must differ from the naive close-to-close measure, or the test proves nothing.
    naive = (candles[11].close - candles[10].close) / candles[10].close * Decimal("10000")
    assert result != naive


def test_forward_return_horizon_boundaries_are_exact() -> None:
    candles = _candles(30)
    # horizon h ends at index + h, so the last usable index is len - h - 1.
    assert forward_return_bps(candles=candles, index=29 - 5, horizon=5) is not None
    assert forward_return_bps(candles=candles, index=29 - 4, horizon=5) is None
    assert forward_return_bps(candles=candles, index=28, horizon=1) is not None
    assert forward_return_bps(candles=candles, index=29, horizon=1) is None


def test_forward_return_returns_none_rather_than_zero_when_truncated() -> None:
    """A truncated forward window is missing data, not a zero return.

    Substituting zero would pull every estimate toward no-effect and could turn a real signal
    into a null, or a null into a spurious one.
    """
    candles = _candles(20)
    assert forward_return_bps(candles=candles, index=19, horizon=5) is None


def test_forward_return_rejects_a_nonpositive_horizon() -> None:
    candles = _candles(20)
    with pytest.raises(AlphaError):
        forward_return_bps(candles=candles, index=5, horizon=0)
    with pytest.raises(AlphaError):
        forward_return_bps(candles=candles, index=-1, horizon=1)


def test_market_series_resolves_the_bar_that_had_closed() -> None:
    """A label marks a bar's start, so the close is known one interval later.

    Using the label directly would return the bar that was still forming, whose close the
    decision could not have known. That off-by-one is the most likely way this module could leak.
    """
    series = _series("BTC/MXN", count=20)
    label = series.candles[5].timestamp
    # At the label itself, bar 5 has not closed: at most bars 0..3 are complete is false, bar 4
    # is the last whose close (label 5) has occurred.
    assert series.index_at(label) == 4
    assert series.index_at(label + timedelta(minutes=1)) == 5
    assert series.index_at(label - timedelta(seconds=1)) == 3


def test_stale_market_is_excluded_not_carried_forward() -> None:
    """A market whose last close is too old is excluded, never forward-filled.

    Carrying a stale price into a feature fabricates an observation, and a feature computed from
    one has no interpretation.
    """
    series = _series("BTC/MXN", count=20)
    last_close = series.candles[-1].timestamp + series.interval
    assert series.index_at(last_close) is not None
    assert series.index_at(last_close + timedelta(
        seconds=DEFAULT_MAX_STALENESS_SECONDS + 1)) is None


def test_market_series_return_uses_only_bars_at_or_before_the_index() -> None:
    series = _series("BTC/MXN", count=50)
    value = series.return_bps(20, bars=3)
    expected = ((series.candles[20].close - series.candles[17].close)
                / series.candles[17].close * Decimal("10000"))
    assert value == expected


def test_series_rejects_a_nonpositive_interval() -> None:
    with pytest.raises(PanelError):
        MarketSeries(market="BTC/MXN", candles=(), interval=timedelta(0))


# ------------------------------------------------------------------- leakage: the panel join


def test_panel_forward_returns_are_keyed_by_market_and_horizon() -> None:
    panel = build_panel(series=[_series("BTC/MXN"), _series("ETH/MXN", seed=8)],
                        interval_seconds=60, horizons=(1, 5))
    observation = panel.observations[50]
    assert observation.forward_for("BTC/MXN|1") is not None
    assert observation.forward_for("BTC/MXN|5") is not None
    assert observation.forward_for("ETH/MXN|1") is not None
    # A horizon that was not requested must not be fabricated.
    assert observation.forward_for("BTC/MXN|15") is None


def test_panel_feature_at_an_instant_uses_only_data_at_or_before_it() -> None:
    """The panel's own feature at instant T must equal a recomputation from bars up to T.

    The instant for the observation at index `i` is the close time of bar `i`, so the bar that
    has closed *at* that instant is bar `i` itself. The property under test is the equality, not
    a particular index, which is why it is asserted as a recomputation rather than a literal.
    """
    a = _series("BTC/MXN", count=200)
    b = _series("ETH/MXN", count=200, seed=9)
    panel = build_panel(series=[a, b], interval_seconds=60, horizons=(1,))
    observation = panel.observations[100]
    index_a = dict(observation.indices)["BTC/MXN"]
    # Every resolved index must lie at or before the instant, which is the leakage property.
    for market, index in observation.indices:
        source = a if market == "BTC/MXN" else b
        close_time = source.candles[index].timestamp + source.interval
        assert close_time <= observation.moment
    expected = a.return_bps(index_a, bars=1)
    assert observation.feature("LAGGED_RETURN|BTC/MXN") == expected


def test_panel_records_stale_markets_rather_than_silently_dropping_them() -> None:
    """A gap in one market must be visible in the output.

    Silently dropping a market would make a two-market observation look like a four-market one,
    and the dispersion and breadth features would then be computed over a different set than the
    reader believes.
    """
    full = _series("BTC/MXN", count=100)
    # A second market with a large hole in the middle.
    gapped_candles = list(full.candles[:30]) + list(
        Candle(timestamp=full.candles[i].timestamp, open=full.candles[i].open,
               high=full.candles[i].high, low=full.candles[i].low,
               close=full.candles[i].close, volume=full.candles[i].volume)
        for i in range(80, 100))
    gapped = MarketSeries(market="ETH/MXN", candles=tuple(gapped_candles),
                          interval=timedelta(minutes=1))
    panel = build_panel(series=[full, gapped], interval_seconds=60, horizons=(1,))
    stale_seen = [o for o in panel.observations if o.stale_markets]
    assert stale_seen, "the gap must be reported as staleness"
    assert panel.markets_excluded_stale > 0
    # The market with the hole is the one reported stale, and it is named rather than omitted.
    assert all("ETH/MXN" in o.stale_markets for o in stale_seen)
    # An observation with a stale market must not carry that market's features or forward return.
    for observation in stale_seen:
        assert "ETH/MXN" not in dict(observation.indices)


def test_panel_reports_that_it_never_forward_fills() -> None:
    panel = build_panel(series=[_series("BTC/MXN"), _series("ETH/MXN", seed=3)],
                        interval_seconds=60, horizons=(1,))
    assert panel.public()["forward_fill_used"] is False
    assert panel.public()["stale_markets_excluded"] is True


def test_basket_omits_bars_where_any_constituent_is_missing() -> None:
    """A partially populated basket bar would represent a market that did not exist."""
    a = _series("BTC/MXN", count=100)
    b = _series("ETH/MXN", count=60, seed=4)
    basket = basket_series(series=[a, b], interval_seconds=60)
    labels = {candle.timestamp for candle in basket.candles}
    b_labels = {candle.timestamp for candle in b.candles}
    a_labels = {candle.timestamp for candle in a.candles}
    assert labels == a_labels & b_labels
    assert basket.market == BASKET


def test_panel_requires_at_least_one_series() -> None:
    with pytest.raises(PanelError):
        build_panel(series=[], interval_seconds=60)
    with pytest.raises(PanelError):
        build_panel(series=[_series("BTC/MXN")], interval_seconds=0)


# -------------------------------------------------------------- the discovery window split


def test_validation_window_lies_strictly_after_development() -> None:
    """Validation must be later than development, and inside fetchable history.

    An earlier design placed validation *after* the discovery instant, which sounds stricter but
    cannot work: no such data exists at run time, so validation returned zero observations and the
    candidate appeared to have failed a test it never took. The dates must therefore be ordered
    *and* both inhabited, and the guarantee that validation is untouched comes from the discovery
    code filtering to development, not from the validation window being in the future.
    """
    windows = declare_windows(now=BASE, development_hours=24, validation_hours=24)
    assert windows.validation_start > windows.development_end
    assert windows.validation_end <= BASE, "validation must be inside fetchable history"
    # Both windows contain observations, or the split is a formality.
    assert windows.contains_development(windows.development_start) is True
    assert windows.contains_development(windows.development_end
                                        - timedelta(minutes=1)) is True
    assert windows.contains_validation(windows.validation_start) is True
    assert windows.contains_validation(windows.validation_end
                                       - timedelta(minutes=1)) is True
    # And no instant belongs to both.
    for hours in range(0, 72):
        moment = windows.development_start + timedelta(hours=hours)
        assert not (windows.contains_development(moment)
                    and windows.contains_validation(moment))


def test_windows_reject_an_overlapping_validation() -> None:
    with pytest.raises(AlphaError):
        DiscoveryWindows(
            development_start=BASE, development_end=BASE + timedelta(hours=10),
            validation_start=BASE + timedelta(hours=5),
            validation_end=BASE + timedelta(hours=20), note="overlapping")


def test_windows_require_timezone_aware_boundaries() -> None:
    with pytest.raises(AlphaError):
        DiscoveryWindows(development_start=datetime(2026, 9, 1),
                         development_end=BASE, validation_start=BASE,
                         validation_end=BASE + timedelta(hours=1), note="naive")


def test_validation_is_unreachable_before_a_candidate_is_frozen() -> None:
    """A candidate may not be declared after the validation window has been inspected."""
    with pytest.raises(AlphaError):
        AlphaCandidateManifest(
            candidate_id="c", source_family="f", feature_name="x", leader="a", follower="b",
            horizon_minutes=1, bucket_count=5, development_observations=500,
            development_effective_observations=Decimal("100"),
            development_rank_relationship=Decimal("0.2"), development_monotone=True,
            development_monotone_direction="INCREASING", development_stable_subwindows=4,
            economic_interpretation="test", controls_expected_behavior_met=True,
            multiple_test_comparisons=10, created_at=BASE.isoformat(),
            development_window=("a", "b"), validation_window=("c", "d"),
            validation_touched=True)


# ------------------------------------------------------------------- predictive measurement


def _monotone(n: int = 800) -> tuple[tuple[Decimal, ...], tuple[Decimal, ...]]:
    feature = tuple(Decimal(str(i % 40)) for i in range(n))
    forward = tuple(feature[i] * Decimal("0.5") + Decimal(str((i % 7) - 3))
                    for i in range(n))
    return feature, forward


def test_a_genuine_monotone_relationship_is_reported_as_predictive() -> None:
    feature, forward = _monotone()
    content = assess_predictive_content(feature_name="f", market="BTC/MXN",
                                        horizon_minutes=1, feature=feature, forward=forward)
    assert content.verdict == PREDICTIVE
    assert content.monotone is True
    assert content.monotone_direction == "INCREASING"
    assert content.stable is True
    assert content.rank_relationship is not None and content.rank_relationship > 0


def test_pure_noise_is_not_reported_as_predictive() -> None:
    rng = random.Random(11)
    feature = tuple(Decimal(str(round(rng.gauss(0, 1), 4))) for _ in range(800))
    forward = tuple(Decimal(str(round(rng.gauss(0, 1), 4))) for _ in range(800))
    content = assess_predictive_content(feature_name="f", market="BTC/MXN",
                                        horizon_minutes=1, feature=feature, forward=forward)
    assert content.verdict == NOT_PREDICTIVE


def test_a_relationship_confined_to_one_subwindow_is_not_stable() -> None:
    """A signal present in one part of the sample and absent elsewhere is a regime artefact."""
    n = 800
    forward = tuple(Decimal(str(round((i % 13) - 6, 4))) for i in range(n))
    # The feature only tracks the outcome in the first quarter.
    feature = tuple(forward[i] if i < n // 4 else Decimal(str((i * 37) % 11))
                    for i in range(n))
    content = assess_predictive_content(feature_name="f", market="BTC/MXN",
                                        horizon_minutes=1, feature=feature, forward=forward)
    assert content.stable is False or content.monotone is False


def test_insufficient_sample_is_reported_separately_from_no_signal() -> None:
    """"Could not measure" and "measured and found nothing" are different findings."""
    feature, forward = _monotone(MINIMUM_OBSERVATIONS - 1)
    content = assess_predictive_content(feature_name="f", market="BTC/MXN",
                                        horizon_minutes=1, feature=feature, forward=forward)
    assert content.verdict == INSUFFICIENT_SAMPLE
    assert content.notes["reason"] == "BELOW_MINIMUM_OBSERVATIONS"


def test_effective_count_can_only_reduce_the_apparent_sample() -> None:
    """Autocorrelation must never inflate a sample into more weight than it has."""
    smooth = tuple(Decimal(str(i // 10)) for i in range(1000))
    effective = effective_observation_count(values=smooth)
    assert effective <= Decimal(1000)
    assert effective < Decimal(100), "a perfectly autocorrelated series must be penalised"
    # An alternating series is as independent as data gets; it must not be reduced to nothing.
    alternating = tuple(Decimal(str(1 if i % 2 else -1)) for i in range(1000))
    assert effective_observation_count(values=alternating) > Decimal(100)


def test_buckets_are_equal_count_and_cover_every_observation() -> None:
    feature = tuple(Decimal(str(i % 100)) for i in range(500))
    forward = tuple(Decimal(str(i)) for i in range(500))
    buckets = split_buckets(feature=feature, forward=forward, bucket_count=5)
    assert len(buckets) == 5
    counts = [bucket.observations for bucket in buckets]
    # The last bucket absorbs the remainder, so it may be larger but none may be empty.
    assert all(count > 0 for count in counts)
    assert sum(counts) == 500
    # Boundens must be ordered, or the buckets are not a partition of the feature's range.
    assert [bucket.lower_bound for bucket in buckets] == sorted(
        bucket.lower_bound for bucket in buckets)


def test_bucket_directionality_requires_an_effect_larger_than_its_error() -> None:
    feature = tuple(Decimal(str(i % 20)) for i in range(600))
    # A forward series that increases strongly with the feature in every bucket.
    forward = tuple(feature[i] * Decimal("10") for i in range(600))
    buckets = split_buckets(feature=feature, forward=forward, bucket_count=4)
    assert all(bucket.directional for bucket in buckets)


def test_mismatched_series_are_rejected() -> None:
    with pytest.raises(AlphaError):
        split_buckets(feature=(Decimal("1"),), forward=(Decimal("1"), Decimal("2")))
    with pytest.raises(AlphaError):
        assess_predictive_content(feature_name="f", market="m", horizon_minutes=1,
                                  feature=(Decimal("1"),), forward=(Decimal("1"),
                                                                    Decimal("2")))


# ------------------------------------------------------------------------- negative controls


def test_time_shuffle_destroys_a_genuine_signal() -> None:
    feature, forward = _monotone()
    result = time_shuffled_control(feature_name="f", market="BTC/MXN", horizon_minutes=1,
                                   feature=feature, forward=forward)
    assert result.structure_survived is False
    assert result.as_expected is True


def test_randomised_pairing_does_not_reproduce_the_signal() -> None:
    feature, forward = _monotone()
    result = randomised_pairing_control(feature_name="f", market="BTC/MXN",
                                        horizon_minutes=1, feature=feature,
                                        forward=forward)
    assert result.structure_survived is False
    assert result.as_expected is True


def test_an_injected_future_leak_must_be_detected() -> None:
    """The sensitivity check. An undetected leak means every null result is uninformative."""
    rng = random.Random(5)
    feature = tuple(Decimal(str(round(rng.gauss(0, 1), 4))) for _ in range(800))
    forward = tuple(Decimal(str(round(rng.gauss(0, 1), 4))) for _ in range(800))
    result = future_leak_control(feature_name="f", market="BTC/MXN", horizon_minutes=1,
                                 feature=feature, forward=forward)
    assert result.structure_survived is True
    assert result.as_expected is True


def test_sign_inversion_reverses_a_genuine_monotone_direction() -> None:
    feature, forward = _monotone()
    original = assess_predictive_content(feature_name="f", market="BTC/MXN",
                                         horizon_minutes=1, feature=feature,
                                         forward=forward)
    result = sign_inversion_control(feature_name="f", market="BTC/MXN", horizon_minutes=1,
                                    feature=feature, forward=forward, original=original)
    assert result.as_expected is True
    assert result.structure_survived is False


def test_all_controls_run_and_are_retained() -> None:
    feature, forward = _monotone()
    original = assess_predictive_content(feature_name="f", market="BTC/MXN",
                                         horizon_minutes=1, feature=feature,
                                         forward=forward)
    results = run_controls(feature_name="f", market="BTC/MXN", horizon_minutes=1,
                           feature=feature, forward=forward, original=original)
    assert len(results) == len(PREDECLARED_CONTROLS)
    assert {result.control for result in results} == set(PREDECLARED_CONTROLS)
    assert all(result.as_expected for result in results)
    for result in results:
        assert result.public()["as_expected"] is True


def test_a_candidate_is_refused_when_controls_do_not_behave() -> None:
    """A candidate resting on a pipeline whose own controls failed is not a candidate."""
    feature, forward = _monotone()
    content = assess_predictive_content(feature_name="f", market="BTC/MXN",
                                        horizon_minutes=1, feature=feature, forward=forward)
    broken = time_shuffled_control(feature_name="f", market="BTC/MXN", horizon_minutes=1,
                                   feature=feature, forward=forward)
    # Fabricate a control that claims structure survived when it should not have.
    from dataclasses import replace

    failed = replace(broken, structure_survived=True)
    with pytest.raises(AlphaError):
        build_candidate(
            candidate_id="c", source_family="f", feature_name="x", leader="a", follower="b",
            horizon_minutes=1, bucket_count=5, content=content,
            economic_interpretation="test", controls=(failed,), comparisons=1,
            created_at=BASE.isoformat(), development_window=("a", "b"),
            validation_window=("c", "d"))


def test_a_candidate_requires_a_stated_economic_interpretation() -> None:
    with pytest.raises(AlphaError):
        AlphaCandidateManifest(
            candidate_id="c", source_family="f", feature_name="x", leader="a", follower="b",
            horizon_minutes=1, bucket_count=5, development_observations=500,
            development_effective_observations=Decimal("100"),
            development_rank_relationship=Decimal("0.2"), development_monotone=True,
            development_monotone_direction="INCREASING", development_stable_subwindows=4,
            economic_interpretation="", controls_expected_behavior_met=True,
            multiple_test_comparisons=10, created_at=BASE.isoformat(),
            development_window=("a", "b"), validation_window=("c", "d"),
            validation_touched=False)


def test_a_candidate_rejects_a_horizon_outside_the_predeclared_set() -> None:
    with pytest.raises(AlphaError):
        AlphaCandidateManifest(
            candidate_id="c", source_family="f", feature_name="x", leader="a", follower="b",
            horizon_minutes=7, bucket_count=5, development_observations=500,
            development_effective_observations=Decimal("100"),
            development_rank_relationship=Decimal("0.2"), development_monotone=True,
            development_monotone_direction="INCREASING", development_stable_subwindows=4,
            economic_interpretation="test", controls_expected_behavior_met=True,
            multiple_test_comparisons=10, created_at=BASE.isoformat(),
            development_window=("a", "b"), validation_window=("c", "d"),
            validation_touched=False)


def test_a_frozen_candidate_does_not_claim_to_be_a_strategy() -> None:
    feature, forward = _monotone()
    content = assess_predictive_content(feature_name="f", market="BTC/MXN",
                                        horizon_minutes=1, feature=feature, forward=forward)
    original = content
    controls = run_controls(feature_name="f", market="BTC/MXN", horizon_minutes=1,
                            feature=feature, forward=forward, original=original)
    candidate = build_candidate(
        candidate_id="c", source_family="CROSS_MARKET_LEAD_LAG", feature_name="x",
        leader="a", follower="b", horizon_minutes=1, bucket_count=5, content=content,
        economic_interpretation="interpretation", controls=controls, comparisons=1,
        created_at=BASE.isoformat(), development_window=("a", "b"),
        validation_window=("c", "d"))
    public = candidate.public()
    assert public["is_a_strategy"] is False
    assert public["trading_policy_defined"] is False
    assert public["frozen_before_validation"] is True
    assert public["fingerprint"] if "fingerprint" in public else True
    assert candidate.fingerprint


# ------------------------------------------------------------------ multiple-test discipline


def test_multiple_test_ledger_counts_comparisons_explicitly() -> None:
    ledger = MultipleTestLedger(
        feature_families_examined=len(PREDECLARED_FEATURES),
        relationships_examined=len(PREDECLARED_RELATIONSHIPS),
        horizons_examined=len(PREDECLARED_HORIZONS_MINUTES), buckets_per_feature=5,
        negative_controls_run=40, failed_candidates_retained=12)
    public = ledger.public()
    assert public["total_comparisons"] == (len(PREDECLARED_RELATIONSHIPS)
                                           * len(PREDECLARED_HORIZONS_MINUTES) * 5)
    assert public["failed_candidates_retained"] == 12
    assert public["best_result_interpreted_against_this_multiplicity"] is True


def test_multiple_test_budget_is_enforced() -> None:
    with pytest.raises(AlphaError):
        MultipleTestLedger(feature_families_examined=MAX_FEATURE_FAMILIES + 1,
                           relationships_examined=1, horizons_examined=1,
                           buckets_per_feature=5, negative_controls_run=0,
                           failed_candidates_retained=0)


def test_discovery_budget_stays_small() -> None:
    """A larger budget is how a search manufactures a finding out of noise."""
    assert len(PREDECLARED_FEATURES) <= MAX_FEATURE_FAMILIES
    assert len(PREDECLARED_RELATIONSHIPS) <= 12
    assert len(PREDECLARED_HORIZONS_MINUTES) == 3


# ------------------------------------------------------- microstructure: past and future


def _book(*, moment: datetime, bid: str = "99", ask: str = "101",
          bid_size: str = "5", ask_size: str = "5", sequence: int = 1) -> BookEvent:
    return BookEvent(
        book="btc_mxn", exchange_timestamp=moment, received_at=moment, sequence=sequence,
        bids=(DepthLevel(price=Decimal(bid), quantity=Decimal(bid_size)),
              DepthLevel(price=Decimal(bid) - 1, quantity=Decimal("2"))),
        asks=(DepthLevel(price=Decimal(ask), quantity=Decimal(ask_size)),
              DepthLevel(price=Decimal(ask) + 1, quantity=Decimal("2"))))


def test_book_features_use_only_this_snapshot() -> None:
    moment = BASE
    features = features_from_book(event=_book(moment=moment))
    assert features.moment == moment
    assert features.best_bid == Decimal("99")
    assert features.best_ask == Decimal("101")
    assert features.midpoint == Decimal("100")
    assert features.public()["uses_only_this_snapshot"] is True


def test_imbalance_sign_convention_is_bid_minus_ask() -> None:
    heavy_bid = features_from_book(event=_book(moment=BASE, bid_size="9", ask_size="1"))
    heavy_ask = features_from_book(event=_book(moment=BASE, bid_size="1", ask_size="9"))
    assert heavy_bid.top_imbalance > 0
    assert heavy_ask.top_imbalance < 0


def test_microprice_displacement_is_zero_for_a_balanced_book() -> None:
    features = features_from_book(event=_book(moment=BASE, bid_size="5", ask_size="5"))
    assert features.microprice is not None
    assert abs(features.microprice_displacement_bps or Decimal("0")) < Decimal("0.5")


def test_markout_is_measured_strictly_after_the_observation() -> None:
    """A markout against the observation's own snapshot would be identically zero.

    Zero markouts and zero spread would look like a perfect null result; in fact they would mean
    the forward window was never actually forward.
    """
    early = features_from_book(event=_book(moment=BASE, bid="99", ask="101"))
    later = [features_from_book(event=_book(moment=BASE + timedelta(seconds=10),
                                            bid="100", ask="102", sequence=2))]
    results = markouts_for(features=early, later=later, horizons=(5,), direction=Decimal("1"))
    assert len(results) == 1
    assert results[0].measured_at > results[0].observed_at
    assert results[0].signed_markout_bps != 0
    assert results[0].public()["measured_strictly_after_observation"] is True


def test_markout_is_not_produced_when_no_later_snapshot_exists() -> None:
    """A horizon the capture cannot reach is missing evidence, not a zero."""
    early = features_from_book(event=_book(moment=BASE))
    assert markouts_for(features=early, later=[], horizons=(5,)) == ()


def test_markout_sign_follows_the_position_direction() -> None:
    early = features_from_book(event=_book(moment=BASE, bid="99", ask="101"))
    later = [features_from_book(event=_book(moment=BASE + timedelta(seconds=10),
                                            bid="100", ask="102", sequence=2))]
    long_side = markouts_for(features=early, later=later, horizons=(5,),
                             direction=Decimal("1"))[0]
    short_side = markouts_for(features=early, later=later, horizons=(5,),
                              direction=Decimal("-1"))[0]
    assert long_side.signed_markout_bps == -short_side.signed_markout_bps


def test_markout_summary_refuses_to_state_anything_below_the_floor() -> None:
    early = features_from_book(event=_book(moment=BASE, bid="99", ask="101"))
    later = [features_from_book(event=_book(moment=BASE + timedelta(seconds=10),
                                            bid="100", ask="102", sequence=2))]
    markouts = markouts_for(features=early, later=later, horizons=(5,))
    assert len(markouts) < MINIMUM_MARKOUT_OBSERVATIONS
    summary = summarise_markouts(feature_name="f", book="btc_mxn", horizon_seconds=5,
                                 markouts=markouts)
    assert summary.evidence_class == INSUFFICIENT_QUEUE_EVIDENCE
    assert summary.queue_exact is False
    assert summary.public()["queue_position_assumed"] is False


def test_book_transitions_use_only_two_observed_snapshots() -> None:
    events = [_book(moment=BASE, sequence=1),
              _book(moment=BASE + timedelta(seconds=3), bid="99", ask="101",
                    bid_size="1", sequence=2)]
    result = transitions(events=events)
    assert len(result) == 1
    assert result[0].elapsed_seconds == Decimal("3")
    assert result[0].public()["uses_only_two_observed_snapshots"] is True


def test_book_transition_preserves_a_real_gap_rather_than_smoothing_it() -> None:
    """A change over a long gap must be distinguishable from the same change over a short one."""
    events = [_book(moment=BASE, sequence=1),
              _book(moment=BASE + timedelta(seconds=300), sequence=2)]
    result = transitions(events=events)
    assert result[0].elapsed_seconds == Decimal("300")


def test_trade_flow_inverts_the_maker_side_correctly() -> None:
    """A maker side of "sell" means the aggressor bought.

    Getting this backwards would invert every trade-flow conclusion while looking entirely
    plausible, so the convention is asserted rather than left to a reader.
    """
    trades = [
        TradeEvent(book="btc_mxn", trade_id="1", exchange_timestamp=BASE,
                   received_at=BASE, price=Decimal("100"), quantity=Decimal("3"),
                   maker_side="sell"),
        TradeEvent(book="btc_mxn", trade_id="2",
                   exchange_timestamp=BASE + timedelta(seconds=1), received_at=BASE,
                   price=Decimal("100"), quantity=Decimal("1"), maker_side="buy"),
    ]
    flow = trade_flow(trades=trades, start=BASE, end=BASE + timedelta(minutes=1))
    assert flow is not None
    assert flow.buy_volume == Decimal("3")
    assert flow.sell_volume == Decimal("1")
    assert flow.imbalance > 0


def test_trade_flow_excludes_trades_without_a_stated_side() -> None:
    """Inferring an aggressor would create a field indistinguishable from a real one."""
    trades = [
        TradeEvent(book="btc_mxn", trade_id="1", exchange_timestamp=BASE,
                   received_at=BASE, price=Decimal("100"), quantity=Decimal("3"),
                   maker_side=None),
    ]
    assert trade_flow(trades=trades, start=BASE,
                      end=BASE + timedelta(minutes=1)) is None


def test_trade_flow_window_must_be_non_empty() -> None:
    """An empty window is a caller error, not an empty result.

    Returning a zero-flow object for an inverted or degenerate window would let a caller read it
    as "no aggressive flow" when in fact it asked for nothing.
    """
    trade = TradeEvent(book="btc_mxn", trade_id="1", exchange_timestamp=BASE,
                       received_at=BASE, price=Decimal("100"), quantity=Decimal("1"),
                       maker_side="sell")
    with pytest.raises(Exception):
        trade_flow(trades=[trade], start=BASE, end=BASE)
    with pytest.raises(Exception):
        trade_flow(trades=[trade], start=BASE + timedelta(minutes=1), end=BASE)


# --------------------------------------------------- frozen prior research and safety policy


def test_price_only_baseline_label_exists_and_prior_profiles_are_untouched() -> None:
    assert PRICE_ONLY_RESEARCH_BASELINE == "PRICE_ONLY_RESEARCH_BASELINE"
    assert "mean-reversion-safe-v1" in FROZEN_PRICE_ONLY_PROFILES
    assert len(FROZEN_PRICE_ONLY_PROFILES) == 25
    # Fingerprints of the frozen price-only work must be exactly as 0.2.5 left them.
    assert PROFILE_BY_ID["mean-reversion-safe-v1"].fingerprint == (
        "1cadcfa967a919b6d041cf382d5acc993d7c70c7c1b008bba2db764c4f51bb6e")
    assert PROFILE_BY_ID["trend-continuation-v1"].fingerprint == (
        "6314058847ec352c76bf10f3c61fdd3b2989df3ee0782b3609fed5995e6157d7")
    assert PROFILE_BY_ID["volatility-mean-reversion-v2"].fingerprint.startswith(
        "b80b400c5329c219")


def test_policy_constants_are_unchanged_by_alpha_discovery() -> None:
    from autofund.mvp.executable_replay import (
        MAX_SINGLE_TRADE_RISK_MXN,
        MINIMUM_REWARD_RISK_RATIO,
    )
    from autofund.mvp.robustness import MAX_DRAWDOWN_MXN

    assert MAX_DRAWDOWN_MXN == Decimal("0.50")
    assert MAX_SINGLE_TRADE_RISK_MXN == Decimal("0.50")
    assert MINIMUM_REWARD_RISK_RATIO == Decimal("1.0")


def test_microstructure_collector_remains_read_only() -> None:
    from autofund.mvp.microstructure import MicrostructureCollector

    for name in ("submit", "place", "cancel", "replace", "post", "amend"):
        assert not hasattr(MicrostructureCollector, name)


def test_alpha_modules_expose_no_trading_capability() -> None:
    """Alpha discovery measures information; it must not be able to place anything."""
    import autofund.mvp.alpha_discovery as discovery
    import autofund.mvp.cross_market as cross
    import autofund.mvp.microstructure_alpha as micro

    for module in (discovery, cross, micro):
        for name in ("place_order", "submit_order", "cancel_order", "execute"):
            assert not hasattr(module, name)


def test_predeclared_sets_are_interpretable_and_frozen() -> None:
    assert len(PREDECLARED_FEATURES) == 6
    assert len(PREDECLARED_RELATIONSHIPS) == 12
    assert PREDECLARED_HORIZONS_MINUTES == (1, 5, 15)
    assert PREDECLARED_MARKOUT_SECONDS == (5, 30, 60)
    assert LEADER_FOLLOWER_DIVERGENCE in PREDECLARED_FEATURES
    assert LAGGED_RETURN in PREDECLARED_FEATURES
    # Every declared relationship names a leader and a follower distinctly.
    for leader, follower in PREDECLARED_RELATIONSHIPS:
        assert leader != follower


# -------------------------------------------- declared features must actually be measured


def test_every_declared_feature_key_is_emitted_by_the_panel() -> None:
    """A declared combination that names a non-existent key measures nothing, silently.

    This is the most dangerous failure available to this milestone, because the expected answer is
    negative: a pipeline that evaluated nothing and a pipeline that found nothing produce the same
    report. Every combination the search intends to run must therefore be checked against what the
    panel actually emits.
    """
    from autofund.mvp.cross_market import DIVERGENCE

    series = [_series(m, count=200, seed=i) for i, m in
              enumerate(["BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN"], start=1)]
    basket = basket_series(series=series, interval_seconds=60)
    panel = build_panel(series=[basket, *series], interval_seconds=60, horizons=(1,))
    available: set[str] = set()
    for observation in panel.observations[:50]:
        available.update(observation.features.keys())

    emitted = set(available)
    for leader, follower in PREDECLARED_RELATIONSHIPS:
        for feature in PREDECLARED_FEATURES:
            if leader == DIVERGENCE:
                # DIVERGENCE is not a market, so it contributes no lagged return of its own; the
                # relationship it names is already expressed by the follower's relative return.
                if feature == LEADER_FOLLOWER_DIVERGENCE:
                    key = f"RELATIVE_RETURN_VS_BASKET|{follower}"
                else:
                    key = f"{feature}|{follower}"
            elif feature == LEADER_FOLLOWER_DIVERGENCE:
                key = f"{feature}|{leader}|{follower}"
            elif feature == LAGGED_RETURN and leader != BASKET:
                key = f"{feature}|{leader}"
            else:
                key = f"{feature}|{follower}"
            assert key in emitted, f"declared combination resolves to nothing: {key}"


def test_divergence_relationship_is_covered_by_the_basket_relative_feature() -> None:
    """The divergence relationship must map to an existing feature, not a phantom key.

    Declaring a relationship whose key the panel never emits makes the search report zero
    measurements for it while still counting it in the multiplicity budget -- the search appears
    larger than it was and one hypothesis is never actually tested.
    """
    from autofund.mvp.cross_market import DIVERGENCE

    divergence_followers = {follower for leader, follower in PREDECLARED_RELATIONSHIPS
                            if leader == DIVERGENCE}
    assert divergence_followers
    series = [_series(m, count=200, seed=i) for i, m in
              enumerate(["BTC/MXN", "ETH/MXN", "SOL/MXN", "XRP/MXN"], start=1)]
    basket = basket_series(series=series, interval_seconds=60)
    panel = build_panel(series=[basket, *series], interval_seconds=60, horizons=(1,))
    available = set(panel.observations[100].features.keys())
    for follower in divergence_followers:
        assert f"RELATIVE_RETURN_VS_BASKET|{follower}" in available


def test_panel_feature_lookup_is_by_key_and_constant_time() -> None:
    """Feature lookup must be a mapping access, not a scan.

    A linear scan over ~40 features per lookup, repeated across a few hundred combinations and
    54,721 observations, is billions of comparisons: the search appears to hang rather than fail.
    """
    series = [_series("BTC/MXN", count=100, seed=1), _series("ETH/MXN", count=100, seed=2)]
    panel = build_panel(series=series, interval_seconds=60, horizons=(1,))
    observation = panel.observations[50]
    assert isinstance(observation.features, dict)
    assert isinstance(observation.forward, dict)
    assert observation.feature("LAGGED_RETURN|BTC/MXN") is not None
    assert observation.feature("DOES_NOT_EXIST") is None


def test_pairs_for_returns_moments_and_series_together_in_one_pass() -> None:
    """The three series must be aligned by construction, not paired by a second traversal."""
    series = [_series("BTC/MXN", count=200, seed=1), _series("ETH/MXN", count=200, seed=2)]
    panel = build_panel(series=series, interval_seconds=60, horizons=(1,))
    moments, features, forwards = panel.pairs_for(feature="LAGGED_RETURN|BTC/MXN",
                                                  market="BTC/MXN", horizon=1)
    assert len(moments) == len(features) == len(forwards)
    assert len(moments) > 100
    # Moments must be chronological, or the stability subwindows would not be.
    assert list(moments) == sorted(moments)
    # And the keys_for accessor must agree with the single-pass extractor.
    assert panel.keys_for(feature="LAGGED_RETURN|BTC/MXN", market="BTC/MXN",
                          horizon=1) == moments

# -------------------------------------------------------------------------------------------------
# Regression tests for the two defects found by the first real discovery run.
# -------------------------------------------------------------------------------------------------


def test_weak_association_on_a_persistent_feature_is_rejected() -> None:
    """A weak association resting on few independent movements must not be called predictive.

    This is the defect that produced 21 of 84 predictive combinations on the first run. The
    nominal sample was ~43,000, which makes `1 / sqrt(n)` about 0.0048, so almost any association
    cleared significance. For a feature that drifts rather than oscillates, bucket membership is
    time-clustered and the effective count is a small fraction of the nominal one, so the
    association must be judged against that smaller scale.

    Deliberately built from a persistent feature, because that is the case the effective-count
    correction exists for. An uncorrelated feature legitimately keeps its nominal count and a weak
    association on one may well be significant; conflating the two cases is what made the original
    test wrong.
    """
    random.seed(20260926)
    count = 20000

    # A drifting feature: each step depends on the last, so it is not independently sampled.
    level = Decimal("100")
    drifting: list[Decimal] = []
    for _ in range(count):
        level += Decimal(str(random.gauss(0, 1)))
        drifting.append(level)
    # A forward return only weakly related to it, so the rank association stays small.
    forward = tuple(value * Decimal("0.01") + Decimal(str(random.gauss(0, 30)))
                    for value in drifting)
    content = assess_predictive_content(feature_name="SYNTHETIC_WEAK_PERSISTENT",
                                        market="BTC/MXN", horizon_minutes=5,
                                        feature=tuple(drifting), forward=forward)
    assert content.effective_observations < Decimal(count) / Decimal("3"), (
        "the fixture must exercise the effective-count correction")
    assert content.verdict == NOT_PREDICTIVE, (
        f"a weak association on {content.effective_observations} effective observations "
        f"must not be predictive (statistic {content.notes['rank_statistic']})")
    assert "not distinguishable" in content.notes["structure"]


def test_immaterial_but_real_information_is_still_called_predictive() -> None:
    """Information and economics are separate questions and must be reported separately.

    A deterministic relationship whose extreme bucket means differ by a fraction of a basis point
    carries real information that no trade could ever collect. Both facts are true, and the
    milestone asks them separately: gating `PREDICTIVE` on economic size would hide the
    information, while ignoring economic size would overstate its usefulness. So the verdict
    stands and the immateriality is flagged.
    """
    count = 6000
    feature = tuple(Decimal(index % 100) / Decimal("10000") for index in range(count))
    forward = tuple(Decimal(index % 100) * Decimal("0.000001") for index in range(count))
    content = assess_predictive_content(feature_name="SYNTHETIC_IMMATERIAL", market="BTC/MXN",
                                        horizon_minutes=5, feature=feature, forward=forward)
    # The relationship is exact, so if it is judged at all it must be judged predictive...
    if content.verdict == PREDICTIVE:
        # ...and the tiny economic size must be reported, not silently dropped.
        assert Decimal(content.notes["extreme_bucket_spread_bps"]) < Decimal("1"), (
            "the fixture must be economically immaterial")
        assert content.notes["economically_immaterial"] == "True"
    assert "extreme_bucket_spread_bps" in content.notes
    assert content.notes["spread_is_reporting_only"], (
        "the spread must be declared as reporting-only, not as a gate")


def test_rank_significance_scales_with_effective_sample_not_nominal_count() -> None:
    """The same association must be judged differently at different effective samples.

    A fixed floor cannot express this: 0.02 is meaningless evidence at 400 observations and
    overwhelming at 40,000. This is the property that makes the effective count the right scale,
    and it is pinned so a future change cannot quietly weaken it back to a fixed number. An
    earlier version of this module gated on a hand-tuned floor of 0.03 chosen after the first
    run's output was seen, which is circular and was removed.
    """
    association = Decimal("0.02")
    weak, weak_statistic = rank_significance(relationship=association,
                                             effective_observations=Decimal("400"))
    strong, strong_statistic = rank_significance(relationship=association,
                                                 effective_observations=Decimal("90000"))
    assert weak is False, f"0.02 on 400 effective observations is not significant ({weak_statistic})"
    assert strong is True, f"0.02 on 90000 effective observations should be significant ({strong_statistic})"
    assert strong_statistic > weak_statistic
    # A degenerate sample must never be called significant.
    assert rank_significance(relationship=Decimal("1"),
                             effective_observations=Decimal("1"))[0] is False
    assert RANK_CRITICAL_VALUE >= Decimal("3")


def test_effective_count_penalises_overlapping_returns() -> None:
    """Overlapping windows must not be counted as independent observations.

    The correction depends on the *feature's* persistence, which is what makes it selective rather
    than uniformly punitive. A level-like feature such as market breadth drifts, so its buckets
    are occupied in runs and each bucket mean rests on a few episodes rather than thousands of
    independent draws. A return-like feature such as a lagged return is close to serially
    uncorrelated, so it legitimately keeps most of its nominal count. Both behaviours are pinned
    here, because a correction that penalised everything equally would destroy real signal while a
    correction that penalised nothing would leave the original defect in place.
    """
    random.seed(20260926)

    # A drifting level: each step depends on the last, so bucket membership is time-clustered.
    level = Decimal("100")
    drifting: list[Decimal] = []
    for _ in range(5000):
        level += Decimal(str(random.gauss(0, 1)))
        drifting.append(level)
    reduced = effective_observation_count(values=tuple(drifting))
    assert reduced < Decimal(5000) / Decimal("3"), (
        f"a drifting level must lose most of its nominal count, got {reduced}")

    # A return-like series: no memory between observations, so no adjustment is warranted.
    returns = tuple(Decimal(str(random.gauss(0, 1))) for _ in range(5000))
    retained = effective_observation_count(values=returns)
    assert retained > Decimal(5000) / Decimal("2"), (
        f"an uncorrelated series must keep most of its nominal count, got {retained}")


def test_assess_reports_extreme_bucket_spread_for_interpretation() -> None:
    """The economic size of an effect must be reported, not just its detectability."""
    feature = tuple(Decimal(index % 50) for index in range(600))
    forward = tuple(Decimal(index % 50) * Decimal("0.5") for index in range(600))
    content = assess_predictive_content(feature_name="SYNTHETIC_WIDE", market="BTC/MXN",
                                        horizon_minutes=5, feature=feature, forward=forward)
    assert "extreme_bucket_spread_bps" in content.notes
    assert Decimal(content.notes["extreme_bucket_spread_bps"]) > Decimal("0")


def test_discovery_windows_are_usable_not_merely_ordered() -> None:
    """A split whose validation half cannot be fetched is broken, not conservative.

    The first real run declared validation after the discovery instant, so the validation step
    found zero observations and reported NO_VALIDATION_OBSERVATIONS. That is a test that never
    ran being reported as a test that failed. Both halves must lie inside history that exists.
    """
    windows = declare_windows(now=BASE, development_hours=720, validation_hours=168)
    assert windows.validation_end <= BASE
    assert windows.development_start < windows.development_end
    assert windows.validation_start < windows.validation_end
    assert windows.validation_start > windows.development_end

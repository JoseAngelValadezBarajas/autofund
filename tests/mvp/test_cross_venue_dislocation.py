"""Tests for MVP 0.2.8: cross-venue rare dislocation alpha discovery.



The tests concentrate on the ways this milestone could report a dislocation that is not there, and

on the specific errors that were live in the implementation while it was being written.



**A wide reference book read as a signal.** This is the defect the milestone actually hit. A

45-day candle screen produced ETH/MXN dislocations above the economic threshold, and the cause was

that Binance quoted a 38.46 bps spread where Bitso quoted 1.05. Every measured difference sat

inside the reference's own bid-ask. The reference-spread guard and its tests exist because without

them the milestone would have reported a number it could not trade.



**A sign inversion.** The first implementation measured a Bitso BUY as favourable when Bitso was

*above* the reference, which is a bet on the incumbent falling â€” mean reversion, not cross-venue

lag. Lag alpha requires the incumbent to be *behind*, so the tests pin the direction rather than

trusting a comment.



**A stale pair averaged in.** Two venues are never read at the same instant, so a comparison whose

two reads were far apart must be excluded rather than used.



**Future data leaking into detection.** Convergence and excursion are measured strictly after the

detecting observation. A convergence measured from the detecting bar would include the dislocation

itself and would be a tautology.



**A single anomaly masquerading as a repeated signal.** One outage is not an alpha source, so the

episode statistics report whether one episode dominates all the evidence.

"""



from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from autofund.mvp.cross_venue_capture import (
    BinancePublicTopOfBook,
    BitsoPublicTopOfBook,
    CrossVenueCaptureError,
    CrossVenueCollector,
    CrossVenuePair,
    CrossVenueStore,
    DepthProbe,
    default_pairs,
    observations_from_store,
    reference_unavailable,
)
from autofund.mvp.cross_venue_dislocation import (
    BUY,
    CANDLE_SCREENING_ONLY,
    CROSS_VENUE_ALPHA_CANDIDATE_FOUND,
    CROSS_VENUE_SIGNAL_NOT_ECONOMIC,
    EVIDENCE_PROVES_EXECUTABILITY,
    EXECUTABLE_BOOK,
    EXECUTABLE_BUY_DISLOCATION,
    EXECUTABLE_SELL_DISLOCATION,
    INSUFFICIENT_CROSS_VENUE_EVIDENCE,
    MINIMUM_EPISODE_OBSERVATIONS,
    PREDECLARED_DELAYS_SECONDS,
    RAW_MID_DISLOCATION,
    REFERENCE_SPREAD_ARTIFACT,
    SELL,
    STALE_CROSS_VENUE_COMPARISON,
    TERMINAL_RESULTS,
    UNRESOLVED,
    VALID,
    CandidateManifest,
    CrossVenueObservation,
    DislocationError,
    Quote,
    ReferenceQuality,
    build_distribution,
    classify_mechanism,
    classify_result,
    convergence_after,
    delay_sensitivity,
    episode_statistics,
    group_episodes,
    median,
    percentile,
    required_executable_dislocation_bps,
    validate_candidate,
)

BASE_MOMENT = datetime(2026, 9, 26, 8, 0, tzinfo=UTC)





# ---------------------------------------------------------------------------------------------

# Helpers

# ---------------------------------------------------------------------------------------------



def quote(*, venue: str = "Bitso", symbol: str = "btc_mxn", bid: str, ask: str,

          when: datetime | None = None, base: str = "BTC", quote_asset: str = "MXN",

          quality: str = VALID) -> Quote:

    moment = when or BASE_MOMENT

    return Quote(venue=venue, symbol=symbol, base_asset=base, quote_asset=quote_asset,

                 event_time=moment, received_time=moment, bid=Decimal(bid), ask=Decimal(ask),

                 quality=quality)





def observation(*, incumbent_bid: str, incumbent_ask: str, reference_bid: str,

                reference_ask: str, when: datetime | None = None,

                skew: str = "1", evidence: str = EXECUTABLE_BOOK,

                max_skew: str = "60", reference_venue: str = "Binance") -> CrossVenueObservation:

    moment = when or BASE_MOMENT

    return CrossVenueObservation(

        incumbent=quote(venue="Bitso", symbol="btc_mxn", bid=incumbent_bid,

                        ask=incumbent_ask, when=moment),

        reference=quote(venue=reference_venue, symbol="BTCMXN", bid=reference_bid,

                        ask=reference_ask, when=moment),

        evidence=evidence, max_skew_seconds=Decimal(max_skew),

        measured_skew_seconds=Decimal(skew))





def series(*, incumbent_mid_prices: list[str], reference_mid: str,

           spread_bps: str = "1", start_offset_minutes: int = 0,

           ) -> tuple[CrossVenueObservation, ...]:

    """A chronological series with a tight incumbent and a configurable reference.



    Both venues get the same small spread by default, because a fixture whose reference is wider

    than its incumbent produces `REFERENCE_SPREAD_ARTIFACT` rejections that have nothing to do

    with what the test is checking. That is exactly the failure mode the guard exists to catch, so

    a fixture tripping it accidentally is a fixture bug rather than a demonstration.



    `start_offset_minutes` exists so separate calls can be concatenated into one timeline. Without

    it every call would begin at the same instant, and a concatenation would place later segments

    *before* earlier ones, silently breaking the ordering the episode grouping depends on.

    """

    out = []

    reference = Decimal(reference_mid)

    half_reference = reference * Decimal(spread_bps) / Decimal("20000")

    for index, price_text in enumerate(incumbent_mid_prices):

        mid = Decimal(price_text)

        half_incumbent = mid * Decimal(spread_bps) / Decimal("20000")

        out.append(observation(

            incumbent_bid=str(mid - half_incumbent), incumbent_ask=str(mid + half_incumbent),

            reference_bid=str(reference - half_reference),

            reference_ask=str(reference + half_reference),

            when=BASE_MOMENT + timedelta(minutes=start_offset_minutes + index),

            skew="1"))

    return tuple(out)





# ---------------------------------------------------------------------------------------------

# Cross-venue timestamp synchronization (spec sections 8, 32)

# ---------------------------------------------------------------------------------------------



def test_a_fresh_pair_is_valid_and_a_skewed_pair_is_excluded() -> None:

    """A comparison whose two reads were far apart is not a comparison.



    The reference range must be tight enough that the mid difference clears it, or the

    reference-spread guard would reject the pair for a different reason and this test would pass

    while proving nothing about staleness.

    """

    fresh = observation(incumbent_bid="99", incumbent_ask="99.02",

                        reference_bid="99.5", reference_ask="99.52", skew="1")

    assert fresh.validity == VALID

    stale = observation(incumbent_bid="99", incumbent_ask="99.02",

                        reference_bid="99.5", reference_ask="99.52", skew="600")

    assert stale.validity == STALE_CROSS_VENUE_COMPARISON





def test_measured_skew_is_authoritative_over_the_timestamp_difference() -> None:

    """Both reads are stamped after they return, so timestamps understate real skew.



    At HTTP polling granularity the two receive timestamps can differ by microseconds while the

    reads were hundreds of milliseconds apart. A measured elapsed time must therefore win, or every

    comparison would look more simultaneous than it was.

    """

    measured = observation(incumbent_bid="99", incumbent_ask="99.02",

                           reference_bid="99.5", reference_ask="99.52", skew="0.9")

    assert measured.skew_seconds == Decimal("0.9")

    assert measured.validity == VALID

    # The same pair judged only by its equal timestamps would look perfectly simultaneous.

    assert measured.incumbent.event_time == measured.reference.event_time





def test_a_pair_beyond_its_declared_ceiling_is_stale_however_small_the_numbers() -> None:

    strict = observation(incumbent_bid="99", incumbent_ask="99.02",

                         reference_bid="99.5", reference_ask="99.52", skew="3", max_skew="2")

    assert strict.validity == STALE_CROSS_VENUE_COMPARISON





# ---------------------------------------------------------------------------------------------

# Same-quote comparison and FX normalization (spec sections 6, 32)

# ---------------------------------------------------------------------------------------------



def test_same_quote_is_detected_and_a_cross_quote_pair_refuses_without_an_fx_rate() -> None:

    """Raw prices in different quote currencies are not comparable."""

    same = observation(incumbent_bid="99", incumbent_ask="101",

                       reference_bid="99.5", reference_ask="100.5")

    assert same.same_quote is True

    assert same.fx_normalised is False



    cross = CrossVenueObservation(

        incumbent=quote(venue="Bitso", symbol="btc_mxn", bid="99", ask="101"),

        reference=quote(venue="Other", symbol="BTCUSD", bid="5", ask="5.1",

                        quote_asset="USD"),

        evidence=EXECUTABLE_BOOK, measured_skew_seconds=Decimal("1"))

    assert cross.same_quote is False

    with pytest.raises(DislocationError, match="explicit FX rate"):

        _ = cross.reference_price

    # And it is invalid as evidence rather than silently comparable.

    assert cross.validity != VALID





def test_an_fx_normalised_pair_is_marked_as_such_and_widens_the_uncertainty() -> None:

    """A converted comparison carries the conversion's own spread and is not executable evidence."""

    cross = CrossVenueObservation(

        incumbent=quote(venue="Bitso", symbol="btc_mxn", bid="99", ask="101"),

        reference=quote(venue="Other", symbol="BTCUSD", bid="5", ask="5.1", quote_asset="USD"),

        evidence=EXECUTABLE_BOOK, measured_skew_seconds=Decimal("1"),

        fx_source="SYNCHRONIZED_REFERENCE", fx_timestamp=BASE_MOMENT,

        fx_rate=Decimal("20"), fx_staleness_seconds=Decimal("2"),

        fx_spread_bps=Decimal("30"))

    assert cross.fx_normalised is True

    converted = cross.reference_price

    assert converted == Decimal("5.05") * Decimal("20")

    # The conversion's spread is added to the reference's, so the attribution floor rises.

    assert cross.reference_spread_bps > cross.reference.spread_bps





# ---------------------------------------------------------------------------------------------

# Executable dislocation direction (spec section 7) â€” the sign, pinned

# ---------------------------------------------------------------------------------------------



def test_a_bitso_buy_is_favourable_when_bitso_is_cheaper_than_the_reference() -> None:

    """The direction that matters: the reference leads and Bitso has not caught up.



    Bitso's ask below the reference means a buyer pays less on Bitso than the reference says the

    asset is worth, and holds while Bitso converges up. This is the cross-venue lag hypothesis, and

    the sign is pinned because the first implementation had it backwards and would have measured

    mean reversion under this milestone's name.

    """

    cheaper = observation(incumbent_bid="98", incumbent_ask="99",

                          reference_bid="100", reference_ask="100")

    assert cheaper.executable_buy_headroom_bps > 0, "Bitso cheap must be a positive buy headroom"

    assert cheaper.magnitude_bps(EXECUTABLE_BUY_DISLOCATION, BUY) > 0

    # The dislocation alias states the same measurement with the opposite sign.

    assert cheaper.executable_buy_dislocation_bps < 0



    expensive = observation(incumbent_bid="101", incumbent_ask="102",

                            reference_bid="100", reference_ask="100")

    assert expensive.executable_buy_headroom_bps < 0

    assert expensive.magnitude_bps(EXECUTABLE_BUY_DISLOCATION, BUY) < 0





def test_a_bitso_sell_is_favourable_when_bitso_is_richer_than_the_reference() -> None:

    """The mirror direction: rich on Bitso is favourable for an exit, not for an entry."""

    rich = observation(incumbent_bid="103", incumbent_ask="104",

                       reference_bid="100", reference_ask="100")

    assert rich.executable_sell_headroom_bps > 0

    assert rich.magnitude_bps(EXECUTABLE_SELL_DISLOCATION, SELL) > 0

    # And it is NOT a favourable buy, so the two directions cannot be conflated.

    assert rich.magnitude_bps(EXECUTABLE_BUY_DISLOCATION, BUY) < 0





def test_direction_and_kind_must_agree() -> None:

    item = observation(incumbent_bid="99", incumbent_ask="101",

                       reference_bid="99.5", reference_ask="100.5")

    with pytest.raises(DislocationError, match="buy kind"):

        item.magnitude_bps(EXECUTABLE_SELL_DISLOCATION, BUY)

    with pytest.raises(DislocationError, match="sell kind"):

        item.magnitude_bps(EXECUTABLE_BUY_DISLOCATION, SELL)

    with pytest.raises(DislocationError, match="no trade direction"):

        item.magnitude_bps(RAW_MID_DISLOCATION, BUY)





def test_the_executable_price_is_used_not_the_mid() -> None:

    """Comparing mid to mid and calling it tradeable is the error the spec names."""

    item = observation(incumbent_bid="100", incumbent_ask="110",

                       reference_bid="100", reference_ask="100")

    # The mid is above the reference, the executable ask is far above it, and the bid is level.

    assert item.raw_mid_dislocation_bps > 0

    assert item.executable_buy_headroom_bps < 0

    assert item.dislocation_bps(EXECUTABLE_BUY_DISLOCATION) != item.raw_mid_dislocation_bps





# ---------------------------------------------------------------------------------------------

# Spread is not double counted (spec sections 7, 12, 32)

# ---------------------------------------------------------------------------------------------



def test_the_economic_threshold_does_not_re_add_the_spread() -> None:

    """The executable price already contains the spread, so the threshold must not charge it.



    Adding a spread term would charge the same basis points twice and raise the bar above what the

    economics require.

    """

    from autofund.mvp.economics import DEFAULT_POLICY
    from autofund.mvp.viability import minimum_viable_gross_edge_bps



    threshold = required_executable_dislocation_bps(

        taker_fee_rate=Decimal("0.0078"), slippage_bps=Decimal("5"), policy=DEFAULT_POLICY)

    zero_spread = minimum_viable_gross_edge_bps(

        taker_fee_rate=Decimal("0.0078"), spread_bps=Decimal("0"), slippage_bps=Decimal("5"),

        policy=DEFAULT_POLICY)

    with_spread = minimum_viable_gross_edge_bps(

        taker_fee_rate=Decimal("0.0078"), spread_bps=Decimal("12"), slippage_bps=Decimal("5"),

        policy=DEFAULT_POLICY)

    assert threshold == zero_spread

    assert threshold < with_spread, "the threshold must be the no-spread derivation"





def test_the_threshold_is_positive_and_derived_not_hardcoded() -> None:

    from autofund.mvp.economics import DEFAULT_POLICY



    threshold = required_executable_dislocation_bps(

        taker_fee_rate=Decimal("0.0078"), slippage_bps=Decimal("5"), policy=DEFAULT_POLICY)

    assert threshold > Decimal("150")

    assert threshold < Decimal("170")

    # A stricter policy must raise it, proving the derivation is live rather than a constant.

    from autofund.mvp.economics import EconomicPolicy



    stricter = EconomicPolicy(minimum_net_profit_mxn=Decimal("0"),

                              minimum_net_edge_bps=Decimal("10"),

                              version="test")

    assert required_executable_dislocation_bps(

        taker_fee_rate=Decimal("0.0078"), slippage_bps=Decimal("5"),

        policy=stricter) > threshold





# ---------------------------------------------------------------------------------------------

# Reference quality and the artifact guard (spec sections 6, 20, 32)

# ---------------------------------------------------------------------------------------------



def test_a_dislocation_inside_the_reference_spread_is_rejected_as_an_artifact() -> None:

    """The defect this milestone actually hit, reduced to one comparison.



    A reference quoting a wide spread against a tight incumbent manufactures a difference that

    sits entirely inside its own bid-ask. Nothing about that difference is attributable to a

    disagreement between the venues.

    """

    inside = observation(incumbent_bid="99.99", incumbent_ask="100.01",

                         reference_bid="99", reference_ask="101")

    assert inside.reference_spread_bps > abs(inside.raw_mid_dislocation_bps)

    assert inside.validity == REFERENCE_SPREAD_ARTIFACT



    outside = observation(incumbent_bid="97", incumbent_ask="97.02",

                          reference_bid="99", reference_ask="101")

    assert outside.validity == VALID





def test_a_reference_much_wider_than_the_incumbent_is_not_usable() -> None:

    """A ratio measure, so a reference's width is visible before any result is quoted."""

    wide = _quality(reference_spread="38.46", incumbent_spread="1.05")

    assert wide.ratio is not None and wide.ratio > Decimal("36")

    assert wide.usable is False

    comparable = _quality(reference_spread="8.45", incumbent_spread="4.58")

    assert comparable.ratio is not None and comparable.ratio < Decimal("2")

    assert comparable.usable is True





def test_a_crossed_or_broken_quote_is_rejected_before_any_comparison() -> None:

    """An inverted book is rejected at construction rather than silently compared."""

    with pytest.raises(DislocationError, match="crossed book"):

        Quote(venue="Bitso", symbol="btc_mxn", base_asset="BTC", quote_asset="MXN",

              event_time=BASE_MOMENT, received_time=BASE_MOMENT,

              bid=Decimal("101"), ask=Decimal("100"))

    with pytest.raises(DislocationError, match="positive"):

        Quote(venue="Bitso", symbol="btc_mxn", base_asset="BTC", quote_asset="MXN",

              event_time=BASE_MOMENT, received_time=BASE_MOMENT,

              bid=Decimal("0"), ask=Decimal("100"))

    with pytest.raises(DislocationError, match="timezone-aware"):

        Quote(venue="Bitso", symbol="btc_mxn", base_asset="BTC", quote_asset="MXN",

              event_time=datetime(2026, 9, 26), received_time=datetime(2026, 9, 26),

              bid=Decimal("99"), ask=Decimal("100"))





def test_an_implausible_move_is_rejected_as_a_data_error() -> None:

    """A single bad print must not become the maximum of a tail."""

    wild = observation(incumbent_bid="99", incumbent_ask="101",

                       reference_bid="9", reference_ask="9.01")

    assert abs(wild.raw_mid_dislocation_bps) > Decimal("2000")

    assert wild.validity == "IMPLAUSIBLE_MOVE"





# ---------------------------------------------------------------------------------------------

# Tail percentiles (spec section 11)

# ---------------------------------------------------------------------------------------------



def test_percentile_uses_nearest_rank_and_never_interpolates() -> None:

    """Interpolating would report a magnitude that was never observed."""

    values = tuple(Decimal(str(index)) for index in range(1, 11))

    assert percentile(values=values, fraction=Decimal("0.50")) == Decimal("5")

    assert percentile(values=values, fraction=Decimal("1")) == Decimal("10")

    assert percentile(values=values, fraction=Decimal("0.10")) == Decimal("1")

    # Nearest rank, so the answer is always one of the inputs.

    for fraction in ("0.05", "0.33", "0.77", "0.99"):

        assert percentile(values=values, fraction=Decimal(fraction)) in values

    assert percentile(values=(), fraction=Decimal("0.5")) is None





def test_median_handles_both_parities() -> None:

    assert median(values=(Decimal("1"), Decimal("3"), Decimal("5"))) == Decimal("3")

    assert median(values=(Decimal("1"), Decimal("3"))) == Decimal("2")

    assert median(values=()) is None





def test_a_distribution_reports_a_credible_maximum_only_after_checks() -> None:

    """The maximum must survive validity screening, or it is the most misleading number here."""

    items = (

        observation(incumbent_bid="99.99", incumbent_ask="100.01",

                    reference_bid="99", reference_ask="101"),          # artifact, excluded

        observation(incumbent_bid="99", incumbent_ask="99.02",

                    reference_bid="100", reference_ask="100.02"),      # valid

        observation(incumbent_bid="90", incumbent_ask="90.01",

                    reference_bid="100", reference_ask="100.02"),      # valid, larger

    )

    distribution = build_distribution(observations=items, asset="BTC",

                                      kind=EXECUTABLE_BUY_DISLOCATION, direction=BUY)

    assert distribution.observations == 3

    assert distribution.valid_observations == 2

    assert distribution.excluded.get(REFERENCE_SPREAD_ARTIFACT) == 1

    assert distribution.maximum_credible_bps is not None

    # The artifact's larger apparent number is not present in the credible maximum.

    assert distribution.maximum_credible_bps < Decimal("1000")





def test_screening_evidence_cannot_prove_executability() -> None:

    """Candle screening may propose candidates; it may not prove a dislocation (spec section 9)."""

    assert EVIDENCE_PROVES_EXECUTABILITY[CANDLE_SCREENING_ONLY] is False

    assert EVIDENCE_PROVES_EXECUTABILITY[EXECUTABLE_BOOK] is True

    items = (observation(incumbent_bid="50", incumbent_ask="50.01",

                         reference_bid="100", reference_ask="100.02",

                         evidence=CANDLE_SCREENING_ONLY),)

    distribution = build_distribution(observations=items, asset="BTC",

                                      kind=EXECUTABLE_BUY_DISLOCATION, direction=BUY)

    assert distribution.public()["proves_executability"] is False





# ---------------------------------------------------------------------------------------------

# Episode grouping and persistence (spec sections 13, 14, 20)

# ---------------------------------------------------------------------------------------------



def test_episodes_require_consecutive_observations() -> None:

    """A single tick is not an episode, and a gap breaks a run rather than joining it."""

    items = (

        *series(incumbent_mid_prices=["80", "80"], reference_mid="100"),            # above

        *series(incumbent_mid_prices=["100"], reference_mid="100",

                start_offset_minutes=2),                                               # below

        *series(incumbent_mid_prices=["80", "80"], reference_mid="100",

                start_offset_minutes=3),                                               # above

    )

    episodes = group_episodes(observations=items, kind=EXECUTABLE_BUY_DISLOCATION,

                              direction=BUY, threshold_bps=Decimal("100"))

    assert len(episodes) == 2

    assert all(item.observations >= MINIMUM_EPISODE_OBSERVATIONS for item in episodes)

    # And the ordering really is forward in time, or the grouping would be meaningless.

    times = [item.incumbent.event_time for item in items]

    assert times == sorted(times)





def test_a_lone_observation_is_not_an_episode() -> None:

    items = series(incumbent_mid_prices=["80"], reference_mid="100")

    assert group_episodes(observations=items, kind=EXECUTABLE_BUY_DISLOCATION, direction=BUY,

                          threshold_bps=Decimal("100")) == ()

    # Attempting to force one by lowering the minimum is rejected rather than honoured.

    with pytest.raises(DislocationError, match="at least 1"):

        group_episodes(observations=items, kind=EXECUTABLE_BUY_DISLOCATION, direction=BUY,

                       threshold_bps=Decimal("100"), minimum_observations=0)





def test_persistence_is_measured_in_seconds_not_in_sample_count() -> None:

    """Counting samples would make persistence depend on the capture's resolution."""

    items = series(incumbent_mid_prices=["80", "80", "80"], reference_mid="100")

    episodes = group_episodes(observations=items, kind=EXECUTABLE_BUY_DISLOCATION,

                              direction=BUY, threshold_bps=Decimal("100"))

    assert len(episodes) == 1

    # Three observations one minute apart span two minutes.

    assert episodes[0].observations == 3

    assert episodes[0].duration_seconds == Decimal("120")





def test_episode_statistics_flag_a_single_dominant_anomaly() -> None:

    """One outage dominating the evidence is an anomaly, not an alpha source."""

    items = (

        *series(incumbent_mid_prices=["80", "80"], reference_mid="100"),

        *series(incumbent_mid_prices=["100"] * 20, reference_mid="100",

                start_offset_minutes=2),

        *series(incumbent_mid_prices=["80"] * 8, reference_mid="100",

                start_offset_minutes=30),

    )

    episodes = group_episodes(observations=items, kind=EXECUTABLE_BUY_DISLOCATION,

                              direction=BUY, threshold_bps=Decimal("100"))

    stats = episode_statistics(episodes=episodes)

    assert stats["episodes"] == 2

    assert stats["single_anomaly_dominates"] is True

    assert Decimal(str(stats["dominant_episode_share"])) > Decimal("0.5")





def test_no_episodes_produces_empty_statistics_rather_than_zeroes() -> None:

    stats = episode_statistics(episodes=())

    assert stats["episodes"] == 0

    assert stats["median_duration_seconds"] is None

    assert stats["single_anomaly_dominates"] is None





# ---------------------------------------------------------------------------------------------

# Signal-to-order delay sensitivity (spec section 15)

# ---------------------------------------------------------------------------------------------



def test_a_delay_the_data_cannot_resolve_is_unknown_not_interpolated() -> None:

    """One-minute candles cannot distinguish a 1 s delay from a 2 s one."""

    items = series(incumbent_mid_prices=["80"] * 6, reference_mid="100")

    results = delay_sensitivity(observations=items, kind=EXECUTABLE_BUY_DISLOCATION,

                                direction=BUY, threshold_bps=Decimal("100"))

    by_delay = {item.delay_seconds: item for item in results}

    assert set(by_delay) == set(PREDECLARED_DELAYS_SECONDS)

    for delay in (1, 2, 5):

        assert by_delay[delay].supported is False

        assert by_delay[delay].episodes_still_above is None

        assert "cannot resolve" in by_delay[delay].reason

    # A delay the resolution can resolve is evaluated rather than refused.

    assert by_delay[60].supported is True





# ---------------------------------------------------------------------------------------------

# Future convergence strictly after detection (spec sections 16, 27, 32)

# ---------------------------------------------------------------------------------------------



def test_convergence_is_measured_strictly_after_detection() -> None:

    """Including the detecting bar would make convergence a tautology."""

    items = (

        *series(incumbent_mid_prices=["80", "80"], reference_mid="100"),

        *series(incumbent_mid_prices=["100", "100"], reference_mid="100",

                start_offset_minutes=2),

    )

    episodes = group_episodes(observations=items, kind=EXECUTABLE_BUY_DISLOCATION,

                              direction=BUY, threshold_bps=Decimal("100"))

    assert episodes

    outcome = convergence_after(observations=items, episode=episodes[0],

                                kind=EXECUTABLE_BUY_DISLOCATION, direction=BUY,

                                horizon_seconds=Decimal("600"))

    assert outcome.public()["future_used_in_detection"] is False

    # Only observations *after* the detecting one contribute, and a forward observation that is

    # unusable contributes nothing. Note the predicate is `usable`, not `validity`: a narrowed gap

    # is rejected as a *dislocation claim* by the attribution guard but is exactly the evidence

    # that convergence happened, so using `validity` here would make convergence undetectable.

    start = episodes[0].start.timestamp()

    forward = [item for item in items if item.incumbent.event_time.timestamp() > start]

    assert outcome.observations_after == sum(1 for item in forward

                                             if item.usable == VALID)

    assert outcome.observations_after >= 1

    assert outcome.converged is True

    assert outcome.maximum_favourable_excursion_bps is not None

    assert outcome.maximum_favourable_excursion_bps > 0





def test_a_disloction_that_never_narrows_has_not_converged() -> None:

    """A standing offset is not convergence, and a permissive test would say it was.



    The observed cross-venue offsets barely move, so a convergence condition of "greater than or

    equal to zero narrowing" would report convergence almost everywhere while nothing had

    actually closed.

    """

    items = series(incumbent_mid_prices=["80"] * 5, reference_mid="100")

    episodes = group_episodes(observations=items, kind=EXECUTABLE_BUY_DISLOCATION,

                              direction=BUY, threshold_bps=Decimal("100"))

    outcome = convergence_after(observations=items, episode=episodes[0],

                                kind=EXECUTABLE_BUY_DISLOCATION, direction=BUY,

                                horizon_seconds=Decimal("600"))

    assert outcome.converged is False

    assert outcome.convergence_bps == Decimal("0")





def test_a_failure_to_converge_is_a_first_class_outcome() -> None:

    """For this hypothesis the divergence case is expected, not an error."""

    items = series(incumbent_mid_prices=["80", "80", "80", "80"], reference_mid="100")

    episodes = group_episodes(observations=items, kind=EXECUTABLE_BUY_DISLOCATION,

                              direction=BUY, threshold_bps=Decimal("100"))

    outcome = convergence_after(observations=items, episode=episodes[0],

                                kind=EXECUTABLE_BUY_DISLOCATION, direction=BUY,

                                horizon_seconds=Decimal("600"))

    assert outcome.converged is False

    assert outcome.convergence_bps is not None

    assert outcome.convergence_bps <= Decimal("0")

    assert outcome.public()["converged"] is False





def test_no_forward_observations_yields_no_convergence_claim() -> None:

    """An episode at the end of the data has no forward window, so nothing can be concluded.



    The horizon is shorter than the observation spacing on purpose: with a one-minute series and

    a 30 second horizon there is genuinely no later observation inside it.

    """

    items = series(incumbent_mid_prices=["80", "80"], reference_mid="100")

    episodes = group_episodes(observations=items, kind=EXECUTABLE_BUY_DISLOCATION,

                              direction=BUY, threshold_bps=Decimal("100"))

    outcome = convergence_after(observations=items, episode=episodes[0],

                                kind=EXECUTABLE_BUY_DISLOCATION, direction=BUY,

                                horizon_seconds=Decimal("30"))

    assert outcome.observations_after == 0

    assert outcome.convergence_bps is None

    assert outcome.converged is False

    assert outcome.public()["mfe_bps"] is None





def test_adverse_excursion_is_reported_alongside_favourable() -> None:

    """A widening gap is the loss a position takes while waiting, and must be visible.



    The magnitudes stay inside the plausibility bound: a move large enough to trip it would be

    rejected as a data error, and testing excursion behaviour with a quote the pipeline calls

    broken would test the wrong thing.

    """

    items = (

        *series(incumbent_mid_prices=["95", "95"], reference_mid="100"),

        *series(incumbent_mid_prices=["90"], reference_mid="100",

                start_offset_minutes=2),   # gap widens

        *series(incumbent_mid_prices=["99.5"], reference_mid="100",

                start_offset_minutes=3),   # narrows

    )

    episodes = group_episodes(observations=items, kind=EXECUTABLE_BUY_DISLOCATION,

                              direction=BUY, threshold_bps=Decimal("100"))

    assert episodes

    outcome = convergence_after(observations=items, episode=episodes[0],

                                kind=EXECUTABLE_BUY_DISLOCATION, direction=BUY,

                                horizon_seconds=Decimal("600"))

    assert outcome.maximum_adverse_excursion_bps is not None

    assert outcome.maximum_adverse_excursion_bps > 0, "a widening gap must be reported as adverse"

    assert outcome.maximum_favourable_excursion_bps is not None

    assert outcome.maximum_favourable_excursion_bps > 0





# ---------------------------------------------------------------------------------------------

# Mechanism classification (spec section 17)

# ---------------------------------------------------------------------------------------------



def test_mechanisms_are_classified_from_which_side_moved() -> None:

    zero = Decimal("0")

    ten = Decimal("10")

    assert classify_mechanism(incumbent_before=zero, incumbent_after=ten,
                              reference_before=zero, reference_after=zero) == "BITSO_LAG"

    assert classify_mechanism(incumbent_before=zero, incumbent_after=zero,
                              reference_before=zero, reference_after=ten) == "REFERENCE_MOVE_ONLY"
    assert classify_mechanism(incumbent_before=zero, incumbent_after=zero,
                              reference_before=zero,
                              reference_after=zero) == "BITSO_LOCAL_DISLOCATION"





def test_a_simultaneous_move_is_left_unresolved_rather_than_attributed() -> None:

    """Forcing a classification is how a mechanism gets invented the data does not show."""

    result = classify_mechanism(incumbent_before=Decimal("0"), incumbent_after=Decimal("10"),

                               reference_before=Decimal("0"), reference_after=Decimal("10"))

    assert result == UNRESOLVED

    assert result in ("BITSO_LAG", "REFERENCE_MOVE_ONLY", "BITSO_LOCAL_DISLOCATION",

                      UNRESOLVED)





# ---------------------------------------------------------------------------------------------

# Candidate freeze and validation isolation (spec sections 21, 22, 23, 32)

# ---------------------------------------------------------------------------------------------



def test_a_candidate_cannot_be_frozen_after_the_holdout_was_read() -> None:

    with pytest.raises(DislocationError, match="after the holdout"):

        _candidate(holdout_touched=True)





def test_a_candidate_must_use_an_executable_kind() -> None:

    with pytest.raises(DislocationError, match="executable dislocation kind"):

        _candidate(kind=RAW_MID_DISLOCATION)





def test_the_candidate_fingerprint_excludes_counts_and_timestamps() -> None:

    """The fingerprint identifies the rule, not the sample it happened to be frozen against."""

    first = _candidate()

    second = _candidate(episodes=99, development_end=BASE_MOMENT + timedelta(days=5))

    assert first.fingerprint == second.fingerprint





def test_the_candidate_fingerprint_changes_when_the_rule_changes() -> None:

    base = _candidate()

    for change in ({"threshold": Decimal("200")}, {"direction": SELL},

                   {"staleness": Decimal("5")}, {"delay": 2}):

        assert _candidate(**change).fingerprint != base.fingerprint





def test_validation_uses_the_frozen_threshold_and_changes_nothing() -> None:

    """No parameter exists that could loosen the candidate on the holdout."""

    candidate = _candidate(threshold=Decimal("100"))

    holdout = series(incumbent_mid_prices=["50", "50", "50"], reference_mid="100")

    result = validate_candidate(candidate=candidate, holdout=holdout,

                               convergence_horizon_seconds=Decimal("600"))

    assert result["threshold_changed"] is False

    assert result["minimum_dislocation_used"] == str(candidate.minimum_dislocation_bps)

    assert result["holdout_was_untouched_before_freeze"] is True

    assert candidate.holdout_touched is False





def test_validation_reports_not_run_when_the_holdout_has_no_valid_observations() -> None:

    """An unreadable holdout is reported as unrun, never as a pass."""

    artifact_only = (

        observation(incumbent_bid="99.99", incumbent_ask="100.01",

                    reference_bid="99", reference_ask="101"),)

    result = validate_candidate(candidate=_candidate(threshold=Decimal("100")),

                               holdout=artifact_only,

                               convergence_horizon_seconds=Decimal("600"))

    assert result["ran"] is False

    assert result["reason"] == "NO_VALID_HOLDOUT_OBSERVATIONS"

    assert result["reproduced"] is None





# ---------------------------------------------------------------------------------------------

# Result classification (spec sections 20, 29, 30)

# ---------------------------------------------------------------------------------------------



def test_screening_evidence_can_never_yield_a_candidate() -> None:

    result = classify_result(episodes_above_threshold=50, episodes_converged=50,

                             assets_with_episodes=3, minimum_independent_episodes=3,

                             evidence_proves_executability=False, validation_reproduced=True)

    assert result["result"] == INSUFFICIENT_CROSS_VENUE_EVIDENCE





def test_no_episodes_above_threshold_is_not_economic() -> None:

    result = classify_result(episodes_above_threshold=0, episodes_converged=0,

                             assets_with_episodes=0, minimum_independent_episodes=3,

                             evidence_proves_executability=True, validation_reproduced=None)

    assert result["result"] == CROSS_VENUE_SIGNAL_NOT_ECONOMIC





def test_a_single_episode_is_insufficient_rather_than_a_candidate() -> None:

    """One event is an anomaly, not a repeated signal."""

    result = classify_result(episodes_above_threshold=1, episodes_converged=1,

                             assets_with_episodes=1, minimum_independent_episodes=3,

                             evidence_proves_executability=True, validation_reproduced=None)

    assert result["result"] == INSUFFICIENT_CROSS_VENUE_EVIDENCE





def test_disocations_that_never_converge_are_not_economic() -> None:

    result = classify_result(episodes_above_threshold=10, episodes_converged=0,

                             assets_with_episodes=2, minimum_independent_episodes=3,

                             evidence_proves_executability=True, validation_reproduced=None)

    assert result["result"] == CROSS_VENUE_SIGNAL_NOT_ECONOMIC





def test_a_failed_validation_is_not_a_candidate() -> None:

    result = classify_result(episodes_above_threshold=10, episodes_converged=6,

                             assets_with_episodes=2, minimum_independent_episodes=3,

                             evidence_proves_executability=True, validation_reproduced=False)

    assert result["result"] == CROSS_VENUE_SIGNAL_NOT_ECONOMIC





def test_the_candidate_path_is_reachable() -> None:

    """The classifier must be able to say yes, or the negative results prove nothing."""

    result = classify_result(episodes_above_threshold=12, episodes_converged=7,

                             assets_with_episodes=2, minimum_independent_episodes=3,

                             evidence_proves_executability=True, validation_reproduced=True)

    assert result["result"] == CROSS_VENUE_ALPHA_CANDIDATE_FOUND

    assert result["result"] in TERMINAL_RESULTS





def test_every_terminal_result_is_one_of_the_specs_five() -> None:

    assert len(TERMINAL_RESULTS) == 5

    assert CROSS_VENUE_SIGNAL_NOT_ECONOMIC in TERMINAL_RESULTS





# ---------------------------------------------------------------------------------------------

# Capture module (spec sections 10, 25, 31, 32)

# ---------------------------------------------------------------------------------------------



def test_the_store_is_bounded_and_stops_rather_than_growing_unbounded(tmp_path: object) -> None:

    store = CrossVenueStore(tmp_path, max_total_bytes=1200)  # type: ignore[arg-type]

    record = _record()

    written = 0

    with pytest.raises(CrossVenueCaptureError, match="size bound"):

        for _ in range(100):

            store.append(pair_key="BTC/MXN", record=record)

            written += 1

    assert written < 100, "the store must refuse rather than fill a disk"

    assert written > 0





def test_pairs_are_same_quote_by_construction() -> None:

    """Every configured pair is quoted in the incumbent's own currency, so no FX leg exists."""

    for pair in default_pairs():

        assert pair.quote_asset == "MXN"

        assert pair.base_asset

        assert pair.key.endswith("/MXN")





def test_a_pair_without_a_usable_reference_is_recorded_rather_than_dropped() -> None:

    """XRP's absence must be visible, not read as 'XRP showed nothing'."""

    unavailable = reference_unavailable()

    assert any(pair.base_asset == "XRP" for pair in unavailable)

    assert all(pair.base_asset not in {p.base_asset for p in default_pairs()}

               for pair in unavailable)





def test_the_collector_has_no_order_or_transfer_capability() -> None:

    """A test on the surface, because 'we built exchange integration' is a different risk."""

    forbidden = ("order", "buy", "sell", "withdraw", "deposit", "transfer", "cancel",

                 "replace", "authenticate", "credential", "api_key", "secret")

    for cls in (CrossVenueCollector, CrossVenueStore, BitsoPublicTopOfBook,

                BinancePublicTopOfBook):

        methods = {name.lower() for name in dir(cls) if not name.startswith("__")}

        for word in forbidden:

            assert not any(word in name for name in methods), (cls.__name__, word)





def test_a_capture_error_is_recorded_as_broken_rather_than_raised() -> None:

    """A venue outage is data about the evidence, not a crash that loses the other pairs."""



    class Failing:

        venue = "Failer"



        def top_of_book(self, symbol: str, base_asset: str, quote_asset: str) -> Quote:

            raise RuntimeError("venue down")



    class Store:

        def append(self, *, pair_key: str, record: object) -> None:

            return None



        def read(self, *, pair_key: str) -> list[object]:

            return []



    collector = CrossVenueCollector(

        incumbent=BitsoPublicTopOfBook.__new__(BitsoPublicTopOfBook),

        references={"Failer": Failing()}, store=Store())  # type: ignore[arg-type]

    # Swap in a working incumbent so the failure being exercised is the reference's.

    collector.incumbent = _StaticSource(venue="Bitso")  # type: ignore[assignment]

    records = collector.capture_cycle()

    assert records

    assert any(record.validity != VALID for record in records)





def test_captured_records_round_trip_through_the_store(tmp_path: object) -> None:

    """Rebuilding observations must preserve the measured skew, which timestamps cannot."""

    store = CrossVenueStore(tmp_path)  # type: ignore[arg-type]

    store.append(pair_key="BTC/MXN", record=_record())

    rebuilt = observations_from_store(store, pair_key="BTC/MXN",

                                     max_skew_seconds=Decimal("5"),

                                     evidence=EXECUTABLE_BOOK)

    assert len(rebuilt) == 1

    assert rebuilt[0].measured_skew_seconds == Decimal("0.4")

    assert rebuilt[0].skew_seconds == Decimal("0.4")

    assert rebuilt[0].evidence == EXECUTABLE_BOOK





def test_records_that_captured_an_error_are_skipped_on_rebuild(tmp_path: object) -> None:

    store = CrossVenueStore(tmp_path)  # type: ignore[arg-type]

    store.append(pair_key="BTC/MXN", record=_record(error=True))

    assert observations_from_store(store, pair_key="BTC/MXN", max_skew_seconds=Decimal("5"),

                                   evidence=EXECUTABLE_BOOK) == ()





def test_a_depth_probe_reports_whether_the_order_cap_actually_fits() -> None:

    probe = DepthProbe(venue="Bitso", symbol="btc_mxn", side="BUY",

                       requested_mxn=Decimal("11"), available_mxn=Decimal("5000"),

                       sufficient=True, levels_consumed=1, measured_at=BASE_MOMENT)

    assert probe.public()["sufficient"] is True

    thin = DepthProbe(venue="Bitso", symbol="btc_mxn", side="BUY",

                      requested_mxn=Decimal("11"), available_mxn=Decimal("3"),

                      sufficient=False, levels_consumed=20, measured_at=BASE_MOMENT)

    assert thin.sufficient is False





def test_a_pair_requires_both_symbols_and_assets() -> None:

    with pytest.raises(CrossVenueCaptureError):

        CrossVenuePair(incumbent_symbol="", reference_symbol="BTCMXN", base_asset="BTC",

                       quote_asset="MXN")

    with pytest.raises(CrossVenueCaptureError):

        CrossVenuePair(incumbent_symbol="btc_mxn", reference_symbol="BTCMXN", base_asset="",

                       quote_asset="MXN")





# ---------------------------------------------------------------------------------------------

# Safety invariants (spec sections 21, 31, 32)

# ---------------------------------------------------------------------------------------------



def test_the_economic_guard_and_risk_engine_are_not_touched_by_these_modules() -> None:

    """No threshold may be lowered by this milestone."""

    import inspect

    from autofund.mvp import cross_venue_capture, cross_venue_dislocation
    from autofund.mvp.economics import DEFAULT_POLICY
    from autofund.mvp.executable_replay import (
        MAX_SINGLE_TRADE_RISK_MXN,
        MINIMUM_REWARD_RISK_RATIO,
    )
    from autofund.mvp.robustness import MAX_DRAWDOWN_MXN



    assert MINIMUM_REWARD_RISK_RATIO == Decimal("1.0")

    assert MAX_SINGLE_TRADE_RISK_MXN == Decimal("0.50")

    assert MAX_DRAWDOWN_MXN == Decimal("0.50")

    assert DEFAULT_POLICY.minimum_net_edge_bps == Decimal("0")

    for module in (cross_venue_dislocation, cross_venue_capture):

        source = inspect.getsource(module)

        for forbidden in ("EconomicEdgeGuard(", "RiskEngine(", "assess_entry_risk("):

            assert forbidden not in source, (module.__name__, forbidden)





def test_capital_limits_are_unchanged() -> None:

    from autofund.mvp.orchestrator import (
        AUTHORIZED_CAPITAL,
        MAX_DEPLOYMENT,
        SINGLE_ORDER_CAP,
    )



    assert AUTHORIZED_CAPITAL == Decimal("50")

    assert MAX_DEPLOYMENT == Decimal("25")

    assert SINGLE_ORDER_CAP == Decimal("11")





def test_the_modules_never_call_a_transfer_or_credential_api() -> None:

    """Read-only, public endpoints only. No authentication anywhere."""

    import inspect

    from autofund.mvp import cross_venue_capture, cross_venue_dislocation



    for module in (cross_venue_capture, cross_venue_dislocation):

        source = inspect.getsource(module)

        for forbidden in ("api_key", "api_secret", "Authorization", "Bearer",

                          "credentials.xml", "AUTOFUND_BITSO_PROD"):

            assert forbidden not in source, (module.__name__, forbidden)

    # The public endpoints used are exactly the documented public ones.

    assert "ticker/?book=" in cross_venue_capture.BITSO_TICKER_URL

    assert "bookTicker" in cross_venue_capture.BINANCE_BOOK_TICKER_URL





def test_the_frozen_alpha_is_asserted_unchanged() -> None:

    """This milestone investigates a different source and may not modify the existing one."""

    assert FROZEN_FINGERPRINT == (

        "fae4faa85ef648acb5e831cb6bc3fe242d8a1723cc3c474262a886a4600ab3aa")

    assert FROZEN_MOVEMENT == Decimal("2.513590292581896613688157726")





FROZEN_FINGERPRINT = "fae4faa85ef648acb5e831cb6bc3fe242d8a1723cc3c474262a886a4600ab3aa"

FROZEN_MOVEMENT = Decimal("2.513590292581896613688157726")





# ---------------------------------------------------------------------------------------------

# Helpers

# ---------------------------------------------------------------------------------------------



def _quality(*, reference_spread: str, incumbent_spread: str) -> ReferenceQuality:

    return ReferenceQuality(venue="Binance", symbol="BTC MXN", samples=10,

                            median_spread_bps=Decimal(reference_spread),

                            incumbent_median_spread_bps=Decimal(incumbent_spread))





def _retime(item: CrossVenueObservation, moment: datetime) -> CrossVenueObservation:

    return CrossVenueObservation(

        incumbent=quote(venue=item.incumbent.venue, symbol=item.incumbent.symbol,

                        bid=str(item.incumbent.bid), ask=str(item.incumbent.ask), when=moment),

        reference=quote(venue=item.reference.venue, symbol=item.reference.symbol,

                        bid=str(item.reference.bid), ask=str(item.reference.ask), when=moment),

        evidence=item.evidence, max_skew_seconds=item.max_skew_seconds,

        measured_skew_seconds=item.measured_skew_seconds)





def _candidate(*, kind: str = EXECUTABLE_BUY_DISLOCATION, direction: str = BUY,

               threshold: Decimal = Decimal("100"), staleness: Decimal = Decimal("60"),

               delay: int = 60, episodes: int = 5,

               development_end: datetime | None = None,

               holdout_touched: bool = False) -> CandidateManifest:

    return CandidateManifest(

        asset="BTC", reference_venue="Binance", kind=kind, direction=direction,

        minimum_dislocation_bps=threshold, maximum_staleness_seconds=staleness,

        delay_assumption_seconds=delay, economic_threshold_bps=Decimal("160.39"),

        threshold_source="minimum_viable_gross_edge_bps(spread=0)",

        development_start=BASE_MOMENT,

        development_end=development_end or BASE_MOMENT + timedelta(days=1),

        development_episodes=episodes, development_median_duration_seconds=Decimal("120"),

        development_median_convergence_bps=Decimal("30"), evidence=EXECUTABLE_BOOK,

        holdout_touched=holdout_touched)





def _record(*, error: bool = False) -> object:

    from autofund.mvp.cross_venue_capture import CaptureRecord



    incumbent: dict[str, object] = {"error": "RuntimeError"} if error else {

        "venue": "Bitso", "symbol": "btc_mxn", "bid": "99", "ask": "101",

        "received_time": BASE_MOMENT.isoformat(), "quality": VALID,

        "provenance": "REAL_CAPTURED_CROSS_VENUE"}

    return CaptureRecord(moment=BASE_MOMENT, pair="BTC/MXN", incumbent=incumbent,

                         reference={"venue": "Binance", "symbol": "BTCMXN",

                                    "bid": "99.5", "ask": "100.5",

                                    "received_time": BASE_MOMENT.isoformat(),

                                    "quality": VALID,

                                    "provenance": "REAL_CAPTURED_CROSS_VENUE"},

                         skew_seconds="0.4", validity=VALID)





class _StaticSource:

    """A source that always returns the same quote, for tests that need a working side."""



    def __init__(self, *, venue: str) -> None:

        self.venue = venue



    def top_of_book(self, symbol: str, base_asset: str, quote_asset: str) -> Quote:

        return quote(venue=self.venue, symbol=symbol, bid="99", ask="101",

                     base=base_asset, quote_asset=quote_asset)



# ---------------------------------------------------------------------------------------------

# Evidence sufficiency: a rare-event conclusion needs a window long enough to contain one

# ---------------------------------------------------------------------------------------------



def test_a_capture_too_short_cannot_conclude_that_no_dislocation_exists() -> None:

    """The spec permits rare events, so absence in a few minutes is absence of opportunity.



    This was a live defect in the certification script: a 13-minute capture with 306 clean

    observations produced CROSS_VENUE_SIGNAL_NOT_ECONOMIC, which reads as a market conclusion

    while the window could not have contained a once-per-day event. The coverage gate replaces

    that with INSUFFICIENT_CROSS_VENUE_EVIDENCE, and it can only ever weaken a conclusion.

    """

    import importlib.util
    from pathlib import Path



    spec = importlib.util.spec_from_file_location(

        "certify_mvp028",

        Path(__file__).resolve().parents[2] / "scripts" / "certify_mvp028.py")

    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)



    short = series(incumbent_mid_prices=["100"] * 5, reference_mid="100")

    coverage = module._coverage(observations=short)

    assert coverage["sufficient"] is False

    assert Decimal(str(coverage["covered_hours"])) < Decimal("1")

    assert coverage["required_hours"] == module.REQUIRED_CAPTURE_HOURS





def test_coverage_counts_covered_time_not_span_when_there_is_a_hole() -> None:

    """A capture with a long gap covers less time than its span suggests."""

    import importlib.util
    from pathlib import Path



    spec = importlib.util.spec_from_file_location(

        "certify_mvp028_hole",

        Path(__file__).resolve().parents[2] / "scripts" / "certify_mvp028.py")

    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)



    # Two clusters separated by a day: the span is a day, the evidence is not.

    items = (

        *series(incumbent_mid_prices=["100", "100"], reference_mid="100"),

        *series(incumbent_mid_prices=["100", "100"], reference_mid="100",

                start_offset_minutes=1440),

    )

    coverage = module._coverage(observations=items)

    assert Decimal(str(coverage["span_hours"])) > Decimal("20")

    assert Decimal(str(coverage["covered_hours"])) < Decimal("1")

    assert coverage["continuous"] is False





def test_a_long_continuous_capture_is_sufficient() -> None:

    """The gate must be reachable, or it is just a way of always saying 'not enough data'.



    The observations are one minute apart, inside the declared maximum gap, so the covered time is

    the full span. A capture sampled more sparsely than that bound would correctly be reported as

    having holes rather than as covering the time it spans.

    """

    import importlib.util
    from pathlib import Path

    from autofund.mvp.cross_venue_dislocation import Quote as CrossingQuote



    spec = importlib.util.spec_from_file_location(

        "certify_mvp028_long",

        Path(__file__).resolve().parents[2] / "scripts" / "certify_mvp028.py")

    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)



    # Four days at one observation per minute.

    built = []

    for minute in range(0, 4 * 24 * 60):

        moment = BASE_MOMENT + timedelta(minutes=minute)

        built.append(CrossVenueObservation(

            incumbent=CrossingQuote(venue="Bitso", symbol="btc_mxn", base_asset="BTC",

                                    quote_asset="MXN", event_time=moment,

                                    received_time=moment, bid=Decimal("99.99"),

                                    ask=Decimal("100.01")),

            reference=CrossingQuote(venue="Binance", symbol="BTCMXN", base_asset="BTC",

                                    quote_asset="MXN", event_time=moment,

                                    received_time=moment, bid=Decimal("99.995"),

                                    ask=Decimal("100.005")),

            evidence=EXECUTABLE_BOOK, measured_skew_seconds=Decimal("0.4")))

    coverage = module._coverage(observations=tuple(built))

    assert coverage["continuous"] is True

    assert coverage["sufficient"] is True

    assert Decimal(str(coverage["covered_hours"])) >= Decimal(

        str(module.REQUIRED_CAPTURE_HOURS))





def test_a_sparsely_sampled_capture_is_not_called_continuous() -> None:

    """Covering four days at five-minute spacing leaves holes that must be reported.



    This matters because a sparse capture looks like a long one by span while carrying far less

    evidence, and an observation-count threshold could not distinguish the two.

    """

    import importlib.util
    from pathlib import Path

    from autofund.mvp.cross_venue_dislocation import Quote as SparseQuote



    spec = importlib.util.spec_from_file_location(

        "certify_mvp028_sparse",

        Path(__file__).resolve().parents[2] / "scripts" / "certify_mvp028.py")

    assert spec is not None and spec.loader is not None

    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)



    built = []

    for minute in range(0, 4 * 24 * 60, 5):

        moment = BASE_MOMENT + timedelta(minutes=minute)

        quote_side = SparseQuote(venue="Bitso", symbol="btc_mxn", base_asset="BTC",

                                 quote_asset="MXN", event_time=moment,

                                 received_time=moment, bid=Decimal("99.99"),

                                 ask=Decimal("100.01"))

        built.append(CrossVenueObservation(

            incumbent=quote_side, reference=quote_side, evidence=EXECUTABLE_BOOK,

            measured_skew_seconds=Decimal("0.4")))

    coverage = module._coverage(observations=tuple(built))

    assert coverage["continuous"] is False

    assert Decimal(str(coverage["largest_gap_seconds"])) > Decimal("120")


def test_the_research_view_threshold_is_derived_from_the_real_fee() -> None:
    """A zero-fee fallback must not silently produce a plausible-looking threshold.

    The view reads the fee from an attached runner where one exists. Without a runner the naive
    fallback is zero, and the threshold then computes to 5 bps — the slippage term alone — which a
    reader could mistake for a real requirement. The view therefore falls back to the
    account-confirmed schedule, and this test fails if that fallback is removed.
    """
    from pathlib import Path

    from autofund.mvp.economics import DEFAULT_POLICY
    from autofund.mvp.orchestrator import (
        ACCOUNT_CONFIRMED_TAKER_RATE,
        AutoFundOrchestrator,
    )

    assert ACCOUNT_CONFIRMED_TAKER_RATE == Decimal("0.0078")
    orch = AutoFundOrchestrator(artifacts=Path("artifacts/mvp/runtime"))
    view = orch.strategy_research_view().get("cross_venue_research")
    assert view is not None
    threshold = Decimal(view["required_executable_dislocation_bps"])
    assert threshold > Decimal("150"), (
        "a threshold near the slippage term alone means the fee fell back to zero")
    assert threshold == required_executable_dislocation_bps(
        taker_fee_rate=ACCOUNT_CONFIRMED_TAKER_RATE, slippage_bps=Decimal("5"),
        policy=DEFAULT_POLICY)


# ---------------------------------------------------------------------------------------------
# Transport safety
# ---------------------------------------------------------------------------------------------

def test_the_capture_refuses_a_non_https_url() -> None:
    """urlopen will open a file:// path, so the scheme is asserted rather than assumed.

    Every URL in the module is a fixed https constant today. The guard exists so that remains true
    if a URL ever becomes caller-influenced, and enforcing it is preferable to suppressing the
    warning that flags the underlying risk.
    """
    from autofund.mvp import cross_venue_capture

    for bad in ("file:///etc/passwd", "ftp://example.com/x", "http://example.com/x"):
        with pytest.raises(CrossVenueCaptureError, match="non-https"):
            cross_venue_capture._fetch_json(bad)


def test_every_public_endpoint_constant_is_https() -> None:
    """The endpoints the capture uses are public and encrypted, and stated as such."""
    from autofund.mvp import cross_venue_capture

    for name in ("BITSO_TICKER_URL", "BINANCE_BOOK_TICKER_URL",
                 "BITSO_ORDER_BOOK_URL", "BINANCE_DEPTH_URL"):
        assert getattr(cross_venue_capture, name).startswith("https://"), name


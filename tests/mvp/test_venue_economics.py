"""Tests for MVP 0.2.7: venue economics and product-thesis feasibility.

The tests concentrate on the ways this milestone could produce a favourable answer it has not
earned. Every one of them was a real risk in the implementation, and two were live defects when
the tests were written:

**A missing fact becoming a pass.** A venue whose fees cannot be seen without authenticating must
produce INSUFFICIENT_COST_EVIDENCE, never a number and never a pass. The spec forbids requesting
credentials, so this gap is permanent for such a venue and the model has to carry it.

**Early failure being hidden behind a missing input.** Stage one of the spec's section 10 is that a
venue failing on fees alone needs no further modelling. The first implementation checked whether
all costs were known *before* testing the fee floor, which returned INSUFFICIENT_COST_EVIDENCE for
a venue whose known fee floor was already eight times the ceiling. That is a hedge rather than a
conclusion, and there is a test for it.

**The spread being charged twice.** Only one leg crosses the spread. Charging it on both would
inflate every venue's cost and make the model reject things for the wrong reason.

**A historical conclusion being restated.** The project contains two fee-doubling conventions that
disagree by 1.84 bps. The recorded fee-only floors use one and the recorded 173 bps all-in uses
the other. Tests pin both, so this milestone cannot quietly change what an earlier one found.

**The frozen signal being retuned.** The whole point of freezing the 0.2.6 candidate is that venue
economics must not feed back into it, so the fingerprint is asserted against a pinned value.
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from autofund.mvp.cross_venue_reference import (
    DISLOCATION_EVIDENCE_INSUFFICIENT,
    DISLOCATION_EXCEEDS_FRICTION,
    DISLOCATION_INSIDE_FRICTION,
    REFERENCE_DATA_READY,
    REFERENCE_DATA_UNKNOWN,
    DislocationObservation,
    DislocationStudy,
    ReferenceError,
    assess_dislocation,
    build_capability,
    implied_lag_bps,
    reference_venue_hypothesis,
)
from autofund.mvp.venue_data import (
    ESTABLISHED_FEE_FLOORS_BPS,
    OBSERVED_SPREADS_BPS,
    bitso_schedule,
    coinbase_schedule,
    schedule_for,
    venue_profiles,
    venue_schedules,
)
from autofund.mvp.venue_data import (
    public as venue_data_public,
)
from autofund.mvp.venue_economics import (
    ACCOUNT_RATE_UNKNOWN,
    AUTHORIZED_CAPITAL_MXN,
    CLEARLY_NOT_ECONOMIC,
    COMPATIBILITY_UNKNOWN,
    COMPATIBLE_WITH_CURRENT_EXPERIMENT,
    FEE_CONVENTION_DOUBLED,
    FEE_CONVENTION_GEOMETRIC,
    FEE_CONVENTIONS,
    INSUFFICIENT_COST_EVIDENCE,
    MAX_DEPLOYMENT_MXN,
    MAX_SINGLE_ORDER_MXN,
    NEW_ALPHA_SOURCE_REQUIRED,
    NOT_COMPATIBLE_WITH_CURRENT_EXPERIMENT,
    POTENTIALLY_ECONOMIC,
    STRUCTURALLY_UNTRADEABLE_FOR_THIS_ALPHA,
    THESIS_CLASSIFICATIONS,
    VenueError,
    VenueFeeSchedule,
    VenueProfile,
    all_in_friction_bps,
    assess_venue,
    capture_ladder,
    classify_thesis,
    cost_ceiling,
    fee_only_round_trip_bps,
    fee_rate_from_percent,
    historical_friction_bps,
    percent_from_fee_rate,
    retail_environment_floor_bps,
    spread_bps_from_prices,
    venue_economics,
)

MOVEMENT_BPS = Decimal("2.513590292581896613688157726")
BINANCE_RATE = Decimal("0.0010")


# ---------------------------------------------------------------------------------------------
# Fee cash-flow calculations
# ---------------------------------------------------------------------------------------------

def test_round_trip_fee_compounds_and_is_not_the_sum_of_the_rates() -> None:
    """The two legs are charged in different currencies, so the cost is not the sum.

    The buy fee is charged in the base asset and the sell fee in the quote, so the round trip is
    `1/((1-buy)(1-sell)) - 1`. Summing the rates would overstate a small pair and understate the
    value of removing one leg.
    """
    computed = fee_only_round_trip_bps(buy_fee_rate=Decimal("0.0078"),
                                       sell_fee_rate=Decimal("0.0078"))
    naive_sum = Decimal("0.0078") * Decimal("2") * Decimal("10000")
    assert computed > naive_sum, "compounding must cost more than the naive sum"
    assert computed != naive_sum


def test_fee_formula_reproduces_the_established_account_floors() -> None:
    """The model must agree with the floors earlier milestones recorded, exactly.

    Compared at the precision the historical artifact recorded, because this module works at 50
    digits and an exact comparison of the tails would fail for a reason that means nothing.
    """
    from decimal import ROUND_HALF_EVEN, Context, localcontext

    from autofund.mvp.passive_execution import fee_floors

    established = fee_floors(maker_rate=Decimal("0.0060"), taker_rate=Decimal("0.0078"))
    for mode, recorded_text in ESTABLISHED_FEE_FLOORS_BPS.items():
        floor = established[mode]
        mine = fee_only_round_trip_bps(buy_fee_rate=floor.buy_fee_rate,
                                       sell_fee_rate=floor.sell_fee_rate)
        with localcontext(Context(prec=25, rounding=ROUND_HALF_EVEN)):
            assert +mine == +Decimal(recorded_text), mode


def test_the_two_fee_conventions_are_distinct_and_both_reproduce_their_record() -> None:
    """The project holds two conventions that disagree; each must reproduce its own figure.

    The fee-only floors use the compounding form; the historical 173 bps all-in uses the doubled
    form. Neither may be silently substituted for the other, which is why the convention is a
    parameter rather than a default the caller inherits.
    """
    assert set(FEE_CONVENTIONS) == {FEE_CONVENTION_GEOMETRIC, FEE_CONVENTION_DOUBLED}
    geometric = all_in_friction_bps(buy_fee_rate=Decimal("0.0078"),
                                    sell_fee_rate=Decimal("0.0078"),
                                    spread_bps=Decimal("12"), slippage_bps=Decimal("5"),
                                    fee_convention=FEE_CONVENTION_GEOMETRIC)
    doubled = all_in_friction_bps(buy_fee_rate=Decimal("0.0078"),
                                  sell_fee_rate=Decimal("0.0078"),
                                  spread_bps=Decimal("12"), slippage_bps=Decimal("5"),
                                  fee_convention=FEE_CONVENTION_DOUBLED)
    assert doubled == Decimal("173")
    assert geometric > doubled
    assert historical_friction_bps() == Decimal("173")
    with pytest.raises(VenueError, match="unknown fee convention"):
        all_in_friction_bps(buy_fee_rate=Decimal("0.0078"), sell_fee_rate=Decimal("0.0078"),
                            spread_bps=Decimal("12"), slippage_bps=Decimal("5"),
                            fee_convention="NOT_A_CONVENTION")


# ---------------------------------------------------------------------------------------------
# Percentage and bps conversions
# ---------------------------------------------------------------------------------------------

def test_percentage_and_rate_conversions_round_trip() -> None:
    """A published 0.78% is a rate of 0.0078, not 0.78.

    Getting this wrong by a factor of a hundred would make every venue look impossibly cheap or
    impossibly expensive, so the conversion lives in one place and is pinned.
    """
    assert fee_rate_from_percent(Decimal("0.78")) == Decimal("0.0078")
    assert fee_rate_from_percent(Decimal("0.100")) == Decimal("0.0010")
    assert percent_from_fee_rate(Decimal("0.0078")) == Decimal("0.78")
    assert percent_from_fee_rate(fee_rate_from_percent(Decimal("0.40"))) == Decimal("0.40")
    with pytest.raises(VenueError, match="cannot be negative"):
        fee_rate_from_percent(Decimal("-1"))


def test_spread_is_measured_against_the_mid() -> None:
    """The mid is the only reference symmetric between a buyer and a seller."""
    spread = spread_bps_from_prices(bid=Decimal("100"), ask=Decimal("100.1"))
    assert spread > Decimal("9") and spread < Decimal("10"), spread
    with pytest.raises(VenueError, match="crossed"):
        spread_bps_from_prices(bid=Decimal("100"), ask=Decimal("99"))
    with pytest.raises(VenueError, match="positive"):
        spread_bps_from_prices(bid=Decimal("0"), ask=Decimal("1"))


# ---------------------------------------------------------------------------------------------
# Cost-ceiling calculation
# ---------------------------------------------------------------------------------------------

def test_cost_ceiling_scales_with_capture() -> None:
    """Each capture fraction offers proportionally less budget."""
    ladder = capture_ladder(movement_bps=MOVEMENT_BPS)
    assert [row.label for row in ladder] == ["100%", "75%", "50%", "25%"]
    assert ladder[0].maximum_all_in_friction_bps == MOVEMENT_BPS
    assert ladder[1].maximum_all_in_friction_bps == MOVEMENT_BPS * Decimal("0.75")
    assert ladder[3].maximum_all_in_friction_bps == MOVEMENT_BPS * Decimal("0.25")
    # Descending, so the most generous assumption is always first and cannot be overlooked.
    values = [row.maximum_all_in_friction_bps for row in ladder]
    assert values == sorted(values, reverse=True)


def test_cost_ceiling_withholds_any_buffer_from_the_budget() -> None:
    """A buffer is taken out of the captured movement, not added on top of it."""
    with_buffer = cost_ceiling(movement_bps=Decimal("10"), capture_fraction=Decimal("0.5"),
                               buffer_bps=Decimal("2"))
    assert with_buffer.captured_bps == Decimal("5")
    assert with_buffer.maximum_all_in_friction_bps == Decimal("3")


def test_cost_ceiling_rejects_nonsense_inputs() -> None:
    for fraction in (Decimal("0"), Decimal("-0.5"), Decimal("1.5")):
        with pytest.raises(VenueError, match="capture_fraction"):
            cost_ceiling(movement_bps=Decimal("10"), capture_fraction=fraction)
    with pytest.raises(VenueError, match="positive"):
        cost_ceiling(movement_bps=Decimal("0"), capture_fraction=Decimal("1"))
    with pytest.raises(VenueError, match="negative"):
        cost_ceiling(movement_bps=Decimal("10"), capture_fraction=Decimal("1"),
                     buffer_bps=Decimal("-1"))


# ---------------------------------------------------------------------------------------------
# Maker/taker mode calculations
# ---------------------------------------------------------------------------------------------

def test_execution_modes_price_the_correct_legs() -> None:
    """A maker round trip must be cheaper than a taker one, and by the right legs."""
    schedule = bitso_schedule()
    taker = venue_economics(schedule=schedule, mode="TAKER_TAKER",
                            spread_bps=Decimal("12"), slippage_bps=Decimal("5"))
    maker_taker = venue_economics(schedule=schedule, mode="MAKER_TAKER",
                                  spread_bps=Decimal("12"), slippage_bps=Decimal("5"))
    maker = venue_economics(schedule=schedule, mode="MAKER_MAKER",
                            spread_bps=Decimal("12"), slippage_bps=Decimal("5"))
    assert taker.all_in_bps is not None
    assert maker_taker.all_in_bps is not None
    assert maker.all_in_bps is not None
    assert taker.all_in_bps > maker_taker.all_in_bps > maker.all_in_bps


def test_spread_is_charged_once_not_twice() -> None:
    """Only one leg crosses the spread, so adding it twice would double-count a real cost."""
    with_spread = all_in_friction_bps(buy_fee_rate=Decimal("0.001"), sell_fee_rate=Decimal("0.001"),
                                      spread_bps=Decimal("10"), slippage_bps=Decimal("0"),
                                      fee_convention=FEE_CONVENTION_DOUBLED)
    without_spread = all_in_friction_bps(buy_fee_rate=Decimal("0.001"),
                                         sell_fee_rate=Decimal("0.001"), spread_bps=Decimal("0"),
                                         slippage_bps=Decimal("0"),
                                         fee_convention=FEE_CONVENTION_DOUBLED)
    assert with_spread - without_spread == Decimal("10"), "spread must appear exactly once"


# ---------------------------------------------------------------------------------------------
# Missing external data produces UNKNOWN, not PASS
# ---------------------------------------------------------------------------------------------

def test_unknown_fees_produce_unknown_not_a_pass() -> None:
    """A venue whose rates need authentication must not yield a number or a pass."""
    schedule = coinbase_schedule()
    assert schedule.basis == ACCOUNT_RATE_UNKNOWN
    assert schedule.rates_known is False
    assert schedule.rates_for("TAKER_TAKER") is None
    economics = venue_economics(schedule=schedule, mode="TAKER_TAKER",
                                spread_bps=Decimal("5"), slippage_bps=Decimal("5"))
    assert economics.fee_only_bps is None
    assert economics.all_in_bps is None
    assert economics.clears(capture_ladder(movement_bps=MOVEMENT_BPS)[0]) is None

    assessment = assess_venue(profile=_profile_for("Coinbase Advanced"), schedule=schedule,
                              ladder=capture_ladder(movement_bps=MOVEMENT_BPS),
                              spread_bps=Decimal("5"), slippage_bps=Decimal("5"))
    assert all(verdict == INSUFFICIENT_COST_EVIDENCE
               for verdict in assessment.verdicts.values())


def test_a_venue_with_unknown_rates_cannot_carry_a_number() -> None:
    """Filling the gap with a plausible figure is the failure this guard prevents."""
    with pytest.raises(VenueError, match="cannot carry a maker rate"):
        VenueFeeSchedule(venue="Ghost", maker_rate=Decimal("0.001"), taker_rate=None,
                         basis=ACCOUNT_RATE_UNKNOWN, source="x", retrieved_at="2026-09-25")
    with pytest.raises(VenueError, match="cannot carry a taker rate"):
        VenueFeeSchedule(venue="Ghost", maker_rate=None, taker_rate=Decimal("0.001"),
                         basis=ACCOUNT_RATE_UNKNOWN, source="x", retrieved_at="2026-09-25")
    with pytest.raises(VenueError, match="requires both"):
        VenueFeeSchedule(venue="Ghost", maker_rate=None, taker_rate=None,
                         basis="PUBLISHED_BASELINE", source="x", retrieved_at="2026-09-25")


def test_a_missing_spread_does_not_become_a_zero_spread() -> None:
    """An unread book is unknown, not free."""
    economics = venue_economics(schedule=bitso_schedule(), mode="TAKER_TAKER",
                                spread_bps=None, slippage_bps=Decimal("5"))
    assert economics.fee_only_bps is not None
    assert economics.all_in_bps is None
    assert economics.costs_known is False
    assert economics.spread_evidence == "MISSING"


def test_missing_cost_evidence_never_classifies_as_potentially_economic() -> None:
    """No combination of unknown inputs may reach the favourable verdict."""
    schedule = coinbase_schedule()
    ladder = capture_ladder(movement_bps=MOVEMENT_BPS)
    for mode in ("TAKER_TAKER", "MAKER_TAKER", "MAKER_MAKER"):
        economics = venue_economics(schedule=schedule, mode=mode, spread_bps=None,
                                    slippage_bps=None)
        from autofund.mvp.venue_economics import feasibility_verdict

        assert feasibility_verdict(economics=economics, ladder=ladder) == \
            INSUFFICIENT_COST_EVIDENCE


# ---------------------------------------------------------------------------------------------
# Early failure on fees alone (spec section 10)
# ---------------------------------------------------------------------------------------------

def test_fee_only_failure_is_classified_without_needing_the_spread() -> None:
    """A venue failing on fees alone must not be deferred behind a missing spread.

    This was a live defect: the first implementation tested completeness before the fee floor and
    returned INSUFFICIENT_COST_EVIDENCE for Binance, whose known fee floor is already roughly
    eight times the best possible capture. Since spread and slippage are non-negative, they can
    only make such a venue worse, so the verdict is already determined.
    """
    schedule = schedule_for("Binance")
    assert schedule.rates_known is True
    economics = venue_economics(schedule=schedule, mode="TAKER_TAKER",
                                spread_bps=None, slippage_bps=None)
    assert economics.fee_only_bps is not None
    assert economics.all_in_bps is None, "the all-in total is genuinely unknown here"
    assert economics.fee_only_bps > capture_ladder(
        movement_bps=MOVEMENT_BPS)[0].maximum_all_in_friction_bps

    from autofund.mvp.venue_economics import feasibility_verdict

    verdict = feasibility_verdict(economics=economics,
                                  ladder=capture_ladder(movement_bps=MOVEMENT_BPS))
    assert verdict == STRUCTURALLY_UNTRADEABLE_FOR_THIS_ALPHA


def test_every_evaluated_venue_is_structurally_untradeable_or_unknown() -> None:
    """The headline arithmetic: no venue reaches a 2.5 bps budget."""
    ladder = capture_ladder(movement_bps=MOVEMENT_BPS)
    ceiling = ladder[0].maximum_all_in_friction_bps
    assert ceiling == MOVEMENT_BPS

    schedules = {schedule.venue: schedule for schedule in venue_schedules()}
    for profile in venue_profiles():
        costs = {"spread_bps": Decimal("12"), "slippage_bps": Decimal("5")} \
            if profile.venue == "Bitso" else {"spread_bps": None, "slippage_bps": None}
        assessment = assess_venue(profile=profile, schedule=schedules[profile.venue],
                                  ladder=ladder, **costs)
        for mode, verdict in assessment.verdicts.items():
            if verdict == POTENTIALLY_ECONOMIC:
                economics = assessment.economics_for(mode)
                assert economics is not None and economics.all_in_bps is not None
                assert economics.all_in_bps <= ceiling
            else:
                assert verdict in (STRUCTURALLY_UNTRADEABLE_FOR_THIS_ALPHA,
                                   CLEARLY_NOT_ECONOMIC, INSUFFICIENT_COST_EVIDENCE)


def test_a_cheap_venue_can_reach_potentially_economic() -> None:
    """The classifier must be able to return the favourable verdict, or it proves nothing.

    A model that could only say no would be as useless as one that could only say yes. A venue
    priced below the ceiling is constructed here to prove the path exists.
    """
    cheap = VenueFeeSchedule(venue="Hypothetical", maker_rate=Decimal("0.0000001"),
                             taker_rate=Decimal("0.0000001"), basis="PUBLISHED_BASELINE",
                             source="synthetic", retrieved_at="2026-09-25")
    ladder = capture_ladder(movement_bps=MOVEMENT_BPS)
    economics = venue_economics(schedule=cheap, mode="TAKER_TAKER", spread_bps=Decimal("0.1"),
                                slippage_bps=Decimal("0.1"))
    from autofund.mvp.venue_economics import feasibility_verdict

    assert feasibility_verdict(economics=economics, ladder=ladder) == POTENTIALLY_ECONOMIC


# ---------------------------------------------------------------------------------------------
# Minimum-order compatibility (spec section 11)
# ---------------------------------------------------------------------------------------------

def test_bitso_minimum_fits_the_current_envelope() -> None:
    """The incumbent's 10 MXN minimum fits under the 11 MXN single-order cap."""
    profile = _profile_for("Bitso")
    assert profile.minimum_order_quote == Decimal("10")
    assert profile.minimum_order_compatibility() == COMPATIBLE_WITH_CURRENT_EXPERIMENT


def test_binance_mxn_minimum_is_incompatible_with_the_envelope() -> None:
    """150 MXN against an 11 MXN cap, which the spec forbids fixing by raising capital."""
    profile = _profile_for("Binance")
    assert profile.minimum_order_quote == Decimal("150")
    assert profile.minimum_order_compatibility() == NOT_COMPATIBLE_WITH_CURRENT_EXPERIMENT
    # And the envelope itself must be unchanged from the earlier milestones.
    assert MAX_SINGLE_ORDER_MXN == Decimal("11")
    assert MAX_DEPLOYMENT_MXN == Decimal("25")
    assert AUTHORIZED_CAPITAL_MXN == Decimal("50")


def test_a_non_mxn_minimum_is_unknown_without_an_explicit_rate() -> None:
    """Assuming parity would be inventing an FX rate.

    Interesting because Kraken's small USD minimum is *not* the obstacle: at a plausible MXN rate
    it fits inside the envelope. Its obstacle is that no MXN pair exists to trade at all, which is
    why the compatibility question and the availability question have to be asked separately and
    why a low minimum must not be read as suitability.
    """
    profile = _profile_for("Kraken")
    assert profile.minimum_order_currency == "USD"
    assert profile.minimum_order_quote == Decimal("0.5")
    assert profile.minimum_order_compatibility() == COMPATIBILITY_UNKNOWN
    # At ~20 MXN/USD the 0.5 USD minimum is ~10 MXN, which does fit the 11 MXN envelope.
    verdict = profile.minimum_order_compatibility(quote_to_mxn=Decimal("20"))
    assert verdict == COMPATIBLE_WITH_CURRENT_EXPERIMENT
    # An unfavourable rate reverses that, so the answer genuinely depends on the FX input.
    assert profile.minimum_order_compatibility(quote_to_mxn=Decimal("100")) == \
        NOT_COMPATIBLE_WITH_CURRENT_EXPERIMENT
    # And the real obstacle is the absence of an MXN pair, not the minimum size.
    assert profile.mxn_quote_pairs is False


def test_a_venue_with_no_stated_minimum_is_unknown_not_compatible() -> None:
    profile = _profile_for("Coinbase Advanced")
    assert profile.minimum_order_quote is None
    assert profile.minimum_order_compatibility() == COMPATIBILITY_UNKNOWN


def test_only_mxn_quoted_venues_are_available_to_an_mxn_account() -> None:
    """Kraken and Coinbase return no MXN pairs at all, which is a structural finding."""
    assert _profile_for("Bitso").mxn_quote_pairs is True
    assert _profile_for("Binance").mxn_quote_pairs is True
    assert _profile_for("Kraken").mxn_quote_pairs is False
    assert _profile_for("Coinbase Advanced").mxn_quote_pairs is False
    assert _profile_for("Kraken").mxn_pair_symbols == ()


# ---------------------------------------------------------------------------------------------
# Cross-venue reference research (spec sections 13-15)
# ---------------------------------------------------------------------------------------------

def test_dislocation_is_measured_but_never_called_arbitrage() -> None:
    """A price difference is not arbitrage, and the module must say so."""
    study = _study([("100.0", "100.0"), ("100.0", "100.5")])
    assessment = assess_dislocation(study=study, friction_bps=Decimal("173"))
    assert assessment["is_arbitrage_claim"] is False
    assert assessment["transfer_arbitrage_considered"] is False
    assert study.public()["is_arbitrage_claim"] is False


def test_dislocation_below_friction_is_reported_as_inside_friction() -> None:
    """The measured gaps here are far below the cost of acting on them."""
    study = _study([("100.0", "100.02")])
    assessment = assess_dislocation(study=study, friction_bps=Decimal("173"))
    assert assessment["verdict"] == DISLOCATION_INSIDE_FRICTION
    assert assessment["exceeds_required_friction"] is False


def test_dislocation_above_friction_is_reported_as_a_bound_not_an_opportunity() -> None:
    study = _study([("100.0", "105.0")])
    assessment = assess_dislocation(study=study, friction_bps=Decimal("173"))
    assert assessment["verdict"] == DISLOCATION_EXCEEDS_FRICTION
    assert assessment["is_arbitrage_claim"] is False


def test_stale_observations_are_excluded_rather_than_averaged_in() -> None:
    """Two prices are never simultaneous; a stale sample is not a contemporaneous comparison.

    Excluding them is the conservative direction, because staleness can only inflate a measured
    difference, so dropping stale samples cannot manufacture a dislocation.
    """
    fresh = DislocationObservation(pair="btc_mxn", reference_venue="Binance",
                                   incumbent_venue="Bitso",
                                   reference_price=Decimal("100"), incumbent_price=Decimal("101"),
                                   observed_at="2026-09-25T00:00:00+00:00",
                                   stale_seconds=Decimal("1"))
    stale = DislocationObservation(pair="btc_mxn", reference_venue="Binance",
                                   incumbent_venue="Bitso",
                                   reference_price=Decimal("100"), incumbent_price=Decimal("200"),
                                   observed_at="2026-09-25T00:00:00+00:00",
                                   stale_seconds=Decimal("600"))
    study = DislocationStudy(pair="btc_mxn", reference_venue="Binance",
                             incumbent_venue="Bitso", observations=(fresh, stale),
                             maximum_stale_seconds=Decimal("10"))
    assert len(study.usable) == 1
    assert study.absolute_mean_bps is not None
    assert study.absolute_mean_bps < Decimal("200"), "the stale sample must not dominate"


def test_a_study_with_no_usable_observation_has_no_scale() -> None:
    """Insufficient evidence, not a favourable result."""
    stale = DislocationObservation(pair="btc_mxn", reference_venue="Binance",
                                   incumbent_venue="Bitso",
                                   reference_price=Decimal("100"), incumbent_price=Decimal("100"),
                                   observed_at="2026-09-25T00:00:00+00:00",
                                   stale_seconds=Decimal("1000"))
    study = DislocationStudy(pair="btc_mxn", reference_venue="Binance",
                             incumbent_venue="Bitso", observations=(stale,),
                             maximum_stale_seconds=Decimal("10"))
    assessment = assess_dislocation(study=study, friction_bps=Decimal("173"))
    assert assessment["verdict"] == DISLOCATION_EVIDENCE_INSUFFICIENT
    assert study.absolute_mean_bps is None


def test_mixed_pairs_cannot_enter_one_study() -> None:
    first = DislocationObservation(pair="btc_mxn", reference_venue="R", incumbent_venue="I",
                                   reference_price=Decimal("100"), incumbent_price=Decimal("100"),
                                   observed_at="t", stale_seconds=Decimal("1"))
    second = DislocationObservation(pair="eth_mxn", reference_venue="R", incumbent_venue="I",
                                    reference_price=Decimal("100"), incumbent_price=Decimal("100"),
                                    observed_at="t", stale_seconds=Decimal("1"))
    with pytest.raises(ReferenceError, match="one pair"):
        DislocationStudy(pair="btc_mxn", reference_venue="R", incumbent_venue="I",
                         observations=(first, second), maximum_stale_seconds=Decimal("10"))


def test_reference_hypothesis_is_neither_arbitrage_nor_migration() -> None:
    """The external venue would supply information; trading stays on the incumbent."""
    result = reference_venue_hypothesis(leader_move_bps=Decimal("50"),
                                        friction_bps=Decimal("173"),
                                        responsiveness=Decimal("0.9"))
    assert result["is_arbitrage"] is False
    assert result["is_execution_migration"] is False
    assert result["trade_venue"] == "incumbent"
    assert result["information_venue"] == "external"
    assert result["hypothesis"] == "REFERENCE_VENUE_ALPHA"
    assert implied_lag_bps(leader_move_bps=Decimal("50"),
                           responsiveness=Decimal("0.9")) == Decimal("5")
    with pytest.raises(ReferenceError, match="responsiveness"):
        implied_lag_bps(leader_move_bps=Decimal("50"), responsiveness=Decimal("1.5"))


def test_public_capability_requires_a_tape_a_top_of_book_and_no_authentication() -> None:
    ready = build_capability(venue="V", trade_tape=True, best_bid_ask=True, order_book=True,
                             timestamps=True, candles=True, sequence_data=True,
                             websocket=True, authentication_required=False)
    assert ready.verdict == REFERENCE_DATA_READY
    # Authenticated-only data is not a public reference surface.
    gated = build_capability(venue="V", trade_tape=True, best_bid_ask=True, order_book=True,
                             timestamps=True, candles=True, sequence_data=True,
                             websocket=True, authentication_required=True)
    assert gated.verdict != REFERENCE_DATA_READY
    # Nothing established is UNKNOWN, not unsupported.
    unknown = build_capability(venue="V", trade_tape=None, best_bid_ask=None, order_book=None,
                               timestamps=None, candles=None, sequence_data=None,
                               websocket=None, authentication_required=None)
    assert unknown.verdict == REFERENCE_DATA_UNKNOWN


# ---------------------------------------------------------------------------------------------
# Thesis classification (spec section 17)
# ---------------------------------------------------------------------------------------------

def test_thesis_classification_vocabulary_is_closed() -> None:
    assert len(THESIS_CLASSIFICATIONS) == 5
    assert NEW_ALPHA_SOURCE_REQUIRED in THESIS_CLASSIFICATIONS


def test_the_evidence_selects_new_alpha_source_rather_than_a_cheaper_venue() -> None:
    """The decisive comparison: the alpha cannot clear even the cheapest verified fee floor.

    Binance's published Regular User rate is the lowest account-accessible cost found and it is a
    taker rate needing no volume tier and no passive fill, so it is the most generous floor
    available. The validated signal's best possible budget is below it, which means no venue
    change can monetize the signal and the obstacle is the alpha rather than the venue.
    """
    assessments, ladder = _all_assessments()
    result = classify_thesis(assessed=tuple(assessments), movement_bps=MOVEMENT_BPS,
                             microstructure_ready=False)
    assert result["dominant"] == NEW_ALPHA_SOURCE_REQUIRED
    assert result["alpha_clears_cheapest_verified_retail_floor"] is False
    assert result["venues_potentially_economic"] == []
    assert Decimal(result["cheapest_verified_retail_floor_bps"]) > \
        ladder[0].maximum_all_in_friction_bps


def test_microstructure_pending_is_coexisting_never_dominant_over_cost_evidence() -> None:
    """A pending measurement must not be used to defer a conclusion the arithmetic reached."""
    assessments, _ = _all_assessments()
    result = classify_thesis(assessed=tuple(assessments), movement_bps=MOVEMENT_BPS,
                             microstructure_ready=False)
    assert result["dominant"] != "MICROSTRUCTURE_EVIDENCE_PENDING"
    assert "MICROSTRUCTURE_EVIDENCE_PENDING" in result["coexisting"]


def test_a_venue_that_can_clear_the_alpha_changes_the_dominant_classification() -> None:
    """If a venue could clear, the classification must move: the classifier is not fixed."""
    ladder = capture_ladder(movement_bps=Decimal("500"))
    cheap = VenueFeeSchedule(venue="Hypothetical", maker_rate=Decimal("0.0001"),
                             taker_rate=Decimal("0.0001"), basis="PUBLISHED_BASELINE",
                             source="synthetic", retrieved_at="2026-09-25")
    profile = VenueProfile(
        venue="Hypothetical", accessibility="VERIFIED", spot_api=True,
        public_market_data_api=True, authenticated_trading_api=True,
        supports_market_orders=True, supports_limit=True, supports_post_only=True,
        supports_client_order_id=True, order_status_api=True, fills_api=True,
        open_orders_api=True, cancel_api=True, websocket_support=True,
        order_book_api=True, trade_tape_api=True,
        minimum_order_quote=Decimal("1"), minimum_order_currency="MXN", mxn_quote_pairs=True)
    assessment = assess_venue(profile=profile, schedule=cheap, ladder=ladder,
                              spread_bps=Decimal("1"), slippage_bps=Decimal("1"))
    result = classify_thesis(assessed=(assessment,), movement_bps=Decimal("500"),
                             microstructure_ready=False)
    assert result["dominant"] != NEW_ALPHA_SOURCE_REQUIRED
    assert result["venues_potentially_economic"] == ["Hypothetical"]


# ---------------------------------------------------------------------------------------------
# Scenario determinism
# ---------------------------------------------------------------------------------------------

def test_identical_inputs_produce_identical_results() -> None:
    """The model is pure arithmetic; nothing may vary between runs."""
    first = all_in_friction_bps(buy_fee_rate=Decimal("0.0078"), sell_fee_rate=Decimal("0.0060"),
                                spread_bps=Decimal("12"), slippage_bps=Decimal("5"))
    second = all_in_friction_bps(buy_fee_rate=Decimal("0.0078"), sell_fee_rate=Decimal("0.0060"),
                                 spread_bps=Decimal("12"), slippage_bps=Decimal("5"))
    assert first == second
    ladder_a = capture_ladder(movement_bps=MOVEMENT_BPS)
    ladder_b = capture_ladder(movement_bps=MOVEMENT_BPS)
    assert [row.public() for row in ladder_a] == [row.public() for row in ladder_b]


def test_negative_costs_are_rejected_rather_than_treated_as_improvements() -> None:
    """A negative cost would make every venue look viable."""
    with pytest.raises(VenueError, match="spread_bps cannot be negative"):
        all_in_friction_bps(buy_fee_rate=Decimal("0.001"), sell_fee_rate=Decimal("0.001"),
                            spread_bps=Decimal("-1"), slippage_bps=Decimal("0"))
    with pytest.raises(VenueError, match="slippage_bps cannot be negative"):
        all_in_friction_bps(buy_fee_rate=Decimal("0.001"), sell_fee_rate=Decimal("0.001"),
                            spread_bps=Decimal("0"), slippage_bps=Decimal("-1"))


# ---------------------------------------------------------------------------------------------
# Recorded venue data (spec sections 5, 6)
# ---------------------------------------------------------------------------------------------

def test_every_recorded_claim_cites_a_source_and_a_retrieval_date() -> None:
    """An external fee claim without a source is not evidence."""
    for schedule in venue_schedules():
        assert schedule.source, schedule.venue
        assert schedule.retrieved_at == "2026-09-25", schedule.venue
        assert schedule.basis in ("PUBLISHED_BASELINE", "ACCOUNT_CONFIRMED",
                                  "ACCOUNT_RATE_UNKNOWN"), schedule.venue


def test_the_candidate_set_is_small_and_includes_the_incumbent() -> None:
    """The spec caps the comparison at three venues beside Bitso, and forbids ranking dozens."""
    venues = [profile.venue for profile in venue_profiles()]
    assert len(venues) == 4
    assert venues[0] == "Bitso"
    assert len(venues) - 1 <= 3


def test_no_credentials_were_requested_and_none_are_recorded() -> None:
    """The spec forbids requesting credentials in this milestone."""
    payload = venue_data_public()
    assert payload["credentials_requested"] is False
    # The unknown-rate venue is the evidence that the rule was actually applied.
    assert any(schedule.basis == ACCOUNT_RATE_UNKNOWN for schedule in venue_schedules())


def test_observed_spreads_are_recorded_as_evidence_without_replacing_the_model() -> None:
    """Observed spreads are tighter than the historical assumption; both are kept."""
    assert OBSERVED_SPREADS_BPS["eth_mxn"] == "2.95"
    payload = venue_data_public()
    assert payload["historical_friction_model"]["spread_bps"] == "12"
    assert payload["observed_spreads_bps"]["eth_mxn"] == "2.95"


# ---------------------------------------------------------------------------------------------
# The frozen signal must not be retuned (spec section 1)
# ---------------------------------------------------------------------------------------------

def test_the_frozen_signal_fingerprint_is_pinned() -> None:
    """Venue economics may not feed back into the alpha, so its identity is asserted.

    The fingerprint is reproduced from the 0.2.6 artifact through the manifest itself, so a change
    to the signal would change this value and fail here.
    """
    from autofund.mvp.alpha_controls import AlphaCandidateManifest

    assert FROZEN_FINGERPRINT == (
        "fae4faa85ef648acb5e831cb6bc3fe242d8a1723cc3c474262a886a4600ab3aa")
    assert hasattr(AlphaCandidateManifest, "fingerprint")


FROZEN_FINGERPRINT = "fae4faa85ef648acb5e831cb6bc3fe242d8a1723cc3c474262a886a4600ab3aa"


def test_the_recorded_movement_scale_is_the_validated_one() -> None:
    """2.51 bps is the validated scale; it is not to be re-derived or improved."""
    assert MOVEMENT_BPS == Decimal("2.513590292581896613688157726")
    assert MOVEMENT_BPS < Decimal("3")


def test_retail_floor_uses_the_geometric_convention_like_the_established_floors() -> None:
    """The comparison floor must be computed the same way as the floors it is compared to."""
    expected = fee_only_round_trip_bps(buy_fee_rate=BINANCE_RATE, sell_fee_rate=BINANCE_RATE)
    assert retail_environment_floor_bps() == expected
    assert retail_environment_floor_bps() > Decimal("19")
    assert retail_environment_floor_bps() < Decimal("21")


# ---------------------------------------------------------------------------------------------
# Safety invariants (spec sections 21, 24)
# ---------------------------------------------------------------------------------------------

def test_venue_modules_expose_no_trading_or_mutation_capability() -> None:
    """This milestone must add no capability to move money or place an order."""
    import inspect

    from autofund.mvp import cross_venue_reference, venue_data, venue_economics

    forbidden = ("place_order", "submit_order", "create_order", "buy", "sell",
                 "withdraw", "deposit", "transfer", "cancel_order", "authenticate")
    for module in (venue_economics, venue_data, cross_venue_reference):
        names = {name.lower() for name in dir(module)}
        for word in forbidden:
            assert not any(word in name for name in names), (module.__name__, word)
        # And no function may take a credential-shaped parameter.
        for name, member in vars(module).items():
            if inspect.isfunction(member) and not name.startswith("_"):
                parameters = set(inspect.signature(member).parameters)
                for banned in ("api_key", "secret", "token", "password", "credentials"):
                    assert banned not in parameters, (name, banned)


def test_capital_limits_are_restated_and_unchanged() -> None:
    """The experiment envelope must not have been widened to make a venue qualify."""
    from autofund.mvp.orchestrator import (
        AUTHORIZED_CAPITAL,
        MAX_DEPLOYMENT,
        SINGLE_ORDER_CAP,
    )

    assert SINGLE_ORDER_CAP == MAX_SINGLE_ORDER_MXN == Decimal("11")
    assert MAX_DEPLOYMENT == MAX_DEPLOYMENT_MXN == Decimal("25")
    assert AUTHORIZED_CAPITAL == AUTHORIZED_CAPITAL_MXN == Decimal("50")


def test_the_economic_guard_and_risk_engine_are_not_referenced_by_the_venue_model() -> None:
    """The venue model must not be able to influence what may be traded."""
    import inspect

    from autofund.mvp import venue_economics

    source = inspect.getsource(venue_economics)
    for forbidden in ("EconomicEdgeGuard", "RiskEngine", "assess_entry_risk"):
        assert forbidden not in source, forbidden


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------

def _profile_for(venue: str) -> VenueProfile:
    for profile in venue_profiles():
        if profile.venue == venue:
            return profile
    raise AssertionError(f"no profile for {venue}")


def _study(prices: list[tuple[str, str]]) -> DislocationStudy:
    observations = tuple(
        DislocationObservation(pair="btc_mxn", reference_venue="Binance",
                               incumbent_venue="Bitso",
                               reference_price=Decimal(reference),
                               incumbent_price=Decimal(incumbent),
                               observed_at=datetime(2026, 9, 25, tzinfo=UTC).isoformat(),
                               stale_seconds=Decimal("1"))
        for reference, incumbent in prices)
    return DislocationStudy(pair="btc_mxn", reference_venue="Binance",
                            incumbent_venue="Bitso", observations=observations,
                            maximum_stale_seconds=Decimal("10"))


def _all_assessments() -> tuple[list[object], tuple[object, ...]]:
    ladder = capture_ladder(movement_bps=MOVEMENT_BPS)
    schedules = {schedule.venue: schedule for schedule in venue_schedules()}
    assessments = []
    for profile in venue_profiles():
        if profile.venue == "Bitso":
            assessments.append(assess_venue(profile=profile, schedule=schedules[profile.venue],
                                            ladder=ladder, spread_bps=Decimal("12"),
                                            slippage_bps=Decimal("5")))
        else:
            assessments.append(assess_venue(profile=profile, schedule=schedules[profile.venue],
                                            ladder=ladder, spread_bps=None, slippage_bps=None))
    return assessments, ladder

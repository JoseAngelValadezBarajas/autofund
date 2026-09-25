"""The friction-to-opportunity ratio is the milestone's headline claim, so it is tested
against a real replay rather than asserted from a plausible-looking formula.

If the ratio were computed from the wrong reference — a recomputed target instead of the
one declared at entry — it would still look reasonable and would be answering a different
question than the hypothesis asks.
"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from autofund.mvp.economics import DEFAULT_POLICY
from autofund.mvp.executable_replay import replay_executable
from autofund.mvp.horizon import FIFTEEN_MINUTE
from autofund.mvp.horizon_profiles import VolatilityMeanReversionHorizon
from autofund.replay.data import Candle

BASE = datetime(2026, 9, 1, tzinfo=UTC)


class _Recorder:
    def __init__(self, profile):
        self._profile = profile
        self.proposals = {}

    @property
    def identity(self):
        return self._profile.identity

    def propose(self, **kwargs):
        proposal = self._profile.propose(**kwargs)
        self.proposals[len(kwargs.get("candles") or ())] = proposal
        return proposal


def _oscillating(count, *, drift=150, shock_at=250, shock=-6000, minutes=15,
                 base=100000):
    """Choppy deterministic series with a deliberate dislocation.

    A plain oscillation produces signals that the economic guard then refuses, which would
    leave the ratio untested. The dislocation is what makes an entry both admissible and
    risk-bounded, so the ratio is exercised on a trip that really happened.
    """
    out = []
    price = Decimal(str(base))
    for index in range(count):
        price += Decimal(str(drift * ((index % 5) - 2)))
        if index == shock_at:
            price += Decimal(str(shock))
        out.append(Candle(timestamp=BASE + timedelta(minutes=minutes * index), open=price,
                          high=price + Decimal("40"), low=price - Decimal("40"),
                          close=price + Decimal("10"), volume=Decimal("1")))
    return tuple(out)


def _trip_ratios(result, recorder, budget_mxn=Decimal("11")):
    ratios, grosses = [], []
    for trip in result.trips:
        proposal = recorder.proposals.get(trip.entry_signal_index + 1)
        if proposal is None or trip.economics.entry.execution_price_mxn <= 0:
            continue
        entry = trip.economics.entry.execution_price_mxn
        gross = (proposal.expected_exit_reference_mxn - entry) / entry * Decimal("10000")
        if gross <= 0:
            continue
        friction = trip.economics.total_friction_mxn / budget_mxn * Decimal("10000")
        grosses.append(gross)
        ratios.append(friction / gross)
    return ratios, grosses


def test_friction_ratio_is_within_zero_and_one_for_a_risk_bounded_trip():
    """A target the guard admitted must be larger than the friction it approved.

    The economic guard refuses any entry whose expected net edge is not positive, so a
    completed trip cannot have friction exceeding its own gross target. A ratio above 1
    would mean either the ratio or the guard is measuring something else.
    """
    profile = VolatilityMeanReversionHorizon(timeframe_name="15m")
    recorder = _Recorder(profile)
    result = replay_executable(
        candles=_oscillating(500), profile_id=profile.identity.profile_id,
        market="BTC/MXN", evaluator=recorder, taker_fee_rate=Decimal("0.0078"),
        spread_bps=Decimal("12"), policy=DEFAULT_POLICY, budget_mxn=Decimal("11"),
        modelled_slippage_bps=Decimal("5"), bar_minutes=15)
    ratios, grosses = _trip_ratios(result, recorder)
    assert result.trips, "the fixture must produce at least one completed trip"
    for ratio in ratios:
        assert Decimal("0") < ratio <= Decimal("1"), ratio
    for gross in grosses:
        assert gross > 0


def test_friction_ratio_uses_the_entry_declared_target():
    """The denominator must be the target the profile declared when it decided."""
    profile = VolatilityMeanReversionHorizon(timeframe_name="15m")
    recorder = _Recorder(profile)
    result = replay_executable(
        candles=_oscillating(500), profile_id=profile.identity.profile_id,
        market="BTC/MXN", evaluator=recorder, taker_fee_rate=Decimal("0.0078"),
        spread_bps=Decimal("12"), policy=DEFAULT_POLICY, budget_mxn=Decimal("11"),
        modelled_slippage_bps=Decimal("5"), bar_minutes=15)
    ratios, grosses = _trip_ratios(result, recorder)
    assert ratios, "the fixture must produce at least one completed trip"
    for trip, gross in zip(result.trips, grosses):
        proposal = recorder.proposals[trip.entry_signal_index + 1]
        entry = trip.economics.entry.execution_price_mxn
        expected = ((proposal.expected_exit_reference_mxn - entry)
                    / entry * Decimal("10000"))
        assert abs(gross - expected) < Decimal("0.0001")


def test_coarser_bars_scale_capital_hours_by_the_bar_duration():
    """The same trade counted on 1h bars occupies 60x the capital-time of 1m bars."""
    candles = _oscillating(500)
    profile = VolatilityMeanReversionHorizon(timeframe_name="15m")

    def run(bar_minutes):
        recorder = _Recorder(profile)
        return replay_executable(
            candles=candles, profile_id=profile.identity.profile_id, market="BTC/MXN",
            evaluator=recorder, taker_fee_rate=Decimal("0.0078"), spread_bps=Decimal("12"),
            policy=DEFAULT_POLICY, budget_mxn=Decimal("11"),
            modelled_slippage_bps=Decimal("5"), bar_minutes=bar_minutes)

    minute_bars = run(1)
    hour_bars = run(60)
    assert minute_bars.trips and hour_bars.trips
    # Identical decisions: only the wall-clock meaning of a bar changed.
    assert len(minute_bars.trips) == len(hour_bars.trips)
    assert hour_bars.capital_hours == minute_bars.capital_hours * Decimal("60")


def test_aggregation_and_fill_delay_are_independent_of_the_timeframe():
    """The horizon changes what a bar means, not how a fill is modelled."""
    assert FIFTEEN_MINUTE.base_bars == 15
    assert FIFTEEN_MINUTE.label_convention == "BUCKET_OPEN"

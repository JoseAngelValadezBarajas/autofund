from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

from autofund.market.models import QualityStatus, Severity, SourceItem, SourceNotice
from autofund.market.quality import SequenceValidator
from autofund.market.session import ObservationPipeline


def shifted(event, minutes):
    delta = timedelta(minutes=minutes)
    return replace(
        event,
        open_time=event.open_time + delta,
        close_time=event.close_time + delta,
        source_event_time=event.source_event_time + delta,
    )


def test_100_partials_and_one_close_exactly_once(info, events, fake_clock):
    pipeline = ObservationPipeline(info, "1m")
    for _ in range(100):
        pipeline.feed(SourceItem(fake_clock.now(), events[0]))
    assert pipeline.decisions == []
    pipeline.feed(SourceItem(fake_clock.now(), events[1]))
    assert len(pipeline.events) == 101
    assert len(pipeline.candles) == len(pipeline.decisions) == 1
    assert pipeline.validator.report().status is QualityStatus.VALID


def test_duplicate_with_later_emission_is_harmless(info, events):
    validator = SequenceValidator(info.market, "1m")
    assert validator.accept(events[1])
    assert not validator.accept(
        replace(
            events[1],
            source_event_time=events[1].source_event_time + timedelta(seconds=1),
        )
    )
    assert validator.report().duplicates == 1
    assert validator.report().status is QualityStatus.VALID


def test_conflicting_closed_content_invalidates(info, events):
    validator = SequenceValidator(info.market, "1m")
    validator.accept(events[1])
    assert not validator.accept(replace(events[1], close=D("1000001")))
    assert validator.report().status is QualityStatus.INVALID
    assert not validator.accept(events[3])


def test_gap_without_imputation(info, events):
    validator = SequenceValidator(info.market, "1m")
    assert validator.accept(events[1])
    assert validator.accept(events[3])
    assert validator.accept(shifted(events[3], 2))
    report = validator.report()
    assert report.gaps == 1
    assert report.gap_events[0].missing_candles == 1
    assert report.gap_events[0].expected_open == events[1].open_time + timedelta(
        minutes=2
    )
    assert report.closed_candles == 3
    assert report.status is QualityStatus.DEGRADED


def test_out_of_order_and_event_time_regression(info, events):
    validator = SequenceValidator(info.market, "1m")
    validator.accept(events[3])
    assert not validator.accept(events[1])
    assert validator.report().out_of_order == 1
    assert validator.report().status is QualityStatus.INVALID
    validator = SequenceValidator(info.market, "1m")
    validator.accept(
        replace(
            events[0],
            source_event_time=events[0].source_event_time + timedelta(seconds=5),
        )
    )
    assert not validator.accept(events[0])
    assert validator.report().out_of_order == 1


def test_wrong_stream_is_fatal_even_for_recorded_events(info, events):
    validator = SequenceValidator(info.market, "1m")
    assert not validator.accept(replace(events[1], market="ETH/MXN"))
    assert validator.report().status is QualityStatus.INVALID


def test_late_partial_never_redelivered(info, events):
    validator = SequenceValidator(info.market, "1m")
    validator.accept(events[1])
    assert not validator.accept(events[0])
    assert validator.report().out_of_order == 1
    assert validator.report().closed_candles == 1


def test_invalid_payload_budget(info):
    validator = SequenceValidator(info.market, "1m", 3)
    for _ in range(2):
        validator.notice(SourceNotice("invalid_message", Severity.WARNING, "malformed"))
    assert not validator.invalid
    validator.notice(SourceNotice("invalid_message", Severity.WARNING, "malformed"))
    assert validator.report().invalid_messages == 3
    assert validator.invalid

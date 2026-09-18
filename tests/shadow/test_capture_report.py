from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from autofund.observer.errors import MarketDataInvalid
from autofund.replay.serialization import canonical_json
from autofund.shadow.capture import ShadowCapture, read_capture, replay, restore_session
from autofund.shadow.reporting import aggregate_reports, session_report
from autofund.shadow.session import ShadowSession


def write_session(path, session, frames):
    capture = ShadowCapture(path, session)
    try:
        for frame in frames:
            capture.consume(frame)
        return capture.finalize("closed_candles")
    finally:
        capture.close()


def test_golden_H_exact_live_path_replay(session, frames, tmp_path):
    result = write_session(tmp_path, session, frames)
    offline = replay(tmp_path)
    assert canonical_json(result) == canonical_json(offline)
    assert len(offline["executions"]) == 2
    assert offline["metrics"]["final_equity"] == D("49.78239602")


def test_restart_reconstruction_keeps_strategy_pending_inventory(
    session, frames, tmp_path
):
    capture = ShadowCapture(tmp_path, session)
    for frame in frames[:4]:
        capture.consume(frame)
    capture.finalize("interrupted")
    capture.close()
    header, _, _ = read_capture(tmp_path)
    resumed = ShadowCapture(tmp_path, restore_session(header), resume=True)
    assert len(resumed.session.pending) == 1
    assert resumed.session.wallet.positions["BTC/MXN"].quantity > D("0")
    for frame in frames[4:]:
        resumed.consume(frame)
    result = resumed.finalize("closed_candles")
    resumed.close()
    assert canonical_json(replay(tmp_path)) == canonical_json(result)


def test_crash_after_input_before_derived_journal(
    session, frames, tmp_path, monkeypatch
):
    capture = ShadowCapture(tmp_path, session)
    original = capture._append

    def crash(name, value):
        if name == "shadow_journal.jsonl":
            raise KeyboardInterrupt
        original(name, value)

    monkeypatch.setattr(capture, "_append", crash)
    with pytest.raises(KeyboardInterrupt):
        capture.consume(frames[0])
    capture.close()
    header, _, _ = read_capture(tmp_path, verify_manifest=False)
    resumed = ShadowCapture(tmp_path, restore_session(header), resume=True)
    assert resumed.session.frames == 1 and len(resumed.session.candles) == 1
    resumed.finalize("reconstructed")
    resumed.close()
    assert replay(tmp_path)["quality"] == "VALID"


@pytest.mark.parametrize(
    "filename", ["market.jsonl", "shadow_journal.jsonl", "report.json", "session.jsonl"]
)
def test_capture_tampering_fails(session, frames, tmp_path, filename):
    write_session(tmp_path, session, frames)
    path = tmp_path / filename
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(MarketDataInvalid):
        replay(tmp_path)


def test_duplicate_writer_rejected(session, tmp_path):
    first = ShadowCapture(tmp_path, session)
    try:
        with pytest.raises(MarketDataInvalid):
            ShadowCapture(tmp_path, session, resume=True)
    finally:
        first.close()


def test_report_private_data_absent_metrics_correct(session, frames, tmp_path):
    write_session(tmp_path, session, frames)
    result = session_report(tmp_path)
    assert result["parity"] == "PASS"
    assert result["signals"] == 2 and result["shadow_fills"] == 2
    assert result["metrics"]["closed_trades"] == 1 and result["metrics"]["losses"] == 1
    content = canonical_json(result).lower()
    assert all(
        s not in content for s in ("api_key", "authorization", "real_exchange_balances")
    )


def test_weekly_aggregate_independent_allocations(session, frames, tmp_path):
    write_session(tmp_path / "one", session, frames)
    offset = timedelta(hours=1)
    other = ShadowSession(
        session.config, session.limits, session.fee, session.start + offset
    )
    shifted = [
        replace(
            f,
            observed_at=f.observed_at + offset,
            trades=tuple(replace(t, timestamp=t.timestamp + offset) for t in f.trades),
            depth=replace(f.depth, timestamp=f.depth.timestamp + offset),
        )
        for f in frames
    ]
    write_session(tmp_path / "two", other, shifted)
    report = aggregate_reports(tmp_path, "weekly")
    assert report["groups"][0]["allocated_total"] == D("100")
    assert report["groups"][0]["net_pnl_sum"] == D("-0.43520796")
    assert "not compounded" in report["groups"][0]["accounting"]


def test_incompatible_aggregation_rejected(session, frames, tmp_path):
    write_session(tmp_path / "one", session, frames)
    other = ShadowSession(
        replace(session.config, max_spread_bps=D("200")),
        session.limits,
        session.fee,
        session.start,
    )
    write_session(tmp_path / "two", other, frames)
    with pytest.raises(MarketDataInvalid, match="incompatible"):
        aggregate_reports(tmp_path)


def test_irrelevant_receive_latency_excluded(session, frames):
    other = ShadowSession(session.config, session.limits, session.fee, session.start)
    for frame in frames:
        session.process(frame)
        other.process(
            replace(frame, observed_at=frame.observed_at + timedelta(microseconds=1))
        )
    assert (
        session.result()["result_fingerprint"] == other.result()["result_fingerprint"]
    )

import json
from datetime import timedelta

import pytest

from autofund.observer.errors import MarketDataInvalid
from autofund.observer.source import MarketNotice
from autofund.replay.serialization import canonical_json
from autofund.shadow.capture import ShadowCapture, replay
from autofund.shadow.session import ShadowHealth


def test_operational_network_failures_are_replayed(session, frames, tmp_path):
    capture = ShadowCapture(tmp_path, session)
    capture.consume(frames[0])
    capture.consume(
        MarketNotice(frames[0].observed_at + timedelta(seconds=1), "read_unavailable")
    )
    capture.consume(frames[1])
    capture.finalize("interrupted")
    capture.close()
    result = replay(tmp_path)
    assert result["quality"] == "DEGRADED"
    assert result["operational_issues"]["read_unavailable"] == 1


def test_invalid_payload_notice_preserves_partial_report(session, frames, tmp_path):
    capture = ShadowCapture(tmp_path, session)
    capture.consume(frames[0])
    capture.consume(MarketNotice(frames[1].observed_at, "invalid_payload", "INVALID"))
    capture.finalize("MARKET_DATA_HALT")
    capture.close()
    assert session.health is ShadowHealth.HALTED
    assert replay(tmp_path)["benchmark_candidate"] is False


def test_ctrl_c_finalization_reconstructs_input_tail(
    session, frames, tmp_path, monkeypatch
):
    capture = ShadowCapture(tmp_path, session)
    original = capture._append
    failed = [False]

    def interrupted(name, value):
        if name == "shadow_journal.jsonl" and not failed[0]:
            failed[0] = True
            raise KeyboardInterrupt
        original(name, value)

    monkeypatch.setattr(capture, "_append", interrupted)
    with pytest.raises(KeyboardInterrupt):
        capture.consume(frames[0])
    result = capture.finalize("interrupted")
    capture.close()
    assert canonical_json(result) == canonical_json(replay(tmp_path))


def test_manifest_count_tamper_rejected(session, frames, tmp_path):
    capture = ShadowCapture(tmp_path, session)
    for frame in frames:
        capture.consume(frame)
    capture.finalize("done")
    capture.close()
    path = tmp_path / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["frames"] = 1000
    path.write_text(json.dumps(manifest))
    with pytest.raises(MarketDataInvalid, match="counts"):
        replay(tmp_path)

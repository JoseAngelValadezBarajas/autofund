import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, replace
from decimal import ROUND_UP, localcontext
from decimal import Decimal as D
from pathlib import Path

import pytest

from autofund.replay import (
    ReplayConfig,
    ReplayRunner,
    SimpleMeanReversionV0,
    fingerprint,
    load_csv,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.parametrize(
    "fixture", ["flat_market", "uptrend_market", "mean_reversion_market"]
)
def test_two_identical_runs_are_exactly_equal(fixture):
    data = load_csv(FIXTURES / f"{fixture}.csv", market="BTC/MXN")
    strategy = SimpleMeanReversionV0()
    runner = ReplayRunner()
    first = runner.run(dataset=data, strategy=strategy)
    second = runner.run(dataset=data, strategy=strategy)
    assert first.trace.signals == second.trace.signals
    assert first.trace.trades == second.trace.trades
    assert first.trace.ledger == second.trace.ledger
    assert first.trace.equity_curve == second.trace.equity_curve
    assert first.metrics == second.metrics
    assert first.result_fingerprint == second.result_fingerprint
    assert first == second
    assert first.to_json() == second.to_json()
    assert first.metrics.initial_equity_mxn == D("50")
    assert isinstance(first.metrics.final_equity_mxn, D)
    if fixture in ("flat_market", "uptrend_market"):
        assert first.metrics.fill_count == first.metrics.closed_trade_count == 0
        assert first.metrics.final_equity_mxn == D("50")
    else:
        assert first.metrics.closed_trade_count == 1


def test_context_does_not_change_result_or_leak(mean_dataset):
    runner = ReplayRunner()
    strategy = SimpleMeanReversionV0()
    expected = runner.run(dataset=mean_dataset, strategy=strategy)
    with localcontext() as context:
        context.prec = 7
        context.rounding = ROUND_UP
        result = runner.run(dataset=mean_dataset, strategy=strategy)
        assert context.prec == 7
        assert context.rounding == ROUND_UP
        assert result == expected
        assert result.to_json() == expected.to_json()


def test_result_is_deeply_immutable(mean_dataset):
    result = ReplayRunner().run(dataset=mean_dataset, strategy=SimpleMeanReversionV0())
    with pytest.raises(FrozenInstanceError):
        result.metrics.final_equity_mxn = D("999")
    with pytest.raises(FrozenInstanceError):
        result.trace.trades[0].fill.quantity = D("999")
    with pytest.raises(FrozenInstanceError):
        result.trace.ledger[0].cash_delta_mxn = D("999")
    with pytest.raises(FrozenInstanceError):
        result.strategy.parameters = ()
    assert isinstance(result.trace.equity_curve, tuple)


def test_export_contains_full_audit_and_hash_can_be_recomputed(tmp_path, mean_dataset):
    result = ReplayRunner().run(dataset=mean_dataset, strategy=SimpleMeanReversionV0())
    output = tmp_path / "artifact.json"
    text = result.to_json(output)
    assert output.read_text(encoding="utf-8") == text + "\n"
    payload = json.loads(text)
    assert payload["dataset"]["candles"]
    assert payload["config"]["fee_rate"] == "0.001"
    assert payload["metrics"]["initial_equity_mxn"] == "50"
    assert payload["trace"]["ledger"][0]["type"] == "DEPOSIT"
    assert payload["candle_count"] == 24
    assert payload["start_timestamp"] == "2026-01-01T00:00:00.000000Z"
    assert str(tmp_path) not in text
    assert "hostname" not in payload and "wall_clock" not in payload
    expected_hash = payload.pop("result_fingerprint")
    payload.pop("run_id")
    assert fingerprint(payload) == expected_hash


def test_input_run_identity_distinct_from_output_fingerprint(mean_dataset):
    first = ReplayRunner().run(dataset=mean_dataset, strategy=SimpleMeanReversionV0())
    # Same inputs, deliberately altered output models: the output hash detects the change.
    modified = replace(first, metrics=replace(first.metrics, final_equity_mxn=D("999")))
    assert modified.run_id == first.run_id
    assert modified.result_fingerprint != first.result_fingerprint
    assert first.run_id != first.result_fingerprint
    other = ReplayRunner(replace(first.config, fee_rate=D("0.002"))).run(
        dataset=mean_dataset, strategy=SimpleMeanReversionV0()
    )
    assert other.run_id != first.run_id
    assert other.config_fingerprint != first.config_fingerprint
    assert other.result_fingerprint != first.result_fingerprint


def test_distinct_processes_hash_seeds_paths_and_timezones(tmp_path):
    source = (FIXTURES / "mean_reversion_market.csv").read_bytes()
    program = """from autofund.replay import load_csv, ReplayRunner, SimpleMeanReversionV0
import sys
data = load_csv(sys.argv[1], market='BTC/MXN')
print(ReplayRunner().run(dataset=data, strategy=SimpleMeanReversionV0()).to_json())
"""
    outputs = []
    for seed, timezone in (("1", "America/Mexico_City"), ("97", "UTC")):
        path = tmp_path / f"copy_{seed}.csv"
        path.write_bytes(source)
        env = dict(os.environ, PYTHONHASHSEED=seed, TZ=timezone)
        outputs.append(
            subprocess.check_output(
                [sys.executable, "-c", program, str(path)],
                env=env,
                cwd=tmp_path,
                text=True,
            )
        )
    assert outputs[0] == outputs[1]


def test_f1_golden_replay(mean_dataset):
    expected = json.loads(
        (FIXTURES / "golden_mean_reversion.json").read_text(encoding="utf-8")
    )
    config = ReplayConfig(fee_rate=D("0.01"), slippage_bps=D("100"))
    strategy = SimpleMeanReversionV0(allocation_fraction=D("0.20402"))
    result = ReplayRunner(config).run(dataset=mean_dataset, strategy=strategy)
    assert result.metrics.final_equity_mxn == D(expected["final_equity_mxn"])
    assert result.metrics.closed_trade_count == expected["closed_trade_count"]
    assert result.metrics.fill_count == expected["fill_count"]
    assert result.metrics.total_fees_mxn == D(expected["total_fees_mxn"])
    assert result.metrics.ledger_entry_count == expected["ledger_entry_count"]
    assert result.dataset_fingerprint == expected["dataset_fingerprint"]
    assert result.config_fingerprint == expected["config_fingerprint"]
    assert result.strategy_fingerprint == expected["strategy_fingerprint"]
    assert result.result_fingerprint == expected["result_fingerprint"]

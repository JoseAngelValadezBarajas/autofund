import json
import random
from datetime import date, timedelta
from decimal import Decimal as D

import pytest

from autofund.mvp import daily_momentum as dm


def registration(tmp_path, **over):
    base = {
        "id": "T", "symbols": ["AAA", "BBB"], "adjustment": "all", "feed": "iex",
        "start": "2020-01-01", "dev_end": "2021-12-31", "holdout_start": "2022-01-03",
        "end": "2023-12-31", "lookbacks": [10, 20, 40], "rebalance_every": 5,
        "cost_bps_per_side": "5", "control_permutations": 199, "seed": 7, "alpha": "0.05",
        "min_holdout_days": 300,
    }
    base.update(over)
    path = tmp_path / "reg.json"
    path.write_text(json.dumps(base))
    return dm.load_registration(path)


def weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def panel(daily_returns: dict[str, list[D]], start=date(2020, 1, 1)) -> dm.Panel:
    n = len(next(iter(daily_returns.values())))
    days = weekdays(start, n + 1)
    closes = {}
    for sym, rets in daily_returns.items():
        level, series = D(100), [D(100)]
        for r in rets:
            level *= 1 + r
            series.append(level)
        closes[sym] = tuple(series)
    return dm.Panel(tuple(days), closes)


def trending(n=1040, seed=1, drift="0.004", noise="0.002"):
    """A persistent up-then-down regime series: trend-following has real, timeable signal here."""
    rng = random.Random(seed)
    out, sign = [], 1
    for i in range(n):
        if i % 60 == 0:
            sign = 1 if (i // 60) % 2 == 0 else -1
        out.append(sign * D(drift) + D(str(round(rng.uniform(-1, 1), 4))) * D(noise))
    return out


def noise(n=1040, seed=2):
    rng = random.Random(seed)
    return [D(str(round(rng.uniform(-0.01, 0.01), 5))) for _ in range(n)]


# ---- registration ----

def test_registration_is_hashed_and_validated(tmp_path):
    reg = registration(tmp_path)
    assert len(reg.sha256) == 64 and reg.trials == 3
    with pytest.raises(dm.RegistrationError):
        registration(tmp_path, dev_end="2019-01-01")
    with pytest.raises(dm.RegistrationError):
        registration(tmp_path, control_permutations=5)
    (tmp_path / "bad.json").write_text("{}")
    with pytest.raises(dm.RegistrationError):
        dm.load_registration(tmp_path / "bad.json")


def test_hash_changes_when_the_file_changes(tmp_path):
    assert registration(tmp_path).sha256 != registration(tmp_path, seed=8).sha256


# ---- mechanics ----

def test_positions_are_flat_before_warmup_and_held_between_rebalances():
    closes = tuple(D(100 + i) for i in range(30))
    held = dm.positions(closes, lookback=10, every=5)
    assert held[:10] == (0,) * 10
    assert held[10] == 1 and set(held[10:]) == {1}


def test_no_lookahead_a_decision_earns_only_the_next_days_return():
    # Price jumps +10% on day 3 only. A position decided at the close of day 3 must not earn it.
    closes = {"A": (D(100), D(100), D(100), D(110), D(110), D(110))}
    book = {"A": (0, 0, 0, 1, 1, 1)}
    r = dm.net_returns(closes, book, D(0), 1, 6)
    assert r[2] == 0  # day 3: position decided at close of day 2 was 0
    assert all(x == 0 for x in r)
    book = {"A": (0, 0, 1, 1, 1, 1)}
    assert dm.net_returns(closes, book, D(0), 1, 6)[2] == D("0.1")


def test_costs_are_charged_per_side_on_every_change():
    closes = {"A": (D(100),) * 6}
    book = {"A": (0, 1, 1, 0, 0, 0)}
    r = dm.net_returns(closes, book, D(10), 2, 6)  # 10 bps per side
    assert sum(r) == D("-0.002")  # one entry and one exit


def test_metrics():
    assert dm.sharpe((D("0.01"),)) == 0
    assert dm.sharpe((D("0.01"),) * 5) == 0  # zero variance
    assert dm.total_return((D("0.1"), D("-0.1"))) == D("-0.01")
    assert dm.max_drawdown((D("0.5"), D("-0.5"), D("0.1"))) == D("-0.5")


def test_control_p_value_is_bounded_and_deterministic(tmp_path):
    reg = registration(tmp_path)
    p = panel({"AAA": trending(), "BBB": trending(seed=3)})
    book = {s: dm.positions(c, 20, reg.rebalance_every) for s, c in p.closes.items()}
    lo, hi = 400, 700
    observed = dm.sharpe(dm.net_returns(p.closes, book, reg.cost_bps_per_side, lo, hi))
    a = dm.control_p_value(p.closes, book, reg, lo, hi, observed)
    b = dm.control_p_value(p.closes, book, reg, lo, hi, observed)
    assert a == b and D(1) / 200 <= a <= 1


# ---- end-to-end verdicts ----

def test_requires_the_registration_to_be_committed(tmp_path):
    reg = registration(tmp_path)
    with pytest.raises(dm.RegistrationError, match="committed"):
        dm.evaluate(panel({"AAA": noise(), "BBB": noise(seed=3)}), reg, registration_committed=False)


def test_pure_noise_is_no_trade(tmp_path):
    reg = registration(tmp_path)
    out = dm.evaluate(panel({"AAA": noise(), "BBB": noise(seed=3)}), reg, registration_committed=True)
    assert out["decision"] == dm.NO_TRADE
    assert out["holdout_evaluations"] == 1 and out["trials_counted"] == 3
    assert out["registration_sha256"] == reg.sha256


def test_every_trial_is_reported_and_selection_uses_development_only(tmp_path):
    reg = registration(tmp_path)
    out = dm.evaluate(panel({"AAA": trending(), "BBB": trending(seed=3)}), reg,
                      registration_committed=True)
    assert [t["lookback"] for t in out["development_trials"]] == [10, 20, 40]
    assert all("holdout" not in t for t in out["development_trials"])
    assert out["selected_lookback"] in (10, 20, 40)
    assert set(out["gates"]) >= {"timing_control_p_below_adjusted_alpha",
                                 "holdout_beats_benchmark_sharpe"}


def test_buy_and_hold_drift_does_not_pass_as_timing_skill(tmp_path):
    # A pure uptrend: the strategy is just buy-and-hold minus costs, so it must not beat it.
    reg = registration(tmp_path)
    up = [D("0.001")] * 1040
    out = dm.evaluate(panel({"AAA": up, "BBB": up}), reg, registration_committed=True)
    assert out["decision"] == dm.NO_TRADE
    assert out["gates"]["holdout_beats_benchmark_sharpe"] is False


def test_short_holdout_is_refused(tmp_path):
    reg = registration(tmp_path, min_holdout_days=900)
    with pytest.raises(dm.RegistrationError, match="holdout"):
        dm.evaluate(panel({"AAA": noise(), "BBB": noise(seed=3)}), reg, registration_committed=True)


def test_a_genuinely_timeable_regime_series_can_pass(tmp_path):
    # Proves the gates are not unsatisfiable: persistent regimes reward trend-following.
    reg = registration(tmp_path)
    out = dm.evaluate(panel({"AAA": trending(), "BBB": trending(seed=3)}), reg,
                      registration_committed=True)
    assert out["decision"] == dm.ELIGIBLE_FOR_PAPER and all(out["gates"].values())


def test_prior_trials_tighten_the_threshold(tmp_path):
    fresh = registration(tmp_path)
    later = registration(tmp_path, prior_trials=3)
    assert (fresh.trials, later.trials) == (3, 6)
    out = dm.evaluate(panel({"AAA": noise(), "BBB": noise(seed=3)}), later, registration_committed=True)
    assert out["trials_counted"] == 6 and out["prior_trials"] == 3
    assert out["timing_control"]["adjusted_alpha"] == "0.0083"
    with pytest.raises(dm.RegistrationError):
        registration(tmp_path, prior_trials=-1)

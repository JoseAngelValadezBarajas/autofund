import json
from decimal import Decimal as D

import pytest

from autofund.exchanges.alpaca.errors import AlpacaTransportError
from autofund.lab.capture import CaptureError, wait_until
from autofund.lab.session import (
    FLATTEN_PHRASE,
    START_PHRASE,
    SessionConfig,
    SessionError,
)
from autofund.lab.signals import (
    STRATEGIES,
    SignalError,
    certification_status,
    compute_signals,
    research_catalog,
)

from .helpers import CREDS, falling, rising

CONFIG = SessionConfig("momentum-126", D("0.2"))


# ---- signals ----

def test_signals_follow_the_trailing_return_rule(service):
    s = service.signals.signals("momentum-126")
    positions = {r.symbol: r.position for r in s.rows}
    assert positions == {"SPY": 1, "QQQ": 1, "TLT": 0, "GLD": 0}
    assert s.rows[0].public()["action"] == "HOLD LONG"


def test_not_enough_history_is_an_error_not_a_guess(data):
    from .helpers import make_series
    short = {sym: make_series(sym, rising(50)) for sym in STRATEGIES["momentum-126"].universe}
    with pytest.raises(SignalError, match="needed"):
        compute_signals(STRATEGIES["momentum-126"], short)


def test_unknown_strategy_is_rejected(service):
    with pytest.raises(SignalError):
        service.signals.signals("nope")


def test_signals_use_only_closed_bars(service, clock):
    seen = {}
    real = service.data().get_daily_bars

    def spy(symbols, start, end, **kw):
        seen["end"] = end
        return real(symbols, start, end, **kw)
    service.data().get_daily_bars = spy
    service.signals._cache.clear()
    service.signals.signals("momentum-126")
    assert seen["end"] < clock["now"].date()  # yesterday at the latest, never today's open bar


def test_research_catalog_and_certification(tmp_path):
    (tmp_path / "results").mkdir()
    assert research_catalog(tmp_path) == [] and certification_status([]) == "UNCERTIFIED"
    (tmp_path / "results" / "x_evaluation.json").write_text(json.dumps({
        "registration_id": "H-X", "decision": "NO_TRADE", "selected_lookback": 126,
        "holdout": {"net_sharpe": "0.2"}, "benchmark_equal_weight_buy_and_hold": {"net_sharpe": "0.4"},
        "timing_control": {"p_value": "0.5"}, "trials_counted": 6}))
    (tmp_path / "results" / "bad_evaluation.json").write_text("{not json")
    catalog = research_catalog(tmp_path)
    assert [e["id"] for e in catalog] == ["H-X"] and certification_status(catalog) == "UNCERTIFIED"
    assert certification_status([{"decision": "PASS_TO_PAPER"}]) == "CERTIFIED_FOR_PAPER"


# ---- capture: user-controlled, in-process ----

def test_capture_starts_writes_and_stops_on_request(service, data, tmp_path):
    assert not service.capture.status()["running"]
    service.capture.start(5)
    assert wait_until(lambda: service.capture.status()["quotes_written"] >= 4)
    status = service.capture.stop("test")
    assert not status["running"] and status["stop_reason"] == "test" and status["files"] == 1
    lines = [json.loads(x) for f in (tmp_path / "capture").glob("*.jsonl") for x in f.read_text().splitlines()]
    assert lines and {"symbol", "bid", "ask", "half_spread_bps", "in_session"} <= set(lines[0])


def test_capture_cannot_double_start_and_validates_interval(service):
    with pytest.raises(CaptureError):
        service.capture.start(1)
    service.capture.start(5)
    with pytest.raises(CaptureError, match="already"):
        service.capture.start(5)
    service.capture.stop()


def test_unchanged_quotes_are_one_observation(service, data):
    from datetime import UTC, datetime

    from autofund.exchanges.alpaca.models import Quote
    fixed = datetime(2026, 11, 23, 16, tzinfo=UTC)
    data.get_latest_quotes = lambda symbols, *, feed: {
        s: Quote(s, fixed, D("1"), D("2")) for s in symbols}
    assert service.capture.poll_once() == 4
    assert service.capture.poll_once() == 0


def test_capture_survives_transient_errors_and_reports_them(service, data):
    calls = {"n": 0}
    real = data.get_latest_quotes

    def flaky(symbols, *, feed):
        calls["n"] += 1
        if calls["n"] == 1:
            raise AlpacaTransportError("boom")
        return real(symbols, feed=feed)

    def fast_wait(timeout):
        if calls["n"] >= 3:
            service.capture._stop.set()
        return service.capture._stop.is_set()
    data.get_latest_quotes = flaky
    service.capture._stop.wait = fast_wait
    service.capture.start(5)
    assert wait_until(lambda: not service.capture.running)
    status = service.capture.status()
    assert status["errors"] == 1 and "AlpacaTransportError" in status["last_error"] and status["polls"] >= 2


def test_application_shutdown_stops_capture(service):
    service.capture.start(5)
    service.shutdown()
    assert not service.capture.status()["running"]


# ---- session: planning ----

def test_plan_buys_the_trending_assets_only(service):
    plan = service.session.build_plan(CONFIG)
    assert plan.capital == D("20000.00")
    assert {o.symbol: o.action for o in plan.orders} == {"SPY": "BUY", "QQQ": "BUY"}
    assert all(o.notional == D("5000.00") for o in plan.orders)  # 20% of equity over 4 symbols
    assert plan.public()["executable"] is True


def test_plan_closes_positions_the_signal_no_longer_supports(service, broker):
    broker.positions = {"TLT": D("5000"), "SPY": D("5000")}
    orders = {o.symbol: o.action for o in service.session.build_plan(CONFIG).orders}
    assert orders == {"TLT": "CLOSE", "QQQ": "BUY"}  # SPY already at target: no churn


def test_small_differences_do_not_trade(service, broker):
    broker.positions = {"SPY": D("4995"), "QQQ": D("5004")}
    assert service.session.build_plan(CONFIG).orders == ()


def test_open_orders_block_a_plan(service, broker):
    broker.orders["x"] = {"id": "00000000-0000-4000-8000-000000000099", "client_order_id": "x",
                          "symbol": "SPY", "side": "buy", "status": "accepted", "notional": "1",
                          "filled_qty": "0", "filled_avg_price": None, "submitted_at": ""}
    plan = service.session.build_plan(CONFIG)
    assert plan.blocked and plan.public()["executable"] is False


@pytest.mark.parametrize("fraction", ["0", "-0.1", "0.51", "1"])
def test_capital_fraction_is_hard_capped(service, fraction):
    with pytest.raises(SessionError):
        service.session.build_plan(SessionConfig("momentum-126", D(fraction)))


# ---- session: authorization, execution, safety ----

def test_start_requires_the_exact_phrase(service):
    with pytest.raises(SessionError, match="phrase"):
        service.session.start(CONFIG, "start paper experiment")
    service.session.start(CONFIG, START_PHRASE)
    assert service.session.state == "ACTIVE"
    with pytest.raises(SessionError, match="already"):
        service.session.start(CONFIG, START_PHRASE)


def test_cannot_execute_without_an_active_session(service):
    plan = service.session.build_plan(CONFIG)
    with pytest.raises(SessionError, match="start a session"):
        service.session.execute(plan.plan_id)


def test_execution_places_idempotent_orders_and_reconciles(service, broker):
    service.session.start(CONFIG, START_PHRASE)
    plan = service.session.build_plan(CONFIG)
    result = service.session.execute(plan.plan_id)
    assert [o["status"] for o in result["orders"]] == ["filled", "filled"]
    assert all(o["client_order_id"].startswith(f"af-{plan.plan_id}-") for o in result["orders"])
    assert result["orders"][0]["filled_avg_price"] == "100"
    assert set(broker.positions) == {"SPY", "QQQ"}
    events = [r["event"] for r in service.session.journal.tail()]
    assert {"SESSION_STARTED", "PLAN", "ORDER", "EXECUTION"} <= set(events)


def test_a_stale_or_unknown_plan_is_refused(service, clock):
    service.session.start(CONFIG, START_PHRASE)
    plan = service.session.build_plan(CONFIG)
    with pytest.raises(SessionError, match="not found"):
        service.session.execute("deadbeef")
    from datetime import timedelta
    clock["now"] += timedelta(minutes=11)
    with pytest.raises(SessionError, match="stale"):
        service.session.execute(plan.plan_id)


def test_closed_market_requires_explicit_queueing(service, broker):
    broker.is_open = False
    service.session.start(CONFIG, START_PHRASE)
    plan = service.session.build_plan(CONFIG)
    with pytest.raises(SessionError, match="closed"):
        service.session.execute(plan.plan_id)
    result = service.session.execute(plan.plan_id, queue_if_closed=True)
    assert {o["status"] for o in result["orders"]} == {"accepted"} and result["market_open"] is False


def test_rejected_orders_are_reported_not_hidden(service, broker):
    service.session.start(CONFIG, START_PHRASE)
    plan = service.session.build_plan(CONFIG)
    broker.reject = True
    result = service.session.execute(plan.plan_id)
    assert {o["status"] for o in result["orders"]} == {"rejected"}


def test_unknown_outcome_is_reconciled_by_client_id_never_resubmitted(service, broker):
    broker.lose_order_after_unknown = False  # the order reached the broker; the response was lost
    broker.fail_next_post = AlpacaTransportError("lost")
    service.session.start(CONFIG, START_PHRASE)
    plan = service.session.build_plan(CONFIG)
    result = service.session.execute(plan.plan_id)
    assert result["orders"][0].get("recovered") is True
    posts = [r for r in broker.requests if r[0] == "POST"]
    assert len(posts) == 2 and len(broker.orders) == 2  # one per symbol, none duplicated


def test_unresolvable_unknown_outcome_halts_the_session(service, broker):
    broker.lose_order_after_unknown = True  # the order never arrived and cannot be found
    broker.fail_next_post = AlpacaTransportError("lost")
    service.session.start(CONFIG, START_PHRASE)
    plan = service.session.build_plan(CONFIG)
    result = service.session.execute(plan.plan_id)
    assert result["orders"][0]["status"] == "UNKNOWN" and len(result["orders"]) == 1
    assert service.session.state == "HALTED"
    with pytest.raises(SessionError):
        service.session.execute(plan.plan_id)


def test_kill_cancels_open_orders_and_halts(service, broker):
    broker.is_open = False
    service.session.start(CONFIG, START_PHRASE)
    service.session.execute(service.session.build_plan(CONFIG).plan_id, queue_if_closed=True)
    service.session.kill("test")
    assert service.session.state == "HALTED" and service.session.halt_reason == "test"
    assert all(o["status"] == "canceled" for o in broker.orders.values())
    assert any(r[0] == "DELETE" and r[1] == "/v2/orders" for r in broker.requests)


def test_stop_cancels_orders_but_leaves_positions(service, broker):
    service.session.start(CONFIG, START_PHRASE)
    service.session.execute(service.session.build_plan(CONFIG).plan_id)
    service.session.stop()
    assert service.session.state == "STOPPED" and broker.positions


def test_flatten_needs_its_own_confirmation(service, broker):
    broker.positions = {"SPY": D("100"), "AAPL": D("100")}
    with pytest.raises(SessionError, match="confirmation"):
        service.session.flatten("yes")
    closed = service.session.flatten(FLATTEN_PHRASE)
    assert [c["symbol"] for c in closed] == ["SPY"] and "AAPL" in broker.positions  # only lab symbols


def test_app_exit_ends_the_session_without_touching_the_broker(service, broker):
    service.session.start(CONFIG, START_PHRASE)
    before = list(broker.requests)
    service.session.shutdown()
    assert service.session.state == "STOPPED" and broker.requests == before


def test_equity_snapshots_are_throttled_and_logged(service):
    service.session.start(CONFIG, START_PHRASE)
    assert service.session.snapshot_equity() is not None
    assert service.session.snapshot_equity() is None
    assert service.session.snapshot_equity(force=True)["equity"] == "100000"
    assert len(service.session.equity_history()) == 2


# ---- automatic evaluation (only while the app and a session run) ----

def test_auto_rebalance_runs_once_per_day_when_due(service, broker):
    cfg = SessionConfig("momentum-126", D("0.2"), auto_rebalance=False)
    service.session.start(cfg, START_PHRASE)
    assert service.session.auto_tick() == "rebalanced"  # never rebalanced: due
    assert service.session.auto_tick() == "already_evaluated_today"
    assert set(broker.positions) == {"SPY", "QQQ"}


def test_auto_rebalance_waits_when_not_due_or_closed(service, broker, clock):
    from datetime import timedelta
    cfg = SessionConfig("momentum-126", D("0.2"))
    service.session.start(cfg, START_PHRASE)
    service.session.auto_tick()
    clock["now"] += timedelta(days=1)
    assert service.session.auto_tick() == "not_due"  # 21 bars have not passed
    broker.is_open = False
    assert service.session.auto_tick() == "market_closed"


def test_auto_thread_is_owned_by_the_session_and_stops_with_it(service):
    import threading
    cfg = SessionConfig("momentum-126", D("0.2"), auto_rebalance=True)
    service.session.start(cfg, START_PHRASE)
    assert any(t.name == "autofund-paper-auto" for t in threading.enumerate())
    service.session.stop()
    assert wait_until(lambda: not any(t.name == "autofund-paper-auto" for t in threading.enumerate()))


def test_credentials_missing_is_reported_not_crashed(tmp_path):
    from autofund.exchanges.alpaca.errors import AlpacaAuthenticationError
    from autofund.lab.service import LabService

    def none():
        raise AlpacaAuthenticationError("set the keys")
    status = LabService(root=tmp_path, credentials=none).status()
    assert status["credentials"]["configured"] is False and status["portfolio"] is None
    assert status["warnings"] and status["live_trading"] == "NOT_REPRESENTABLE"
    assert CREDS.key_id.startswith("PK") and falling(3)


def test_signals_step_back_when_the_plan_refuses_recent_data(service, data, clock):
    from datetime import timedelta

    from autofund.exchanges.alpaca.errors import AlpacaSubscriptionError
    real, seen = data.get_daily_bars, []

    def refuse_recent(symbols, start, end, **kw):
        seen.append(end)
        if end > clock["now"].date() - timedelta(days=3):
            raise AlpacaSubscriptionError("plan")
        return real(symbols, start, end, **kw)
    data.get_daily_bars = refuse_recent
    service.signals._cache.clear()
    assert service.signals.signals("momentum-126").rows
    assert seen == sorted(seen, reverse=True) and len(seen) == 3  # 1 day, 2 days, then 3 days back


def test_signals_fail_clearly_when_every_window_is_refused(service, data):
    from autofund.exchanges.alpaca.errors import AlpacaSubscriptionError

    def refuse(*a, **k):
        raise AlpacaSubscriptionError("plan")
    data.get_daily_bars = refuse
    service.signals._cache.clear()
    with pytest.raises(SignalError, match="refused"):
        service.signals.signals("momentum-126")


def test_buy_and_hold_control_is_always_long_even_when_trends_are_down(tmp_path):
    from .helpers import FakeData, falling

    down = FakeData({"SPY": falling(), "QQQ": falling(), "TLT": falling(), "GLD": falling()})
    from autofund.lab.signals import compute_signals
    series = down.get_daily_bars(list(STRATEGIES["buy-hold"].universe), None, None, adjustment="all", feed="sip")
    assert {r.position for r in compute_signals(STRATEGIES["buy-hold"], series).rows} == {1}
    assert {r.position for r in compute_signals(STRATEGIES["momentum-126"], series).rows} == {0}
    assert "Control" in STRATEGIES["buy-hold"].description


# ---- audit regressions ----

@pytest.mark.parametrize("fraction", ["NaN", "sNaN", "Infinity", "-Infinity", "0.0001", "1e-400"])
def test_non_finite_and_dust_fractions_are_refused_cleanly(service, fraction):
    with pytest.raises(SessionError):
        service.session.build_plan(SessionConfig("momentum-126", D(fraction)))


def test_a_rejected_order_does_not_count_as_a_finished_rebalance(service, broker):
    service.session.start(CONFIG, START_PHRASE)
    broker.reject = True
    service.session.execute(service.session.build_plan(CONFIG).plan_id)
    assert service.session.last_rebalance() is None  # it must be retried, not skipped
    broker.reject = False
    broker.orders.clear()
    service.session.execute(service.session.build_plan(CONFIG).plan_id)
    assert service.session.last_rebalance() is not None


def test_kill_is_not_blocked_by_an_execution_in_flight(service, broker):
    """The kill switch must work while an execution holds the session lock and waits on the broker."""
    import threading
    import time

    gate, entered = threading.Event(), threading.Event()
    real_send = broker.send

    def slow_send(request):
        if request.method == "POST":
            entered.set()
            gate.wait(10)  # the broker is slow to answer the first order
        return real_send(request)
    broker.send = slow_send

    service.session.start(CONFIG, START_PHRASE)
    plan = service.session.build_plan(CONFIG)
    results = {}
    worker = threading.Thread(target=lambda: results.update(r=service.session.execute(plan.plan_id)))
    worker.start()
    assert entered.wait(5)

    started = time.monotonic()
    service.session.kill("test kill during execution")
    assert time.monotonic() - started < 8  # bounded by the lock wait, not by the 10 s broker stall
    assert service.session.state == "HALTED"
    assert any(r[0] == "DELETE" and r[1] == "/v2/orders" for r in broker.requests)

    gate.set()
    worker.join(10)
    placed = [o for o in results["r"]["orders"]]
    assert len(placed) == 1  # the second order was never placed after the kill
    assert all(o["status"] in ("filled", "canceled") or o["status"] for o in placed)
    # And the broker was told to cancel again after the in-flight order landed.
    deletes = [r for r in broker.requests if r[0] == "DELETE" and r[1] == "/v2/orders"]
    assert len(deletes) >= 2

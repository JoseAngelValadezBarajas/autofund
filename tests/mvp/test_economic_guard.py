"""MVP 0.1.3: fee-aware economic edge guard.

A strategy decision and a financial decision are not the same thing. The Champion
decides from price structure; the guard decides whether the trade the Champion
proposes is expected to make money after the fees AutoFund actually pays. These
tests pin the behaviour that matters:

* a trade that cannot pay for itself is refused, and the refusal is explained;
* a trade that can pay for itself is admitted, and no earlier;
* no financial intent exists before the economic verdict is PASS;
* a safety exit is never economically gated;
* the guard adds no exchange traffic, so Production GET/POST counts are unchanged;
* the Champion's thresholds and fingerprint are untouched.

The numbers are the real ones. The Champion's SELL target is 20 bps while a round
trip costs roughly two taker fees, so at Production's confirmed 78 bps fee a BUY
round trip is net-negative. The guard refusing it is correct behaviour: the
negative expectancy is the finding, not a defect to hide by loosening strategy.
"""

import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from types import SimpleNamespace
from typing import Any

import pytest
from live.test_execution import FakeTransport, trade

from autofund.decimal_utils import PRECISION
from autofund.exchanges.bitso.accounting import ConfirmedFillAccounting
from autofund.exchanges.bitso.models import ExchangeTradeFill, Side
from autofund.mvp import telemetry
from autofund.mvp.champion import CHAMPION_PARAMETERS, exit_boundary
from autofund.mvp.economics import (
    ADMISSIBLE,
    DEFAULT_POLICY,
    EXIT_REJECT,
    EXPECTED_NET_ENTRY_NEGATIVE,
    PROFIT_TAKING,
    RISK_EXIT,
    EconomicPolicy,
    economic_entry_model,
    economic_exit_model,
)
from autofund.mvp.orchestrator import (
    AutoFundOrchestrator,
    DemoAutonomousRunner,
    ProductionAutonomousRunner,
    SessionConfig,
)
from autofund.observer.models import Level, OrderBookSnapshot
from autofund.wallet import Wallet

D = Decimal
CONFIRMATION = "START AUTOFUND REAL 50"


def financial_context() -> Context:
    """The precision production financial arithmetic actually uses.

    `autofund.decimal_utils.financial` isolates every money computation in a
    50-digit context, so expected values in tests must be computed there too.
    Comparing against default 28-digit arithmetic would fail for reasons that
    have nothing to do with the guard.
    """
    return Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)

# Production's confirmed taker fee, taken from the real account.
PRODUCTION_FEE = D("0.0078")

# A fee low enough that the Champion's own 20 bps exit target clears a round trip.
# Used only where a test needs a genuinely profitable trade to exist; it is never
# used to make the guard accept something the real account could not afford.
VIABLE_FEE = D("0.0001")

# The real reference position. 0.1.3 was specified against it, so it is the
# position the economic model must reproduce.
REAL_QUANTITY = D("0.00000727")
REAL_COST_BASIS = D("10.91486406")
REAL_AVERAGE_COST = REAL_COST_BASIS / REAL_QUANTITY


@pytest.fixture
def production(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_API_KEY", "FAKE")
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_API_SECRET", "FAKE")
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_PERMISSIONS_CONFIRMED", "true")
    transport = FakeTransport()
    monkeypatch.setattr("autofund.live.client.BitsoProductionLiveTransport", lambda: transport)
    runner = ProductionAutonomousRunner(tmp_path / "live.jsonl")
    orchestrator = AutoFundOrchestrator(tmp_path / "artifacts", runner)
    yield orchestrator, runner, transport
    if runner.journal is not None:
        runner.journal.close()


def events(app: AutoFundOrchestrator, name: str) -> list[dict[str, Any]]:
    return [row for row in event_rows(app) if row.get("event") == name]


def event_rows(app: AutoFundOrchestrator) -> list[dict[str, Any]]:
    telemetry_session = app.telemetry
    assert telemetry_session is not None, "the orchestrator always owns a telemetry session"
    return list(telemetry_session.rows)


def event_names(app: AutoFundOrchestrator) -> list[str]:
    return [str(row.get("event")) for row in event_rows(app)]


def open_position(runner: ProductionAutonomousRunner, *, quantity: Decimal,
                  cost_basis_mxn: Decimal, fee: Decimal) -> None:
    """Open a real AutoFund position through the real accounting boundary.

    The position is created by the same `ConfirmedFillAccounting.apply` path a
    confirmed Bitso fill uses, so quantity, cost basis and fee currency come from
    production code rather than being asserted into existence. The buy fee is
    charged in BTC, so it reduces owned quantity exactly as Bitso charges it.
    """
    gross = quantity / (D("1") - fee)
    fill = ExchangeTradeFill(
        trade_id="seed-buy", exchange_order_id="seed-order", origin_id="af-guard-seed",
        book="btc_mxn", side=Side.BUY, major_quantity=gross, minor_value=cost_basis_mxn,
        price=cost_basis_mxn / gross, timestamp=datetime.now(UTC), is_maker=False,
        confirmed_fee=gross * fee, fee_currency="btc")
    runner.execution.accounting.apply(fill)


def book(*, bid: Decimal, ask: Decimal) -> OrderBookSnapshot:
    return OrderBookSnapshot("btc_mxn", datetime.now(UTC), 1,
                             (Level(bid, D("1")),), (Level(ask, D("1")),))


# --------------------------------------------------------------------------- #
# Model economics, against the real reference position.
# --------------------------------------------------------------------------- #

def test_exit_model_reproduces_the_real_production_loss() -> None:
    """A regression in the guard's economics must fail loudly, not subtly."""
    target = exit_boundary(REAL_AVERAGE_COST)
    model = economic_exit_model(book="BTC/MXN", quantity=REAL_QUANTITY,
                                cost_basis_mxn=REAL_COST_BASIS,
                                strategy_exit_price_mxn=target, best_bid_mxn=target,
                                exit_fee_rate=PRODUCTION_FEE)
    # 20 bps of strategy edge cannot pay a 78 bps one-way fee.
    assert model.classification == "BELOW BREAK-EVEN"
    assert model.outcome == EXIT_REJECT
    assert model.expected_net_pnl_at_strategy_exit_mxn < 0
    # Break-even sits ~58.5 bps above the executable bid, far above the 20 bps of
    # strategy edge, so the profit-taking exit cannot repay its own fees.
    assert model.distance_to_break_even_bps > D("50")
    assert model.fee_only_break_even_price_mxn > target


def test_fee_only_break_even_is_cost_basis_divided_by_net_quantity() -> None:
    model = economic_exit_model(book="BTC/MXN", quantity=REAL_QUANTITY,
                                cost_basis_mxn=REAL_COST_BASIS,
                                strategy_exit_price_mxn=REAL_AVERAGE_COST,
                                best_bid_mxn=REAL_AVERAGE_COST, exit_fee_rate=PRODUCTION_FEE)
    with localcontext(financial_context()):
        expected = REAL_COST_BASIS / (REAL_QUANTITY * (D("1") - PRODUCTION_FEE))
    assert model.fee_only_break_even_price_mxn == expected


def test_estimated_break_even_is_never_more_optimistic_than_fee_only() -> None:
    """Crossing spread and slippage can only make break-even worse, never better."""
    model = economic_exit_model(book="BTC/MXN", quantity=REAL_QUANTITY,
                                cost_basis_mxn=REAL_COST_BASIS,
                                strategy_exit_price_mxn=REAL_AVERAGE_COST,
                                best_bid_mxn=REAL_AVERAGE_COST, exit_fee_rate=PRODUCTION_FEE,
                                spread_bps=D("50"), slippage_bps=D("25"))
    assert model.estimated_break_even_price_mxn >= model.fee_only_break_even_price_mxn


def test_risk_exit_is_never_economically_gated() -> None:
    """Safety must always be able to realise a loss; the guard must not block it."""
    model = economic_exit_model(book="BTC/MXN", quantity=REAL_QUANTITY,
                                cost_basis_mxn=REAL_COST_BASIS,
                                strategy_exit_price_mxn=REAL_AVERAGE_COST,
                                best_bid_mxn=REAL_AVERAGE_COST, exit_fee_rate=PRODUCTION_FEE,
                                exit_class=RISK_EXIT)
    assert model.outcome == ADMISSIBLE and model.admissible is True
    assert model.expected_net_pnl_at_strategy_exit_mxn < 0, "the loss is still real"


def test_entry_model_refuses_a_round_trip_that_cannot_pay_for_itself() -> None:
    model = economic_entry_model(book="BTC/MXN", budget_mxn=REAL_COST_BASIS,
                                 buy_price_mxn=REAL_AVERAGE_COST,
                                 target_price_mxn=exit_boundary(REAL_AVERAGE_COST),
                                 buy_fee_rate=PRODUCTION_FEE, sell_fee_rate=PRODUCTION_FEE)
    assert model.admissible is False
    assert model.reason == EXPECTED_NET_ENTRY_NEGATIVE
    assert model.expected_net_pnl_mxn < 0
    assert model.estimated_round_trip_cost_mxn > 0


def test_entry_model_admits_a_round_trip_that_does_clear_costs() -> None:
    model = economic_entry_model(book="BTC/MXN", budget_mxn=REAL_COST_BASIS,
                                 buy_price_mxn=REAL_AVERAGE_COST,
                                 target_price_mxn=REAL_AVERAGE_COST * (D("1") + D("0.03")),
                                 buy_fee_rate=PRODUCTION_FEE, sell_fee_rate=PRODUCTION_FEE)
    assert model.admissible is True and model.outcome == ADMISSIBLE
    assert model.expected_net_pnl_mxn > 0


def test_a_stricter_policy_is_honoured_without_changing_the_model() -> None:
    strict = EconomicPolicy(minimum_net_edge_bps=D("100000"))
    model = economic_entry_model(book="BTC/MXN", budget_mxn=D("10"), buy_price_mxn=D("1000000"),
                                 target_price_mxn=D("1500000"), buy_fee_rate=D("0.001"),
                                 sell_fee_rate=D("0.001"), policy=strict)
    assert model.admissible is False
    assert model.expected_net_pnl_mxn > 0, "the trade is profitable; the policy is stricter"


def test_a_policy_that_could_admit_a_loss_is_rejected() -> None:
    with pytest.raises(ValueError):
        EconomicPolicy(minimum_net_profit_mxn=D("-1"))


def test_default_policy_requires_a_genuinely_positive_result() -> None:
    assert DEFAULT_POLICY.minimum_net_profit_mxn == D("0")
    assert DEFAULT_POLICY.minimum_net_edge_bps == D("0")
    assert DEFAULT_POLICY.public()["version"] == DEFAULT_POLICY.version


def test_economic_checkpoints_are_registered() -> None:
    """An unregistered checkpoint raises at runtime, so registration is a contract."""
    assert {"ECONOMIC_EDGE_EVALUATED", "ECONOMIC_EDGE_PASS", "ECONOMIC_EDGE_REJECT",
            "ECONOMIC_EXIT_EVALUATED", "ECONOMIC_EXIT_PASS",
            "ECONOMIC_EXIT_REJECT"} <= telemetry.CHECKPOINTS


def test_champion_thresholds_are_untouched() -> None:
    """0.1.3 adds admission control; it must not retune the Champion."""
    assert CHAMPION_PARAMETERS.min_history == 3 and CHAMPION_PARAMETERS.max_window == 20
    assert CHAMPION_PARAMETERS.entry_threshold == D("0.001")
    assert CHAMPION_PARAMETERS.exit_threshold == D("0.002")
    with localcontext(financial_context()):
        expected = REAL_AVERAGE_COST * (D("1") + CHAMPION_PARAMETERS.exit_threshold)
    assert exit_boundary(REAL_AVERAGE_COST) == expected


def test_fill_accounting_confirms_the_fee_model_the_guard_assumes() -> None:
    """The guard's fee assumption must match the accounting boundary it models."""
    wallet = Wallet()
    wallet.deposit(D("50"))
    accounting = ConfirmedFillAccounting(wallet)
    gross = D("0.00001")
    fill = ExchangeTradeFill(
        trade_id="t1", exchange_order_id="o1", origin_id="af-test", book="btc_mxn",
        side=Side.BUY, major_quantity=gross, minor_value=D("10"), price=D("1000000"),
        timestamp=datetime.now(UTC), is_maker=False, confirmed_fee=gross * PRODUCTION_FEE,
        fee_currency="btc")
    assert accounting.apply(fill)
    position = wallet.positions["BTC/MXN"]
    # A BTC-denominated buy fee reduces owned quantity; the MXN outlay is untouched.
    assert position.quantity == gross * (D("1") - PRODUCTION_FEE)
    assert position.cost_basis_mxn == D("10")


# --------------------------------------------------------------------------- #
# Orchestrator admission.
# --------------------------------------------------------------------------- #

def test_new_buy_is_refused_at_production_fee_and_creates_no_intent(production) -> None:
    """The headline 0.1.3 behaviour: no negative-expectancy intent is created."""
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        runner.handle_signal("BUY")
        assert transport.posts == 0, "an economically refused BUY must never POST"
        snapshot = app.snapshot()
        assert snapshot["signals"]["economically_rejected"] == 1
        assert snapshot["signals"]["admitted"] == 0
        assert snapshot["position"] is None
        assert not events(app, "ORDER_INTENT_CREATED"), "no intent before an economic PASS"
        rejected = events(app, "ECONOMIC_EDGE_REJECT")
        assert rejected and rejected[0]["reason"] == EXPECTED_NET_ENTRY_NEGATIVE
    finally:
        app.stop()


def test_the_refusal_explains_itself_with_structured_evidence(production) -> None:
    """A refusal must be explained, not silent."""
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        runner.handle_signal("BUY")
        evaluated = events(app, "ECONOMIC_EDGE_EVALUATED")
        assert evaluated, "the refusal must be explained, not silent"
        assert evaluated[0]["side"] == "BUY" and evaluated[0]["admissible"] is False
        assert D(evaluated[0]["expected_net_pnl_mxn"]) < 0
        assert D(evaluated[0]["expected_net_edge_bps"]) < 0
        assert D(evaluated[0]["estimated_round_trip_cost_mxn"]) > 0
        assert D(evaluated[0]["buy_fee_rate"]) == PRODUCTION_FEE
    finally:
        app.stop()


def test_the_economic_verdict_is_recorded_for_both_outcomes(production) -> None:
    """Evaluated-then-decided, so a PASS and a REJECT are both auditable."""
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        runner.handle_signal("BUY")
        assert events(app, "ECONOMIC_EDGE_EVALUATED")
        assert events(app, "ECONOMIC_EDGE_REJECT")
        assert not events(app, "ECONOMIC_EDGE_PASS")
    finally:
        app.stop()


def test_economically_viable_buy_is_admitted_and_posts_exactly_once(production) -> None:
    app, runner, transport = production
    transport.fee = str(VIABLE_FEE)
    app.startup()
    original = runner.execution.submit_authorized

    def fill_then_submit(intent: Any) -> None:
        transport.rows = [trade(intent.origin_id, minor=str(intent.minor_budget),
                                fee=transport.fee)]
        original(intent)

    app.start(SessionConfig(), CONFIRMATION)
    runner.execution.submit_authorized = fill_then_submit
    try:
        runner.handle_signal("BUY")
        assert transport.posts == 1, "an admitted BUY posts exactly once"
        assert app.snapshot()["signals"]["admitted"] == 1
        assert app.snapshot()["signals"]["economically_rejected"] == 0
        assert events(app, "ECONOMIC_EDGE_PASS")
    finally:
        app.stop()


def test_profit_taking_exit_is_refused_when_it_would_realise_a_loss(production) -> None:
    """The strategy exit boundary cannot repay cost basis plus fees, so it is refused."""
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        open_position(runner, quantity=REAL_QUANTITY, cost_basis_mxn=REAL_COST_BASIS,
                      fee=PRODUCTION_FEE)
        before = transport.posts
        runner.handle_signal("SELL")
        assert transport.posts == before, "an unprofitable profit-taking exit must not POST"
        assert app.snapshot()["signals"]["economically_rejected"] == 1
        rejected = events(app, "ECONOMIC_EXIT_REJECT")
        assert rejected and rejected[0]["classification"] == "BELOW BREAK-EVEN"
        assert rejected[0]["exit_class"] == PROFIT_TAKING
    finally:
        app.stop()


def test_profit_taking_exit_is_admitted_when_the_target_clears_break_even(production) -> None:
    """The same exit is admitted when the strategy's own target can repay its fees.

    The profit-taking decision is made on the price the strategy will actually
    attempt to sell at, not on the live bid, so whether it can pay for itself is a
    property of the position's cost basis and the fee paid.
    """
    app, runner, transport = production
    transport.fee = str(VIABLE_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        open_position(runner, quantity=REAL_QUANTITY, cost_basis_mxn=REAL_COST_BASIS,
                      fee=VIABLE_FEE)
        assert runner._economically_admissible("SELL", "correlation") is True
        assert runner._economically_rejected == 0
        assert events(app, "ECONOMIC_EXIT_PASS")
    finally:
        app.stop()


def test_profit_taking_exit_is_a_property_of_cost_basis_and_fee(production) -> None:
    """Production's fee cannot repay break-even from a 20 bps target; a cheap fee can."""
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        open_position(runner, quantity=REAL_QUANTITY, cost_basis_mxn=REAL_COST_BASIS,
                      fee=PRODUCTION_FEE)
        assert runner._economically_admissible("SELL", "correlation") is False
        # The 20 bps target sits below break-even, so it can never be admitted,
        # no matter what the order book currently shows.
        average_cost = REAL_COST_BASIS / REAL_QUANTITY
        with localcontext(financial_context()):
            break_even = REAL_COST_BASIS / (REAL_QUANTITY * (D("1") - PRODUCTION_FEE))
        assert exit_boundary(average_cost) < break_even
    finally:
        app.stop()


def test_economic_refusal_happens_before_any_financial_intent(production) -> None:
    """Ordering is the guarantee: no intent may exist before the economic verdict."""
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        runner.handle_signal("BUY")
        names = event_names(app)
        assert "ECONOMIC_EDGE_EVALUATED" in names
        assert "SIGNAL_GENERATED" not in names, "a refused signal was never admitted"
        for later in ("ORDER_INTENT_CREATED", "ORDER_SUBMITTING", "ORDER_ACKNOWLEDGED"):
            assert later not in names
    finally:
        app.stop()


def test_the_guard_adds_no_exchange_traffic(production) -> None:
    """Economic admission must not add GETs or POSTs: it reuses the preflight."""
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        before = list(transport.calls)
        runner.handle_signal("BUY")
        assert transport.calls == before
    finally:
        app.stop()


def test_missing_economic_evidence_fails_closed(production) -> None:
    """Economics that cannot be established must never admit an intent."""
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        # No validated preflight means no confirmed fee and no executable price, so
        # the economics cannot be established. Failing closed is the only safe
        # outcome: the alternative is an unresearched financial commitment.
        runner.last_preflight = None
        runner.handle_signal("BUY")
        assert transport.posts == 0
        assert app.snapshot()["signals"]["economically_rejected"] == 1
        assert events(app, "ECONOMIC_EDGE_REJECT")
    finally:
        app.stop()


def test_economic_rejections_are_counted_per_session(production) -> None:
    """A restarted session must not inherit the previous session's counters."""
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    runner.handle_signal("BUY")
    assert runner._economically_rejected == 1
    app.stop()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        assert runner._economically_rejected == 0
    finally:
        app.stop()


def test_economic_policy_is_reported_in_the_session_config(production) -> None:
    """Two runs with different policies made different decisions; they must differ."""
    app, _runner, transport = production
    transport.fee = str(VIABLE_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    app.stop()
    report = json.loads((app.artifacts / app.session_id / "report.json").read_text())
    config = report["identity"]["config"]
    assert config["economic_policy"]["version"] == DEFAULT_POLICY.version
    assert config["economic_policy"]["minimum_net_edge_bps"] == "0"


def test_read_model_reports_the_exit_economics_for_an_open_position(production) -> None:
    """The operator must be able to see why a profit-taking exit is refused."""
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        open_position(runner, quantity=REAL_QUANTITY, cost_basis_mxn=REAL_COST_BASIS,
                      fee=PRODUCTION_FEE)
        economics = app.snapshot()["economics"]
        assert economics["position_open"] is True
        assert economics["classification"] == "BELOW BREAK-EVEN"
        for field in ("strategy_exit_price_mxn", "fee_only_break_even_price_mxn",
                      "estimated_break_even_price_mxn", "current_best_bid_mxn",
                      "expected_net_pnl_if_sold_now_mxn",
                      "expected_net_pnl_at_strategy_exit_mxn", "cost_basis_mxn",
                      "owned_quantity", "average_cost_mxn", "policy"):
            assert field in economics, f"{field} must be reported"
        # Break-even sits above the strategy's own target: that gap is the reason
        # the profit-taking exit is currently refused.
        assert D(economics["fee_only_break_even_price_mxn"]) > D(
            economics["strategy_exit_price_mxn"])
        assert D(economics["expected_net_pnl_at_strategy_exit_mxn"]) < 0
    finally:
        app.stop()


def test_read_model_does_not_invent_economics_without_a_position(production) -> None:
    app, _runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        economics = app.snapshot()["economics"]
        assert economics["position_open"] is False
        assert economics["classification"] == "NO_POSITION"
        assert "strategy_exit_price_mxn" not in economics
        assert "expected_net_pnl_if_sold_now_mxn" not in economics
    finally:
        app.stop()


def test_demo_read_model_labels_its_economics_as_demo() -> None:
    """The demo fixture must never be mistaken for Production fee evidence."""
    economics = DemoAutonomousRunner._demo_economics(DemoAutonomousRunner.DEMO_QUANTITY,
                                                     DemoAutonomousRunner.DEMO_COST_BASIS)
    assert economics["demo"] is True and economics["source"] == "DEMO_FIXTURE"
    assert economics["position_open"] is True
    assert economics["classification"] in {"NET-PROFITABLE", "BREAK-EVEN", "BELOW BREAK-EVEN"}
    for field in ("strategy_exit_price_mxn", "fee_only_break_even_price_mxn",
                  "estimated_break_even_price_mxn", "current_best_bid_mxn",
                  "expected_net_pnl_if_sold_now_mxn",
                  "expected_net_pnl_at_strategy_exit_mxn"):
        assert field in economics


def test_demo_runner_never_reports_a_confirmed_account_fee() -> None:
    """Only Production has a confirmed fee, and demo must not claim one.

    `_demo_economics` is a pure projection of the demo fixture, so calling it
    directly cannot be confused with a confirmed-fee Production verdict.
    """
    demo = DemoAutonomousRunner()
    assert demo._taker_fee_rate == D("0"), "demo holds no confirmed account fee"
    # With no position the demo reports no position rather than inventing one.
    assert demo.snapshot()["economics"]["position_open"] is False
    # With the fixture position it reports economics that are explicitly DEMO.
    assert demo._demo_economics(demo.DEMO_QUANTITY, demo.DEMO_COST_BASIS)["demo"] is True


def test_session_limits_are_reported_not_only_accepted(production) -> None:
    """Spec section 17: duration and order limits must be persisted and reported."""
    app, _runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(max_session_duration_seconds=120, max_orders_per_session=7),
              CONFIRMATION)
    try:
        session = app.snapshot()["session"]
        assert session["max_duration_seconds"] == 120
        assert session["max_orders_per_session"] == 7
        assert session["max_session_loss_mxn"] == "10"
    finally:
        app.stop()


def test_the_guard_never_retries_a_refused_signal(production) -> None:
    """A refusal is final: the guard must not retry or cause a later POST."""
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        for _ in range(3):
            runner.handle_signal("BUY")
        assert transport.posts == 0
        assert app.snapshot()["signals"]["economically_rejected"] == 3
    finally:
        app.stop()


def test_guard_uses_the_real_account_fee_from_the_preflight(production) -> None:
    """The guard must use the confirmed account fee, not an invented constant."""
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    assert runner._taker_fee_rate == PRODUCTION_FEE
    app.start(SessionConfig(), CONFIRMATION)
    try:
        assert runner.economic_policy is DEFAULT_POLICY
    finally:
        app.stop()


def test_expected_slippage_is_not_assumed_from_the_tolerance(production) -> None:
    """A tolerance is an upper bound, not a forecast.

    Treating `slippage_tolerance` as expected slippage would double-count friction
    and reject trades that are genuinely viable. Unmodelled friction is covered by
    the explicit `minimum_net_edge_bps` policy buffer instead.
    """
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    assert runner._slippage_bps == D("0")
    assert runner.last_preflight.config.slippage_tolerance == D("0.5")
    app.start(SessionConfig(), CONFIRMATION)
    try:
        runner.handle_signal("BUY")
        evaluated = events(app, "ECONOMIC_EDGE_EVALUATED")
        assert evaluated and D(evaluated[0]["slippage_bps"]) == D("0")
    finally:
        app.stop()


def test_an_unprofitable_exit_is_refused_regardless_of_the_market(production) -> None:
    """The profit-taking verdict follows the strategy's target, not the current bid.

    This is deliberate: the decision is whether the strategy's own exit is worth
    taking, so a temporary spike in the bid cannot silently authorise a loss-making
    profit-taking exit.
    """
    app, runner, transport = production
    transport.fee = str(PRODUCTION_FEE)
    app.startup()
    app.start(SessionConfig(), CONFIRMATION)
    try:
        open_position(runner, quantity=REAL_QUANTITY, cost_basis_mxn=REAL_COST_BASIS,
                      fee=PRODUCTION_FEE)
        with localcontext(financial_context()):
            break_even = REAL_COST_BASIS / (REAL_QUANTITY * (D("1") - PRODUCTION_FEE))
        checked = runner.last_preflight
        rising = SimpleNamespace(depth=book(bid=break_even * D("1.10"), ask=break_even * D("1.11")),
                                 fees=checked.fees, budget=checked.budget,
                                 config=checked.config)
        runner._last_depth = rising.depth
        assert runner._economically_admissible("SELL", "correlation") is False
        assert runner._economically_rejected == 1
    finally:
        app.stop()

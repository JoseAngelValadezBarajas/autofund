"""MVP lifecycle integration with the existing F5 GET-only Production preflight.

Offline only: the F5 transport is replaced by the certified fake used by the live
suite. Nothing here may reach Bitso; explicit lifecycle tests POST only to the fake.
"""

import json
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from live.test_execution import FakeTransport, trade

from autofund.mvp.api import create_mvp_app
from autofund.mvp.app import production_scanner_source
from autofund.mvp.orchestrator import (
    AppState,
    AutoFundOrchestrator,
    DemoAutonomousRunner,
    ProductionAutonomousRunner,
    SessionConfig,
    SessionStartBlocked,
)
from autofund.mvp.scanner import MarketScanner

D = Decimal
CONFIRMATION = "START AUTOFUND REAL 50"
ORDER_BOOK = "GET /api/v3/order_book?book=btc_mxn"
START_BODY = {"confirmation": CONFIRMATION, "max_session_loss_mxn": "10",
              "max_session_duration_seconds": 3600, "max_orders_per_session": 10}


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


def test_startup_preflight_is_get_only_and_reaches_stopped_ready(production):
    app, runner, transport = production
    app.startup()
    assert app.state is AppState.STOPPED and app.auto_execution is False
    readiness = app.snapshot()["production_preflight"]
    assert readiness["ready"] is True and readiness["label"] == "READY"
    assert readiness["production_post_count"] == 0 and readiness["production_get_count"] == 5
    assert runner.last_preflight.ready
    assert transport.posts == 0
    wallet = app.snapshot()["wallet"]
    assert wallet["status"] == "PASS" and wallet["read_only"] is True
    assert {row["currency"] for row in wallet["balances"]} == {"MXN", "BTC"}
    assert {call for call in transport.calls} == {
        "GET /api/v3/balance", "GET /api/v3/fees", "GET /api/v3/available_books", ORDER_BOOK}


def test_start_runs_a_fresh_preflight_and_enables_auto_execution_without_posting(production):
    app, runner, transport = production
    app.startup()
    startup_preflight = runner.last_preflight
    app.start(SessionConfig(), CONFIRMATION)
    try:
        assert app.state is AppState.RUNNING and app.auto_execution is True
        assert transport.posts == 0
        # A start decision never reuses the startup result: the F5 preflight ran again.
        assert runner.last_preflight is not startup_preflight
        assert runner.last_preflight.checked_at >= startup_preflight.checked_at
        assert runner.last_preflight.depth.sequence >= startup_preflight.depth.sequence
        assert sum(call == ORDER_BOOK for call in transport.calls) >= 2
        assert app.snapshot()["production_preflight"]["production_post_count"] == 0
    finally:
        app.stop()
    assert app.state is AppState.STOPPED and app.auto_execution is False


def test_start_preflight_failure_blocks_without_posting_or_halting(production):
    app, _runner, transport = production
    app.startup()
    transport.stale = True  # startup was healthy; the fresh start-time GET is not
    with pytest.raises(SessionStartBlocked) as blocked:
        app.start(SessionConfig(), CONFIRMATION)
    assert blocked.value.blockers == ("STALE_MARKET",)
    assert "STALE_MARKET" in str(blocked.value)
    assert app.state is AppState.STOPPED and app.auto_execution is False
    assert app.last_error == "PRODUCTION_PREFLIGHT_BLOCKED: STALE_MARKET"
    assert transport.posts == 0
    readiness = app.snapshot()["production_preflight"]
    assert readiness["label"] == "BLOCKED" and readiness["reason"] == "STALE_MARKET"


def test_control_start_reports_exact_blocker_and_remains_retryable(production):
    app, _runner, transport = production
    app.startup()
    http = TestClient(create_mvp_app(app))
    token = http.get("/api/v1/control/session").json()["control_token"]
    headers = {"Origin": "http://testserver", "X-AutoFund-Control-Token": token}
    transport.wide = True  # ask far from bid; only the spread guard fails
    response = http.post("/api/v1/control/start", json=START_BODY, headers=headers)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "PRODUCTION_PREFLIGHT_BLOCKED" and detail["blockers"] == ["SPREAD_GUARD"]
    assert app.state is AppState.STOPPED and app.auto_execution is False
    assert transport.posts == 0
    assert http.get("/api/v1/mvp").json()["production_preflight"]["label"] == "BLOCKED"
    # The blocker clearing without a restart is enough for the normal product flow.
    transport.wide = False
    assert http.post("/api/v1/control/start", json=START_BODY, headers=headers).status_code == 200
    assert app.state is AppState.RUNNING and app.auto_execution is True
    assert transport.posts == 0


def test_startup_preflight_failure_still_reaches_stopped_without_trading(production):
    app, _runner, transport = production
    transport.minimum = "60"  # exchange minimum above the 11 MXN single-order cap
    app.startup()
    assert app.state is AppState.STOPPED and app.auto_execution is False
    readiness = app.snapshot()["production_preflight"]
    assert readiness["ready"] is False and readiness["label"] == "BLOCKED"
    assert "VALUE_LIMITS_FAIL" in readiness["reason"]
    with pytest.raises(SessionStartBlocked):
        app.start(SessionConfig(), CONFIRMATION)
    assert app.state is AppState.STOPPED
    assert transport.posts == 0


def test_demo_runner_never_claims_production_readiness(tmp_path):
    app = AutoFundOrchestrator(tmp_path, DemoAutonomousRunner(), demo=True)
    app.startup()
    assert app.snapshot()["production_preflight"]["label"] == "NOT APPLICABLE"
    app.start(SessionConfig(), CONFIRMATION)
    assert app.state is AppState.RUNNING and app.auto_execution is True


def test_missing_production_credentials_block_without_any_exchange_write(tmp_path, monkeypatch):
    for name in ("AUTOFUND_BITSO_LIVE_API_KEY", "AUTOFUND_BITSO_LIVE_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    calls: list[str] = []
    monkeypatch.setattr("autofund.live.client.BitsoProductionLiveTransport.request",
                        lambda *args, **kwargs: calls.append("EXCHANGE"))
    runner = ProductionAutonomousRunner(tmp_path / "live.jsonl")
    app = AutoFundOrchestrator(tmp_path / "artifacts", runner)
    app.startup()
    assert app.state is AppState.HALTED
    assert app.last_error == "LIVE_CREDENTIALS_MISSING_OR_INVALID"
    readiness = app.snapshot()["production_preflight"]
    assert readiness["blockers"] == ["LIVE_CREDENTIALS_MISSING_OR_INVALID"]
    assert readiness["ready"] is False and readiness["guidance"]
    with pytest.raises(Exception):
        app.start(SessionConfig(), CONFIRMATION)
    assert app.state is AppState.HALTED and app.auto_execution is False
    assert calls == []


def test_real_app_scanner_wiring_reuses_initialized_production_fee_client(production):
    app, runner, transport = production
    original_request = transport.request

    def multi_book_request(method, path, body, authorization, permit=None):
        if path == "/api/v3/fees":
            transport.calls.append(method + " " + path)
            return {"fees": [
                {"book": "btc_mxn", "maker_fee_decimal": "0.0050",
                 "taker_fee_decimal": "0.0100", "current_volume": "0"},
                {"book": "eth_mxn", "maker_fee_decimal": "0.0065",
                 "taker_fee_decimal": "0.0078", "current_volume": "0"},
            ]}
        if path == "/api/v3/available_books":
            rows = original_request(method, path, body, authorization, permit)
            return [*rows, {**rows[0], "book": "eth_mxn"}]
        return original_request(method, path, body, authorization, permit)

    transport.request = multi_book_request
    app.startup()

    class PublicMarketClient:
        def available_books(self):
            from autofund.exchanges.bitso import parsing
            payload = transport.request("GET", "/api/v3/available_books", b"", "")
            return parsing.books(payload)

        def ticker(self, book):
            return SimpleNamespace(book=book, bid=D("1000000"), ask=D("1000010"),
                                   high=D("1010000"), low=D("990000"),
                                   volume=D("100"), vwap=D("100"))

        def order_book(self, book):
            level = SimpleNamespace(price=D("1000000"), amount=D("0.0002"))
            ask = SimpleNamespace(price=D("1000010"), amount=D("0.0002"))
            return SimpleNamespace(book=book, timestamp=datetime(2026, 9, 19, 12, tzinfo=UTC),
                                   sequence=1, bids=(level,), asks=(ask,), spread_bps=D("0.1"))

    source = production_scanner_source(runner, PublicMarketClient())
    before_posts = transport.posts
    evidence = MarketScanner(source).scan(now=datetime(2026, 9, 19, 12, tzinfo=UTC))
    assert evidence["fee_source"] == "ACCOUNT CONFIRMED"
    assert evidence["books_with_account_fee"] == 2
    by_book = {row["book"]: row for row in evidence["candidates"]}
    assert by_book["btc_mxn"]["maker_fee"] == "0.0050"
    assert by_book["btc_mxn"]["taker_fee"] == "0.0100"
    assert by_book["eth_mxn"]["maker_fee"] == "0.0065"
    assert by_book["eth_mxn"]["taker_fee"] == "0.0078"
    # One startup/preflight fee GET plus one scanner snapshot, never one per book.
    assert transport.calls.count("GET /api/v3/fees") == 2
    assert transport.posts == before_posts == 0


def test_wallet_api_is_get_only_and_separates_account_from_portfolio(production):
    app, _runner, transport = production
    app.startup()
    http = TestClient(create_mvp_app(app))
    response = http.get("/api/v1/mvp/wallet")
    assert response.status_code == 200
    body = response.json()
    assert body["bitso_wallet"]["read_only"] is True
    assert body["bitso_wallet"]["balances"][1]["total"] == "5"
    assert body["autofund_portfolio"]["position"] is None
    assert body["autofund_portfolio"]["cash_mxn"] == "50"
    assert transport.posts == 0
    assert not any(route.path.startswith("/api/v1/mvp/wallet") and "POST" in route.methods
                   for route in http.app.routes)


def test_wallet_failure_degrades_view_without_mutating_ledger(production):
    app, runner, transport = production
    app.startup()
    before = runner.execution.wallet.ledger
    runner.execution.client.balances = lambda: (_ for _ in ()).throw(RuntimeError("down"))
    runner._refresh_wallet()
    assert runner.execution.wallet.ledger == before
    assert app.snapshot()["wallet"]["status"] == "DEGRADED"
    assert app.snapshot()["wallet"]["error"] == "WALLET_READ_UNAVAILABLE"
    assert transport.posts == 0


def test_wallet_unknown_asset_has_no_fabricated_mxn_mark(production):
    from autofund.exchanges.bitso.models import ExchangeBalance
    app, runner, transport = production
    app.startup()
    runner.wallet_balances = (*runner.wallet_balances,
                              ExchangeBalance("eth", D("2"), D("0"), D("2")),
                              ExchangeBalance("xrp", D("0"), D("0"), D("0")))
    wallet = app.snapshot()["wallet"]
    eth = next(row for row in wallet["balances"] if row["currency"] == "ETH")
    assert eth["approx_mxn"] is None
    assert all(row["currency"] != "XRP" for row in wallet["balances"])
    assert transport.posts == 0


def test_account_balance_contradiction_blocks_start_without_post(production):
    app, runner, transport = production
    app.startup()
    def contradiction():
        runner.account_balance_contradiction = True
        runner.wallet_status = "DEGRADED"
        runner.wallet_error = "ACCOUNT_BALANCE_CONTRADICTION"
    runner._refresh_wallet = contradiction
    with pytest.raises(SessionStartBlocked) as blocked:
        app.start(SessionConfig(), CONFIRMATION)
    assert "ACCOUNT_BALANCE_CONTRADICTION" in blocked.value.blockers
    assert app.state is AppState.STOPPED and app.auto_execution is False
    assert transport.posts == 0


def test_offline_real_buy_lifecycle_reaches_report_and_handoff(production):
    app, runner, transport = production
    # The economic guard refuses a BUY whose round trip cannot pay for itself, and
    # a 1% taker fee against the Champion's 20 bps exit target never can. This test
    # exercises lifecycle plumbing, so it uses a fee at which the strategy's own
    # exit boundary is genuinely profitable.
    transport.fee = "0.001"
    app.startup()
    original = runner.execution.submit_authorized
    def fill_then_submit(intent):
        transport.rows = [trade(intent.origin_id, minor=str(intent.minor_budget),
                                fee=transport.fee)]
        original(intent)
    runner.execution.submit_authorized = fill_then_submit
    app.start(SessionConfig(), CONFIRMATION)
    runner.handle_signal("BUY")
    app.stop()
    assert transport.posts == 1
    assert app.snapshot()["position"]["status"] == "OPEN"
    root = app.artifacts / app.session_id
    report = json.loads((root / "report.json").read_text())
    handoff = json.loads((root / "handoff.json").read_text())
    assert report["execution"]["order_submitting"] >= 1
    assert report["execution"]["order_acknowledged"] >= 1
    assert report["execution"]["fills"] >= 1
    assert report["financial"]["final_equity_mxn"] != "50"
    assert handoff["financial_truth"]["execution"]["reconciliation_state"] == "PASS"
    # The budget is capped by the 11 MXN single-order cap and split between the
    # quoted ask and the quote-denominated fee the account actually pays, so the
    # owned quantity is a consequence of the fee, not a fixed number.
    cap = D("11")
    fee = D(transport.fee)
    budget = (cap / (1 + fee)).quantize(D("0.00000001"), rounding=ROUND_DOWN)
    assert Decimal(handoff["financial_truth"]["portfolio"]["quantity"]) == budget / D("1000000")

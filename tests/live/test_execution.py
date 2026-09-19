"""Offline certification: injected exchange payloads only, never real HTTP."""
import io
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from autofund.live.client import (
    BitsoProductionLiveClient,
    LiveCredentials,
    validate_buy,
    validate_request,
)
from autofund.live.execution import LiveExecution
from autofund.live.journal import LiveExecutionJournal
from autofund.live.models import LiveConfig, LiveError
from autofund.live.preflight import preflight

D = Decimal
SCENARIOS = json.loads((Path(__file__).parents[1] / "fixtures" / "live" / "scenarios.json").read_text())


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


class FakeTransport:
    def __init__(self) -> None:
        self.posts = 0
        self.timeout = False
        self.minimum = "1"
        self.fee = "0.01"
        self.stale = False
        self.wide = False
        self.liquid = True
        self.rows: list[dict[str, str]] = []
        self.orders: list[dict[str, str]] = []
        self.calls: list[str] = []
        self.book_time: datetime | None = None
        self.sequence = 1
        self.post_oid = "order1"

    def request(self, method, path, body, authorization, permit=None):
        self.calls.append(method + " " + path)
        if method == "POST":
            assert permit is not None
            permit.consume(body)
            self.posts += 1
            if self.timeout:
                raise TimeoutError("fake-secret-MUST-NOT-ESCAPE")
            return {"oid": self.post_oid}
        if path == "/api/v3/balance":
            return {"balances": [{"currency": "mxn", "total": "100000", "locked": "0", "available": "100000"},
                                  {"currency": "btc", "total": "5", "locked": "0", "available": "5"}]}
        if path == "/api/v3/fees":
            return {"fees": [{"book": "btc_mxn", "maker_fee_decimal": "0.005", "taker_fee_decimal": self.fee, "current_volume": "0"}]}
        if path == "/api/v3/available_books":
            return [{"book": "btc_mxn", "minimum_amount": "0.00000001", "maximum_amount": "600",
                     "minimum_price": "1", "maximum_price": "40000000", "minimum_value": self.minimum,
                     "maximum_value": "200000000", "tick_size": "1"}]
        if path.startswith("/api/v3/order_book"):
            stamp = self.book_time or datetime.now(UTC) - timedelta(seconds=60 if self.stale else 0)
            return {"updated_at": stamp.isoformat(), "sequence": str(self.sequence), "bids": [{"book": "btc_mxn", "price": "999900", "amount": "1"}],
                    "asks": [{"book": "btc_mxn", "price": "1200000" if self.wide else "1000000", "amount": "1" if self.liquid else "0.00000001"}]}
        if path.startswith("/api/v3/orders?"):
            return self.orders
        if path.startswith("/api/v3/order_trades?"):
            return self.rows
        raise AssertionError("unexpected fake request")


def trade(origin: str, tid="trade1", minor="5"):
    return {"tid": tid, "oid": "order1", "origin_id": origin, "book": "btc_mxn", "side": "buy",
            "major_currency": "btc", "minor_currency": "mxn", "major": str(D(minor) / D("1000000")),
            "minor": "-" + minor, "price": "1000000", "created_at": datetime.now(UTC).isoformat(),
            "fees_amount": str(-D(minor) * D("0.01")), "fees_currency": "mxn", "maker_side": "sell"}


def sell_trade(origin: str, tid="trade-sell", minor="5", major="0.000005"):
    return {"tid": tid, "oid": "order2", "origin_id": origin, "book": "btc_mxn", "side": "sell",
            "major_currency": "btc", "minor_currency": "mxn", "major": "-" + major,
            "minor": minor, "price": str(D(minor) / D(major)), "created_at": datetime.now(UTC).isoformat(),
            "fees_amount": str(-D(minor) * D("0.01")), "fees_currency": "mxn", "maker_side": "buy"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # A fake can exercise a human gate in pytest; the real CLI detects pytest.
    for name in ("CI", "PYTEST_CURRENT_TEST", "PLAYWRIGHT_TEST", "AUTOFUND_AUTO_CONFIRM", "CODEX_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    fake = FakeTransport()
    journal = LiveExecutionJournal(tmp_path / "live.jsonl")
    client = BitsoProductionLiveClient(LiveCredentials("FAKE", "FAKE", True), transport=fake)
    engine = LiveExecution(client, journal, LiveConfig(slippage_tolerance=D("0.5")))
    yield engine, fake, journal
    journal.close()


def submit(engine, intent, **kwargs):
    assert isinstance(engine.client._transport, FakeTransport)
    os.environ.pop("PYTEST_CURRENT_TEST", None)
    engine.confirm_and_submit(intent, confirm_real_money=True,
                              stdin=Terminal("CONFIRM " + intent.origin_id + "\n"), stdout=Terminal(), **kwargs)


def test_success_ledger_dedupe_and_private_balance_isolation(setup):
    engine, fake, journal = setup
    checked = engine.check(D("5"))
    assert checked.ready
    intent = engine.create(checked)
    row = trade(intent.origin_id)
    fake.rows = [row, row]
    submit(engine, intent)
    assert fake.posts == 1
    assert engine.states[intent.origin_id] == "RECONCILED"
    assert engine.wallet.cash_mxn == D("44.95")
    assert engine.public()["inventory_btc"] == D("0.000005")
    assert len(engine.wallet.ledger) == 2
    assert "100000" not in str(engine.public())
    with pytest.raises(LiveError):
        submit(engine, intent)
    assert fake.posts == 1
    before = engine.wallet.ledger
    journal.close()
    recovered = LiveExecution(engine.client, LiveExecutionJournal(journal.path), engine.config)
    assert recovered.wallet.ledger == before
    assert not recovered.unresolved
    recovered.journal.close()


def test_session_authorized_buy_then_owned_sell_each_posts_once(setup):
    engine, fake, _ = setup
    buy = engine.create(engine.check(D("5")))
    fake.rows = [trade(buy.origin_id)]
    engine.submit_authorized(buy)
    assert fake.posts == 1 and engine.wallet.positions["BTC/MXN"].quantity == D("0.000005")
    sell = engine.create_sell(D("0.000005"))
    fake.post_oid = "order2"
    fake.rows = [sell_trade(sell.origin_id)]
    engine.submit_sell_authorized(sell)
    assert fake.posts == 2
    assert engine.wallet.positions["BTC/MXN"].quantity == 0
    assert not engine.unresolved


def test_sell_cannot_exceed_autofund_owned_inventory(setup):
    engine, fake, _ = setup
    with pytest.raises(LiveError, match="SELL_EXCEEDS_AUTOFUND_INVENTORY"):
        engine.create_sell(D("0.000001"))
    assert fake.posts == 0


@pytest.mark.parametrize("confirmation,interactive,flag", [("", True, True), ("yes", True, True), ("wrong", True, True), ("exact", False, True), ("exact", True, False)])
def test_gates_no_post(setup, confirmation, interactive, flag):
    engine, fake, _ = setup
    intent = engine.create(engine.check(D("5")))
    text = "CONFIRM " + intent.origin_id if confirmation == "exact" else confirmation
    stdin = Terminal(text) if interactive else io.StringIO(text)
    with pytest.raises(LiveError):
        engine.confirm_and_submit(intent, confirm_real_money=flag, stdin=stdin, stdout=Terminal())
    assert fake.posts == 0


@pytest.mark.parametrize("scenario", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_preflight_failure_no_post(setup, scenario):
    engine, fake, _ = setup
    setting = next(key for key in scenario if key != "check")
    value, check = scenario[setting], scenario["check"]
    setattr(fake, setting, value)
    checked = engine.check()
    assert not checked.ready and not checked.checks[check]
    assert fake.posts == 0


def test_fee_refetched_and_slippage_not_inferred(setup):
    engine, fake, _ = setup
    first = engine.check().budget
    fake.fee = "0.10"
    assert engine.check().budget < first
    engine.config = LiveConfig()
    assert not engine.check().checks["slippage_policy"]


def test_fee_inclusive_candidate_uses_eleven_mxn_cap(setup):
    engine, fake, _ = setup
    checked = engine.check()
    assert checked.config.single_order_cap == D("11")
    assert checked.budget == D("10.89108910")
    public = checked.public()
    assert public["estimated_fee"] == D("0.1089108910")
    assert public["estimated_maximum_debit"] == D("10.9999999910")
    fake.minimum = "10.89"
    assert engine.check().ready
    fake.minimum = "10.89"
    fake.fee = "0.02"
    assert not engine.check().checks["value_limits"]


@pytest.mark.parametrize(
    ("exchange_delta", "timestamp_ok", "fresh_ok", "offset", "status"),
    [(D("1.2"), True, True, D("1.2"), "PASS_WITH_WARNING"),
     (D("1.5"), True, True, D("1.5"), "PASS_WITH_WARNING"),
     (D("2"), True, True, D("2"), "PASS_WITH_WARNING"),
     (D("2.000001"), False, True, D("2.000001"), "EXCHANGE_TIMESTAMP_ANOMALY"),
     (D("-16"), True, False, D("0"), "PASS")],
)
def test_exchange_timestamp_sanity_and_staleness_are_independent(
        setup, exchange_delta, timestamp_ok, fresh_ok, offset, status):
    engine, fake, _ = setup
    base = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    fake.book_time = base + timedelta(microseconds=int(exchange_delta * D("1000000")))
    values = iter((base, base, base))
    monotonic_values = iter((10.0, 10.01))
    checked = preflight(engine.client, engine.config, engine.wallet, unresolved=False,
                        clock=lambda: next(values), monotonic_clock=lambda: next(monotonic_values))
    assert checked.checks["exchange_timestamp_sanity"] is timestamp_ok
    assert checked.checks["market_freshness"] is fresh_ok
    assert checked.exchange_timestamp_future_offset_seconds == offset
    assert checked.public()["exchange_timestamp_status"] == status


def test_old_local_snapshot_is_a_hard_failure(setup):
    engine, fake, _ = setup
    base = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    fake.book_time = base
    values = iter((base, base, base))
    monotonic_values = iter((10.0, 11.000001))
    checked = preflight(engine.client, engine.config, engine.wallet, unresolved=False,
                        clock=lambda: next(values), monotonic_clock=lambda: next(monotonic_values))
    assert not checked.checks["local_snapshot_freshness"]
    assert "LOCAL_SNAPSHOT_STALE" in checked.failures


def test_clock_normalizes_timezone_offset_and_preserves_microseconds(setup):
    engine, fake, _ = setup
    fake.book_time = datetime.fromisoformat("2026-09-18T07:00:00.250-05:00")
    values = iter((datetime(2026, 9, 18, 12, 0, 0, 500000, tzinfo=UTC),) * 3)
    checked = preflight(engine.client, engine.config, engine.wallet, unresolved=False,
                        clock=lambda: next(values))
    assert checked.local_received_at.tzinfo == UTC
    assert checked.market_age_seconds == D("0.25")
    assert checked.exchange_timestamp_future_offset_seconds == D("0")
    assert UTC.utcoffset(checked.checked_at) == timedelta(0)


def test_utc_z_equivalent_timestamp_and_diagnostics(setup):
    engine, fake, _ = setup
    base = datetime.fromisoformat("2026-09-18T12:00:00.125Z")
    fake.book_time = base
    values = iter((base, base, base))
    checked = preflight(engine.client, engine.config, engine.wallet, unresolved=False,
                        clock=lambda: next(values))
    report = checked.public()
    assert report["exchange_updated_at"] == base
    assert report["local_received_at"] == base
    assert report["local_validation_at"] == base
    assert report["exchange_timestamp_status"] == "PASS"
    assert report["freshness_status"] == "PASS"


@pytest.mark.parametrize(("prior", "current", "allowed"), [(1, 2, True), (2, 2, True), (3, 2, False)])
def test_orderbook_sequence_policy(setup, prior, current, allowed):
    engine, fake, _ = setup
    fake.sequence = current
    checked = preflight(engine.client, engine.config, engine.wallet, unresolved=False, prior_sequence=prior)
    assert checked.checks["orderbook_sequence"] is allowed
    assert ("ORDERBOOK_SEQUENCE_REGRESSION" in checked.failures) is (not allowed)


@pytest.mark.parametrize(("change", "failure"), [("stale", "STALE_MARKET"), ("wide", "SPREAD_GUARD")])
def test_confirmation_revalidates_final_get_and_never_posts_on_failure(setup, change, failure):
    engine, fake, _ = setup
    intent = engine.create(engine.check(D("5")))
    setattr(fake, change, True)
    with pytest.raises(LiveError, match=failure):
        submit(engine, intent)
    assert fake.posts == 0


def test_confirmation_valid_final_get_posts_exactly_once_to_fake(setup):
    engine, fake, _ = setup
    intent = engine.create(engine.check(D("5")))
    fake.rows = [trade(intent.origin_id)]
    submit(engine, intent)
    assert fake.posts == 1
    assert sum(call.startswith("GET /api/v3/order_book") for call in fake.calls) == 2


@pytest.mark.parametrize("evidence", ["filled", "partial", "none", "open"])
def test_timeout_recovery_never_retries_post(setup, evidence):
    engine, fake, journal = setup
    intent = engine.create(engine.check(D("5")))
    fake.timeout = True
    if evidence in {"filled", "partial"}:
        fake.rows = [trade(intent.origin_id, minor="5" if evidence == "filled" else "2")]
    if evidence == "open":
        fake.orders = [{"oid": "order1", "origin_id": intent.origin_id, "book": "btc_mxn", "side": "buy", "status": "open",
                        "original_amount": "0.000005", "unfilled_amount": "0.000005", "price": "0"}]
    submit(engine, intent)
    assert fake.posts == 1
    assert bool(engine.unresolved) == (evidence != "filled")
    journal.close()
    restarted = LiveExecution(engine.client, LiveExecutionJournal(journal.path), engine.config)
    restarted.recover()
    assert fake.posts == 1
    if evidence != "filled":
        with pytest.raises(LiveError):
            restarted.create(restarted.check(D("5")))
    restarted.journal.close()


def test_late_partial_fill_and_contradictory_tid_halt(setup):
    engine, fake, _ = setup
    intent = engine.create(engine.check(D("5")))
    first = trade(intent.origin_id, minor="2")
    fake.rows = [first]
    submit(engine, intent)
    assert engine.public()["inventory_btc"] == D("0.000002")
    fake.rows = [first, trade(intent.origin_id, "trade2", "3")]
    engine.recover()
    assert engine.states[intent.origin_id] == "RECONCILED"
    assert len(engine.wallet.ledger) == 3
    assert fake.posts == 1


@pytest.mark.parametrize("terminal", ["cancelled", "completed"])
def test_partial_fill_reconciles_only_with_terminal_remote_evidence(setup, terminal):
    engine, fake, _ = setup
    intent = engine.create(engine.check(D("5")))
    fake.timeout = True
    fake.rows = [trade(intent.origin_id, minor="2")]
    fake.orders = [{"oid": "order1", "origin_id": intent.origin_id, "book": "btc_mxn", "side": "buy",
                    "status": terminal, "original_amount": "0.000005", "unfilled_amount": "0" if terminal == "completed" else "0.000003", "price": "0"}]
    submit(engine, intent)
    assert engine.states[intent.origin_id] == "RECONCILED"
    assert engine.public()["inventory_btc"] == D("0.000002")
    assert fake.posts == 1


def test_contradictory_fill_remains_unresolved(setup):
    engine, fake, _ = setup
    intent = engine.create(engine.check(D("5")))
    original = trade(intent.origin_id, minor="2")
    fake.rows = [original]
    submit(engine, intent)
    fake.rows = [{**original, "minor": "-3"}]
    engine.recover()
    assert engine.states[intent.origin_id] == "HALTED"
    assert engine.unresolved
    assert len(engine.wallet.ledger) == 2


@pytest.mark.parametrize("method,path", [("DELETE", "/api/v3/orders"), ("PATCH", "/api/v3/orders"),
    ("PUT", "/api/v3/orders"), ("POST", "/api/v3/withdrawals"), ("POST", "/unknown"),
    ("GET", "https://evil.example/api/v3/balance"), ("GET", "/api/v3/orders"),
    ("GET", "/api/v3/balance?broken"), ("GET", "/api/v3/order_book?book=eth_mxn")])
def test_http_policy_rejects_before_network(method, path):
    with pytest.raises(LiveError):
        validate_request(method, path)


def test_buy_body_rejects_other_capabilities():
    with pytest.raises(LiveError):
        validate_buy(b'{"side":"sell"}')


def test_automation_real_gate_is_blocked(setup, monkeypatch):
    engine, fake, _ = setup
    intent = engine.create(engine.check(D("5")))
    monkeypatch.setenv("CI", "true")
    with pytest.raises(LiveError):
        submit(engine, intent)
    assert fake.posts == 0


def test_credentials_redacted():
    assert "topsecret" not in repr(LiveCredentials("mykey", "topsecret"))


def test_live_environment_is_logically_separate_even_when_physical_values_match(monkeypatch):
    monkeypatch.setenv("AUTOFUND_BITSO_PROD_API_KEY", "shared-key")
    monkeypatch.setenv("AUTOFUND_BITSO_PROD_API_SECRET", "shared-secret")
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_API_KEY", "shared-key")
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_API_SECRET", "shared-secret")
    monkeypatch.setenv("AUTOFUND_BITSO_LIVE_PERMISSIONS_CONFIRMED", "true")
    credentials = LiveCredentials.from_environment()
    assert credentials.permissions_confirmed
    monkeypatch.delenv("AUTOFUND_BITSO_LIVE_API_KEY")
    monkeypatch.delenv("AUTOFUND_BITSO_LIVE_API_SECRET")
    with pytest.raises(LiveError):
        LiveCredentials.from_environment()


def test_eleven_mxn_transport_cap_is_hard():
    origin = "af-live-" + "1" * 32

    def body(minor):
        return json.dumps({"book": "btc_mxn", "side": "buy", "type": "market", "minor": minor,
                           "origin_id": origin, "slippage_tolerance": "0.5"}).encode()

    validate_buy(body("11"))
    with pytest.raises(LiveError):
        validate_buy(body("11.00000001"))


def test_no_permission_attestation_blocks_intent(setup):
    engine, fake, _ = setup
    engine.client.credentials = LiveCredentials("FAKE", "FAKE", False)
    checked = engine.check(D("5"))
    assert not checked.checks["permissions"]
    with pytest.raises(LiveError):
        engine.create(checked)
    assert fake.posts == 0


def test_shadow_dashboard_do_not_import_live_transport():
    root = Path(__file__).parents[2] / "src" / "autofund"
    for package in ("shadow", "dashboard"):
        for path in (root / package).glob("*.py"):
            assert "autofund.live" not in path.read_text()


def test_journal_fsync_failure_prohibits_network_write(setup, monkeypatch):
    engine, fake, journal = setup
    intent = engine.create(engine.check(D("5")))
    def failure(*args):
        raise OSError("disk unavailable")
    monkeypatch.setattr(journal, "append", failure)
    with pytest.raises(OSError):
        submit(engine, intent)
    assert fake.posts == 0


@pytest.mark.parametrize("crash_state", ["SUBMITTING", "ACKNOWLEDGED", "OUTCOME_UNKNOWN"])
def test_restart_before_response_never_resubmits(setup, crash_state):
    engine, fake, journal = setup
    intent = engine.create(engine.check(D("5")))
    engine.state(intent.origin_id, "SUBMITTING")
    if crash_state != "SUBMITTING":
        engine.state(intent.origin_id, crash_state, oid="order1")
    fake.rows = [trade(intent.origin_id)]
    journal.close()
    restarted = LiveExecution(engine.client, LiveExecutionJournal(journal.path), engine.config)
    assert restarted.unresolved
    restarted.recover()
    assert not restarted.unresolved
    assert fake.posts == 0
    assert restarted.public()["inventory_btc"] == D("0.000005")
    restarted.journal.close()


def test_sell_invariant_uses_owned_inventory_only(setup):
    from autofund.errors import RiskRejected
    from autofund.risk import RiskEngine
    engine, _, _ = setup
    with pytest.raises(RiskRejected):
        RiskEngine().check_sell(engine.wallet, "BTC/MXN", D("0.000001"), D("1000000"))
    assert not hasattr(engine.client, "sell")


def test_mutation_denied_by_real_transport_before_http(monkeypatch):
    from autofund.live.client import BitsoProductionLiveTransport
    def fail(*args, **kwargs):
        raise AssertionError("network must not be reached")
    monkeypatch.setattr("httpx.Client", fail)
    transport = BitsoProductionLiveTransport()
    for method, path in (("POST", "/api/v3/withdrawals"), ("DELETE", "/api/v3/orders"), ("PATCH", "/api/v3/orders"), ("POST", "/api/v3/orders")):
        with pytest.raises(LiveError):
            transport.request(method, path, b"{}", "REDACTED")


def test_committed_fill_rebuilt_after_crash_before_wallet_apply(setup):
    engine, fake, journal = setup
    intent = engine.create(engine.check(D("5")))
    engine.state(intent.origin_id, "SUBMITTING")
    fake.rows = [trade(intent.origin_id)]
    fill = engine.client.order_trades(intent.origin_id)[0]
    journal.append("LIVE_FILL", {"origin_id": intent.origin_id, "fill": fill})
    journal.close()
    restarted = LiveExecution(engine.client, LiveExecutionJournal(journal.path), engine.config)
    restarted.recover()
    assert restarted.wallet.cash_mxn == D("44.95")
    assert len(restarted.wallet.ledger) == 2
    assert not restarted.unresolved
    assert fake.posts == 0
    restarted.journal.close()

from dataclasses import replace
from decimal import Decimal as D

import pytest

from autofund.exchanges.bitso import parsing
from autofund.exchanges.bitso.accounting import ConfirmedFillAccounting
from autofund.exchanges.bitso.errors import ExchangeInvariantError
from autofund.exchanges.bitso.execution import StageExecutionEngine
from autofund.exchanges.bitso.journal import ExecutionJournal
from autofund.exchanges.bitso.models import ExecutionHealth, ExecutionPolicy, OrderState
from autofund.models import Side
from autofund.wallet import Wallet


def test_remote_order_size_mismatch_halts(engine, exchange, instruction, book, fees):
    engine.submit(
        instruction,
        book,
        fees,
        D("1000"),
        confirmed_stage=True,
        origin="af-stage-golden",
    )
    exchange.orders["af-stage-golden"] = replace(
        exchange.orders["af-stage-golden"],
        original_amount=D("0.02"),
        unfilled_amount=D("0.02"),
    )
    with pytest.raises(ExchangeInvariantError):
        engine.reconcile()
    assert engine.health is ExecutionHealth.HALTED


def test_crash_after_fill_persisted_before_wallet_application(
    tmp_path, exchange, instruction, book, fees, fixture_data, monkeypatch
):
    path = tmp_path / "crash-fill.jsonl"
    policy = ExecutionPolicy(single_order_cap_mxn=D("12"))
    with ExecutionJournal(path) as journal:
        engine = StageExecutionEngine(exchange, journal, policy)
        engine.startup()
        engine.submit(
            instruction,
            book,
            fees,
            D("1000"),
            confirmed_stage=True,
            origin="af-stage-golden",
        )
        for fill in parsing.fills(fixture_data["fills"]):
            exchange.add_fill(fill)
        original_append = journal.append

        def crash_after_fill(kind, data):
            original_append(kind, data)
            if kind == "fill" and data["fill"].trade_id == "trade-B":
                raise KeyboardInterrupt("simulated power loss")

        monkeypatch.setattr(journal, "append", crash_after_fill)
        with pytest.raises(KeyboardInterrupt):
            engine.reconcile()
    exchange.hidden = True  # Completed orders may disappear from lookup.
    with ExecutionJournal(path) as journal:
        restored = StageExecutionEngine(exchange, journal, policy)
        restored.startup()
        assert restored.wallet.cash_mxn == D("39.90")
        assert len(restored.wallet.ledger) == 3
        assert restored.orders["af-stage-golden"].state is OrderState.COMPLETED
        assert restored.health is ExecutionHealth.HEALTHY


def test_base_fee_and_sell_cost_basis(fixture_data):
    first, second = parsing.fills(fixture_data["fills"])
    wallet = Wallet()
    wallet.deposit(D("50"))
    accounting = ConfirmedFillAccounting(wallet)
    buy = replace(first, confirmed_fee=D("0.00003"), fee_currency="btc")
    assert accounting.apply(buy)
    assert wallet.cash_mxn == D("47")
    assert wallet.positions["BTC/MXN"].quantity == D("0.00297")
    assert wallet.positions["BTC/MXN"].cost_basis_mxn == D("3")
    sell = replace(
        second,
        side=Side.SELL,
        major_quantity=D("0.00297"),
        minor_value=D("5.94"),
        price=D("2000"),
        confirmed_fee=D("0.0594"),
    )
    assert accounting.apply(sell)
    assert wallet.cash_mxn == D("52.8806")
    assert wallet.positions["BTC/MXN"].quantity == 0
    assert wallet.realized_pnl() == D("2.8806")
    wallet.assert_invariants()


def test_rate_limit_cooldown_is_honored(fixture_data):
    import json

    from autofund.exchanges.bitso.auth import BitsoCredentials
    from autofund.exchanges.bitso.client import BitsoClient, TransportResponse
    from autofund.exchanges.bitso.errors import BitsoRateLimitError

    clock = [0.0]
    delays = []

    class Transport:
        calls = 0

        def send(self, request):
            self.calls += 1
            if self.calls == 1:
                return TransportResponse(429, b"{}", 120)
            return TransportResponse(
                200,
                json.dumps(
                    {"success": True, "payload": fixture_data["balances"]}
                ).encode(),
            )

    def sleep(value):
        delays.append(value)
        clock[0] += value

    api = BitsoClient(
        BitsoCredentials("synthetic", "synthetic"),
        transport=Transport(),
        sleep=sleep,
        monotonic=lambda: clock[0],
    )
    with pytest.raises(BitsoRateLimitError):
        api.get_balances()
    api.get_balances()
    assert delays == [120]


def test_concurrent_submissions_share_one_reservation(
    engine, exchange, instruction, book, fees
):
    from concurrent.futures import ThreadPoolExecutor

    from autofund.exchanges.bitso.errors import ExecutionHalted

    def submit_one(index):
        try:
            return engine.submit(
                instruction,
                book,
                fees,
                D("1000"),
                confirmed_stage=True,
                origin=f"af-stage-thread-{index}",
            )
        except ExecutionHalted:
            return None

    with ThreadPoolExecutor(2) as workers:
        results = list(workers.map(submit_one, range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(exchange.posts) == 1


def test_fee_overrun_is_booked_then_halts(
    engine, exchange, instruction, book, fees, fixture_data
):
    engine.submit(
        instruction,
        book,
        fees,
        D("1000"),
        confirmed_stage=True,
        origin="af-stage-golden",
    )
    first, second = parsing.fills(fixture_data["fills"])
    exchange.add_fill(replace(first, confirmed_fee=D("0.5")))
    exchange.add_fill(second)
    assert "ORDER_BUDGET_EXCEEDED" in engine.reconcile()
    assert engine.wallet.cash_mxn == D("39.43")
    assert len(engine.wallet.ledger) == 3
    assert engine.health is ExecutionHealth.HALTED


def test_cancel_after_partial_fill_keeps_accounting(
    engine, exchange, instruction, book, fees, fixture_data
):
    tracked = engine.submit(
        instruction,
        book,
        fees,
        D("1000"),
        confirmed_stage=True,
        origin="af-stage-golden",
    )
    exchange.add_fill(parsing.fills(fixture_data["fills"])[0])
    assert engine.cancel(tracked.request.origin_id)
    assert tracked.state is OrderState.CANCELLED
    assert engine.wallet.cash_mxn == D("46.97")
    assert engine.health is ExecutionHealth.HEALTHY


def test_journal_modified_record_fails_integrity(tmp_path):
    path = tmp_path / "tamper.jsonl"
    with ExecutionJournal(path) as journal:
        journal.append("example", {"amount": "5"})
    path.write_text(path.read_text().replace('"5"', '"6"'))
    with pytest.raises(ExchangeInvariantError):
        ExecutionJournal(path)

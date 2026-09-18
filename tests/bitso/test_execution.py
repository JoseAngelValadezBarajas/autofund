from dataclasses import replace
from decimal import Decimal as D

import pytest

from autofund.capital import CapitalManager
from autofund.errors import RiskRejected
from autofund.exchanges.bitso import parsing
from autofund.exchanges.bitso.errors import (
    BitsoValidationError,
    ExchangeInvariantError,
    ExecutionHalted,
)
from autofund.exchanges.bitso.execution import StageExecutionEngine
from autofund.exchanges.bitso.journal import ExecutionJournal
from autofund.exchanges.bitso.models import (
    ExecutionHealth,
    ExecutionPolicy,
    HaltReason,
    OrderState,
)
from autofund.models import Side
from autofund.replay.strategy import OrderIntent


def submit(engine, instruction, book, fees):
    return engine.submit(
        instruction,
        book,
        fees,
        D("1000"),
        confirmed_stage=True,
        origin="af-stage-golden",
    )


def test_golden_A_clean_lifecycle(engine, exchange, instruction, book, fees):
    tracked = submit(engine, instruction, book, fees)
    assert tracked.state is OrderState.ACKNOWLEDGED
    assert len(engine.wallet.ledger) == 1  # acknowledgement is not a fill
    engine.reconcile()
    assert tracked.state is OrderState.OPEN
    assert engine.cancel(tracked.request.origin_id)
    assert tracked.state is OrderState.CANCELLED
    assert engine.health is ExecutionHealth.HEALTHY
    assert engine.last_reconciliation == ("MATCH",)
    assert len(exchange.posts) == len(exchange.cancels) == 1


def test_golden_B_partial_duplicate_complete(
    engine, exchange, instruction, book, fees, fixture_data
):
    tracked = submit(engine, instruction, book, fees)
    first, second = parsing.fills(fixture_data["fills"])
    exchange.add_fill(first)
    engine.reconcile()
    assert engine.wallet.cash_mxn == D(fixture_data["expected"]["after_A_cash"])
    assert tracked.state is OrderState.PARTIALLY_FILLED
    exchange.add_fill(first)
    assert "DUPLICATE_FILL" in engine.reconcile()
    assert len(engine.wallet.ledger) == 2
    exchange.add_fill(second)
    engine.reconcile()
    expected = fixture_data["expected"]
    position = engine.wallet.positions["BTC/MXN"]
    assert engine.wallet.cash_mxn == D(expected["final_cash"])
    assert position.quantity == D(expected["final_quantity"])
    assert position.cost_basis_mxn == D(expected["final_cost_basis"])
    assert len(engine.wallet.ledger) == expected["ledger_entries"]
    assert len(engine.accounting.processed_fill_ids) == expected["unique_fills"]
    assert tracked.state is OrderState.COMPLETED
    assert engine.health is ExecutionHealth.HEALTHY
    assert len(exchange.posts) == 1


def test_golden_C_ambiguous_post_recovers_no_second_post(
    engine, exchange, instruction, book, fees
):
    exchange.mode = "timeout_found"
    tracked = submit(engine, instruction, book, fees)
    assert tracked.oid == "remote-1" and tracked.state is OrderState.OPEN
    assert len(exchange.posts) == 1
    assert engine.health is ExecutionHealth.HEALTHY
    with pytest.raises(ExecutionHalted):
        submit(engine, instruction, book, fees)
    assert len(exchange.posts) == 1


def test_golden_D_absent_ambiguous_post_stays_halted(
    engine, exchange, instruction, book, fees
):
    exchange.mode = "timeout_absent"
    tracked = submit(engine, instruction, book, fees)
    assert tracked.state is OrderState.UNKNOWN and tracked.oid is None
    assert len(exchange.posts) == 1 and exchange.lookups == 3
    assert engine.health is ExecutionHealth.HALTED
    with pytest.raises(ExecutionHalted):
        submit(engine, instruction, book, fees)


def test_golden_E_remote_unknown_namespace_halts(
    engine, exchange, instruction, book, fees
):
    tracked = submit(engine, instruction, book, fees)
    remote = exchange.orders.pop(tracked.request.origin_id)
    exchange.orders["af-stage-unknown"] = replace(remote, origin_id="af-stage-unknown")
    assert "REMOTE_ORDER_UNKNOWN_LOCALLY" in engine.reconcile()
    assert engine.health is ExecutionHealth.HALTED
    assert not exchange.cancels


def test_manual_order_not_claimed_or_cancelled(
    engine, exchange, instruction, book, fees
):
    tracked = submit(engine, instruction, book, fees)
    remote = exchange.orders[tracked.request.origin_id]
    exchange.orders["manual"] = replace(remote, oid="manual-order", origin_id="manual")
    engine.reconcile()
    assert engine.health is ExecutionHealth.HEALTHY
    with pytest.raises(BitsoValidationError):
        engine.cancel("manual")
    assert not exchange.cancels


def test_capital_not_total_exchange_balance(engine, exchange, instruction, book, fees):
    assert exchange.get_balances()[0].total == D("100000")
    assert engine.wallet.cash_mxn == D("50")
    assert CapitalManager().deployment_limit(engine.wallet, {}) == D("25")
    oversized = replace(
        instruction, intent=OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("26"))
    )
    with pytest.raises(RiskRejected):
        engine.prepare(oversized, book, fees, D("1000"))
    capped = replace(
        instruction, intent=OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("13"))
    )
    with pytest.raises(BitsoValidationError, match="cap"):
        engine.prepare(capped, book, fees, D("1000"))
    assert not exchange.posts


@pytest.mark.parametrize("reason", list(HaltReason))
def test_halts_block_orders_allow_read_and_cancel(
    engine, exchange, instruction, book, fees, reason
):
    tracked = submit(engine, instruction, book, fees)
    engine.halt(reason)
    with pytest.raises(ExecutionHalted):
        engine.prepare(instruction, book, fees, D("1000"))
    assert engine.cancel(tracked.request.origin_id)
    assert len(exchange.posts) == 1


def test_balance_mismatch_halts_and_recovery(engine, exchange, instruction, book, fees):
    exchange.delta = D("1")
    assert "BALANCE_MISMATCH" in engine.reconcile()
    with pytest.raises(ExecutionHalted):
        submit(engine, instruction, book, fees)
    exchange.delta = D("0")
    engine.reconcile()
    assert engine.health is ExecutionHealth.HEALTHY


def test_no_confirmation_no_post(engine, exchange, instruction, book, fees):
    engine.prepare(instruction, book, fees, D("1000"))
    with pytest.raises(BitsoValidationError):
        engine.submit(instruction, book, fees, D("1000"))
    assert not exchange.posts


def test_contradictory_fill_halts(
    engine, exchange, instruction, book, fees, fixture_data
):
    submit(engine, instruction, book, fees)
    first = parsing.fills(fixture_data["fills"])[0]
    exchange.add_fill(first)
    engine.reconcile()
    exchange.fills.append(replace(first, confirmed_fee=D("0.04")))
    with pytest.raises(ExchangeInvariantError):
        engine.reconcile()
    assert engine.health is ExecutionHealth.HALTED and len(engine.wallet.ledger) == 2


@pytest.mark.parametrize("fee,currency", [(None, None), (D("0.01"), "eth")])
def test_unconfirmed_or_unsupported_fee_never_booked(
    engine, exchange, instruction, book, fees, fixture_data, fee, currency
):
    submit(engine, instruction, book, fees)
    fill = replace(
        parsing.fills(fixture_data["fills"])[0],
        confirmed_fee=fee,
        fee_currency=currency,
    )
    exchange.add_fill(fill)
    with pytest.raises(ExchangeInvariantError):
        engine.reconcile()
    assert len(engine.wallet.ledger) == 1 and engine.health is ExecutionHealth.HALTED


def test_restart_after_post_before_ack(tmp_path, exchange, instruction, book, fees):
    path = tmp_path / "restart.jsonl"
    policy = ExecutionPolicy(single_order_cap_mxn=D("12"))
    with ExecutionJournal(path) as journal:
        engine = StageExecutionEngine(exchange, journal, policy)
        engine.startup()
        exchange.mode = "crash"
        with pytest.raises(RuntimeError):
            submit(engine, instruction, book, fees)
    with ExecutionJournal(path) as journal:
        restored = StageExecutionEngine(exchange, journal, policy)
        assert restored.health is ExecutionHealth.HALTED
        restored.startup()
        assert restored.orders["af-stage-golden"].state is OrderState.OPEN
        assert restored.health is ExecutionHealth.HEALTHY
        assert len(exchange.posts) == 1


def test_restart_durable_fill_exactly_once(
    tmp_path, exchange, instruction, book, fees, fixture_data
):
    path = tmp_path / "fills.jsonl"
    policy = ExecutionPolicy(single_order_cap_mxn=D("12"))
    with ExecutionJournal(path) as journal:
        engine = StageExecutionEngine(exchange, journal, policy)
        engine.startup()
        submit(engine, instruction, book, fees)
        for fill in parsing.fills(fixture_data["fills"]):
            exchange.add_fill(fill)
        engine.reconcile()
        original = engine.wallet.ledger
    with ExecutionJournal(path) as journal:
        restored = StageExecutionEngine(exchange, journal, policy)
        restored.startup()
        assert restored.wallet.ledger == original
        assert len(restored.accounting.processed_fill_ids) == 2
        assert restored.health is ExecutionHealth.HEALTHY


def test_journal_single_writer_and_corruption(tmp_path):
    path = tmp_path / "journal.jsonl"
    with ExecutionJournal(path) as journal:
        journal.append("example", {"value": "1"})
        with pytest.raises(ExchangeInvariantError):
            ExecutionJournal(path)
    path.write_bytes(path.read_bytes()[:-1])
    with pytest.raises(ExchangeInvariantError):
        ExecutionJournal(path)

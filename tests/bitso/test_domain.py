from dataclasses import FrozenInstanceError, replace
from decimal import Decimal as D

import pytest

from autofund.errors import InvalidFinancialInput
from autofund.exchanges.bitso import parsing
from autofund.exchanges.bitso.errors import BitsoValidationError, ExchangeInvariantError
from autofund.exchanges.bitso.models import (
    BitsoOrderRequest,
    ExchangeBalance,
    ExecutionInstruction,
    ExecutionPolicy,
    OrderType,
    origin_id,
)
from autofund.exchanges.bitso.precision import ExchangePrecisionPolicy
from autofund.models import Side
from autofund.replay.strategy import OrderIntent


def test_metadata_balances_fees_are_decimal_immutable(fixture_data, book, fees):
    balance = parsing.balances(fixture_data["balances"])[0]
    assert (
        balance.total == D("100000")
        and balance.available == balance.total - balance.locked
    )
    assert book.major_currency == "btc" and book.minor_currency == "mxn"
    assert fees.maker_fee_decimal != fees.taker_fee_decimal
    for value in (
        book.minimum_value,
        book.tick_size,
        balance.total,
        fees.current_volume,
    ):
        assert isinstance(value, D)
    with pytest.raises(FrozenInstanceError):
        book.tick_size = D("1")


@pytest.mark.parametrize(
    "value", [1, 1.0, True, None, "NaN", "Infinity", "1e1000", "invalid"]
)
def test_financial_wire_values_reject_float_and_invalid(value):
    with pytest.raises(ExchangeInvariantError):
        parsing.number(value)


@pytest.mark.parametrize(
    "total,locked,available", [("10", "2", "9"), ("-1", "0", "-1"), ("10", "11", "-1")]
)
def test_balance_invariant(total, locked, available):
    with pytest.raises(ExchangeInvariantError):
        ExchangeBalance("mxn", D(total), D(locked), D(available))


@pytest.mark.parametrize(
    "changes",
    [{"minimum_amount": D("0")}, {"tick_size": 1.0}, {"minimum_price": D("90000000")}],
)
def test_invalid_metadata(book, changes):
    with pytest.raises((ExchangeInvariantError, InvalidFinancialInput)):
        replace(book, **changes)


def test_tick_rounding_audited_and_constraints(book, fees, instruction):
    prepared = ExchangePrecisionPolicy().prepare(
        replace(instruction, limit_price=D("1009")),
        book,
        fees,
        ExecutionPolicy(),
        "af-stage-round",
    )
    assert prepared.request.price == D("1000")
    assert any("1009->1000" in s for s in prepared.adjustments)
    with pytest.raises(BitsoValidationError, match="tick"):
        ExchangePrecisionPolicy().validate(
            replace(prepared.request, price=D("1009")), book
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"major": D("0.00000001")},
        {"major": D("601")},
        {"price": D("90")},
        {"price": D("40000010")},
        {"major": D("0.0001")},
    ],
)
def test_exchange_limit_validation(book, fees, instruction, changes):
    request = (
        ExchangePrecisionPolicy()
        .prepare(instruction, book, fees, ExecutionPolicy(), "af-stage-limits")
        .request
    )
    with pytest.raises(BitsoValidationError):
        ExchangePrecisionPolicy().validate(replace(request, **changes), book)


def test_market_buy_uses_minor_without_price(book, fees):
    instruction = ExecutionInstruction(
        OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("10.2")), OrderType.MARKET
    )
    prepared = ExchangePrecisionPolicy().prepare(
        instruction,
        book,
        fees,
        ExecutionPolicy(slippage_tolerance=D("0.5")),
        "af-stage-market",
    )
    assert prepared.request.major is None and prepared.request.price is None
    assert prepared.request.minor == D("10")
    assert prepared.reserved_mxn == D("10.2")
    assert prepared.request.payload()["slippage_tolerance"] == "0.5"


def test_sell_major_preserved(book, fees):
    instruction = ExecutionInstruction(
        OrderIntent("BTC/MXN", Side.SELL, quantity=D("0.00123456789")), OrderType.MARKET
    )
    prepared = ExchangePrecisionPolicy().prepare(
        instruction, book, fees, ExecutionPolicy(), "af-stage-sell"
    )
    assert prepared.request.major == instruction.intent.quantity
    assert prepared.request.minor is None
    assert "slippage_tolerance" not in prepared.request.payload()


@pytest.mark.parametrize(
    "origin", ["x", "af-stage-", "af-stage-" + "a" * 32, "af-stage-a/b", "af-stage-é"]
)
def test_invalid_origin(origin):
    with pytest.raises(BitsoValidationError):
        origin_id(origin)


def test_origin_unique_valid():
    values = {origin_id() for _ in range(100)}
    assert len(values) == 100 and all(
        len(v) <= 40 and v.startswith("af-stage-") for v in values
    )


@pytest.mark.parametrize(
    "field,value", [("major", 0.001), ("price", 1000.0), ("slippage_tolerance", 0.5)]
)
def test_request_domain_rejects_floats(field, value):
    kwargs = {
        "book": "btc_mxn",
        "side": Side.BUY,
        "order_type": OrderType.LIMIT,
        "origin_id": "af-stage-test",
        "major": D("0.01"),
        "price": D("1000"),
    }
    kwargs[field] = value
    with pytest.raises((InvalidFinancialInput, BitsoValidationError)):
        BitsoOrderRequest(**kwargs)


def test_fill_payload_and_missing_fee(fixture_data):
    rows = fixture_data["fills"]
    fill = parsing.fills(rows)[0]
    assert fill.major_quantity == D("0.003") and fill.minor_value == D("3")
    assert fill.is_maker is True and fill.confirmed_fee == D("0.03")
    rows[0].pop("fees_amount")
    assert parsing.fills(rows)[0].confirmed_fee is None


def test_production_market_order_null_price_and_positive_fee_are_normalized(fixture_data):
    order = {"oid": "remote-1", "origin_id": "af-live-" + "a" * 32,
             "book": "btc_mxn", "side": "buy", "status": "completed",
             "original_amount": "0.003", "unfilled_amount": "0", "price": None}
    assert parsing.orders([order])[0].price == D("0")
    trade = dict(fixture_data["fills"][0])
    trade["fees_amount"] = "0.03"
    assert parsing.fills([trade])[0].confirmed_fee == D("0.03")


@pytest.mark.parametrize(
    "changes",
    [
        {"major": "-0.003"},
        {"minor": "3"},
        {"major_currency": "eth"},
        {"created_at": "2026-09-18T00:00:00"},
        {"price": 1.0},
    ],
)
def test_inconsistent_fill_rejected(fixture_data, changes):
    row = fixture_data["fills"][0] | changes
    with pytest.raises(ExchangeInvariantError):
        parsing.fills([row])

from dataclasses import FrozenInstanceError
from decimal import Decimal as D

import pytest

from autofund import InvalidFinancialInput, Position


def test_deposit_cash(wallet):
    assert wallet.cash_mxn == D("50")


def test_deposit_equity(wallet):
    assert wallet.equity({}) == D("50")


@pytest.mark.parametrize(
    "amount",
    [D("0"), D("-1"), 50.0, 50, "50", True, D("NaN"), D("Infinity"), D("sNaN")],
)
def test_invalid_deposit_atomic(wallet, amount):
    before = wallet.ledger
    with pytest.raises(InvalidFinancialInput):
        wallet.deposit(amount)
    assert wallet.cash_mxn == D("50")
    assert wallet.ledger == before


def test_views_immutable(wallet, engine):
    engine.buy("BTC/MXN", D("10"), D("100"))
    with pytest.raises(AttributeError):
        wallet.cash_mxn = D("900")
    with pytest.raises(TypeError):
        wallet.positions["BTC/MXN"] = Position("BTC/MXN")
    with pytest.raises(FrozenInstanceError):
        wallet.positions["BTC/MXN"].quantity = D("-1")
    with pytest.raises(FrozenInstanceError):
        wallet.ledger[0].cash_delta_mxn = D("900")
    assert isinstance(wallet.ledger, tuple)


@pytest.mark.parametrize(
    "quantity,basis", [(D("-1"), D("0")), (D("0"), D("1")), (D("1"), D("-1"))]
)
def test_invalid_position(quantity, basis):
    with pytest.raises(InvalidFinancialInput):
        Position("BTC/MXN", quantity, basis)


def test_missing_invalid_marks(wallet, engine):
    engine.buy("BTC/MXN", D("10"), D("100"))
    for marks in ({}, {"BTC/MXN": D("0")}, {"BTC/MXN": D("-1")}, {"BTC/MXN": 100.0}):
        with pytest.raises(InvalidFinancialInput):
            wallet.equity(marks)

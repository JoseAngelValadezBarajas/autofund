from decimal import Decimal as D

import pytest

from autofund import (
    InsufficientFunds,
    InvalidFinancialInput,
    PaperExecutionEngine,
    RiskEngine,
    RiskRejected,
)


def test_minimum(wallet):
    engine = PaperExecutionEngine(wallet, risk=RiskEngine(D("2")))
    with pytest.raises(RiskRejected):
        engine.buy("BTC/MXN", D("1.99"), D("100"))
    engine.buy("BTC/MXN", D("2"), D("100"))


@pytest.mark.parametrize("amount", [D("0"), D("-1")])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_nonpositive_amounts(engine, amount, side):
    with pytest.raises(RiskRejected):
        getattr(engine, side)("BTC/MXN", amount, D("100"))


@pytest.mark.parametrize("price", [D("0"), D("-1")])
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_nonpositive_prices(engine, price, side):
    with pytest.raises(RiskRejected):
        getattr(engine, side)("BTC/MXN", D("1"), price)


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize(
    "amount,price",
    [(1.0, D("100")), (D("1"), 100.0), (D("NaN"), D("100")), (D("1"), D("Infinity"))],
)
def test_invalid_public_financial_inputs(engine, side, amount, price):
    with pytest.raises(InvalidFinancialInput):
        getattr(engine, side)("BTC/MXN", amount, price)


def test_oversell_atomic(wallet, engine):
    with pytest.raises(RiskRejected):
        engine.sell("BTC/MXN", D("1"), D("100"))
    engine.buy("BTC/MXN", D("10"), D("100"))
    before = (wallet.cash_mxn, wallet.positions, wallet.ledger)
    with pytest.raises(RiskRejected):
        engine.sell("BTC/MXN", D("0.11"), D("100"))
    assert before == (wallet.cash_mxn, wallet.positions, wallet.ledger)


def test_insufficient_cash(engine):
    with pytest.raises(InsufficientFunds):
        engine.buy("BTC/MXN", D("51"), D("100"))


@pytest.mark.parametrize(
    "market", ["BTC/USD", "btc/MXN", "MXN/MXN", "/MXN", "BTC/MXN/ETH"]
)
def test_market_mxn_only(engine, market):
    with pytest.raises(InvalidFinancialInput):
        engine.buy(market, D("10"), D("100"))


def test_second_market_needs_first_mark(wallet, engine):
    engine.buy("BTC/MXN", D("10"), D("100"))
    with pytest.raises(InvalidFinancialInput):
        engine.buy("ETH/MXN", D("5"), D("50"))
    engine.buy("ETH/MXN", D("5"), D("50"), marks={"BTC/MXN": D("100")})
    assert wallet.equity({"BTC/MXN": D("100"), "ETH/MXN": D("50")}) == D("50")

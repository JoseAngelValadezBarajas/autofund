from decimal import Decimal as D

import pytest

from autofund import CapitalManager, InvalidFinancialInput, RiskRejected


def test_initial_deployable(wallet):
    manager = CapitalManager()
    assert manager.deployment_limit(wallet, {}) == D("25")
    assert manager.available_for_new_buys(wallet, {}) == D("25")


def test_boundary_buy(wallet, engine):
    engine.buy("BTC/MXN", D("25"), D("100"))
    assert wallet.cash_mxn == D("25")
    assert CapitalManager().available_for_new_buys(wallet, {"BTC/MXN": D("100")}) == D(
        "0"
    )


def test_over_boundary(engine):
    with pytest.raises(RiskRejected):
        engine.buy("BTC/MXN", D("25.01"), D("100"))


def test_equity_recalculates(wallet, engine):
    fill = engine.buy("BTC/MXN", D("10"), D("100"))
    manager = CapitalManager()
    marks = {"BTC/MXN": D("120")}
    assert wallet.equity(marks) == D("52")
    assert manager.deployment_limit(wallet, marks) == D("26")
    assert manager.available_for_new_buys(wallet, marks) == D("14")
    engine.sell("BTC/MXN", fill.quantity, D("120"))
    assert manager.available_for_new_buys(wallet, {}) == D("26")


def test_overdeployment_blocks_buys_but_allows_sell(wallet, engine):
    fill = engine.buy("BTC/MXN", D("25"), D("100"))
    marks = {"BTC/MXN": D("200")}
    assert CapitalManager().available_for_new_buys(wallet, marks) == D("0")
    with pytest.raises(RiskRejected):
        engine.buy("BTC/MXN", D("1"), D("200"))
    engine.sell("BTC/MXN", fill.quantity, D("200"))


@pytest.mark.parametrize("fraction", [D("-0.1"), D("1.01"), 0.5, D("NaN")])
def test_invalid_fraction(fraction):
    with pytest.raises(InvalidFinancialInput):
        CapitalManager(fraction)


def test_cash_cap(wallet):
    assert CapitalManager(D("1")).available_for_new_buys(wallet, {}) == wallet.cash_mxn
    assert CapitalManager(D("0")).available_for_new_buys(wallet, {}) == D("0")

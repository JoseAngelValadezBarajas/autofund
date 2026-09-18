from decimal import ROUND_UP, localcontext
from decimal import Decimal as D

import pytest

from autofund import EntryType, InvalidFinancialInput, PaperExecutionEngine


def test_required_roundtrip(wallet):
    engine = PaperExecutionEngine(wallet, fee_rate=D("0.001"))
    with localcontext() as ctx:
        ctx.prec = 50
        buy = engine.buy("BTC/MXN", D("10"), D("1000000"))
        assert wallet.cash_mxn == D("40")
        assert buy.gross_notional_mxn + buy.fee_mxn == D("10")
        assert wallet.positions["BTC/MXN"].cost_basis_mxn == D("10")
        assert buy.fee_mxn > D("0")
        assert wallet.ledger[-1].type == EntryType.BUY
        wallet.assert_invariants({"BTC/MXN": D("1000000")})
        sell = engine.sell("BTC/MXN", buy.quantity, D("1040000"))
        expected_gross = buy.quantity * D("1040000")
        expected_net = expected_gross - expected_gross * D("0.001")
        assert sell.cash_delta_mxn == expected_net
        assert sell.realized_pnl_mxn == expected_net - D("10")
        assert wallet.realized_pnl() == sell.realized_pnl_mxn
        assert wallet.cash_mxn == D("40") + expected_net
        assert wallet.equity({}) > D("50")
        assert wallet.positions["BTC/MXN"].quantity == D("0")
        assert wallet.positions["BTC/MXN"].cost_basis_mxn == D("0")
        assert wallet.cash_mxn == sum((e.cash_delta_mxn for e in wallet.ledger), D("0"))
        wallet.assert_invariants()


def test_losing_trade(wallet, engine):
    buy = engine.buy("BTC/MXN", D("10"), D("100"))
    sell = engine.sell("BTC/MXN", buy.quantity, D("90"))
    assert sell.realized_pnl_mxn == D("-1")
    assert wallet.equity({}) == D("49")


def test_average_cost_partial_sell(wallet, engine):
    engine.buy("BTC/MXN", D("10"), D("100"))
    engine.buy("BTC/MXN", D("10"), D("200"))
    with localcontext() as ctx:
        ctx.prec = 50
        assert wallet.positions["BTC/MXN"].average_unit_cost == D("20") / D("0.15")
    sell = engine.sell("BTC/MXN", D("0.03"), D("200"))
    assert sell.realized_pnl_mxn == D("2")
    assert wallet.positions["BTC/MXN"].quantity == D("0.12")
    assert wallet.positions["BTC/MXN"].cost_basis_mxn == D("16")
    engine.sell("BTC/MXN", D("0.12"), D("200"))
    assert wallet.positions["BTC/MXN"].cost_basis_mxn == D("0")
    assert wallet.realized_pnl() == D("10")


def test_slippage_adverse_both_sides(wallet):
    engine = PaperExecutionEngine(wallet, slippage_bps=D("100"))
    buy = engine.buy("BTC/MXN", D("10"), D("100"))
    assert buy.execution_price_mxn == D("101")
    assert buy.quantity < D("0.1")
    sell = engine.sell("BTC/MXN", buy.quantity, D("100"))
    assert sell.execution_price_mxn == D("99")
    assert sell.realized_pnl_mxn < D("0")


@pytest.mark.parametrize(
    "fee,slippage",
    [
        (D("-1"), D("0")),
        (D("1"), D("0")),
        (D("0"), D("10000")),
        (D("0"), D("-1")),
        (0.1, D("0")),
        (D("0"), 1.0),
    ],
)
def test_invalid_configuration(wallet, fee, slippage):
    with pytest.raises(InvalidFinancialInput):
        PaperExecutionEngine(wallet, fee_rate=fee, slippage_bps=slippage)


def test_decimal_context_independent():
    from autofund import Wallet

    def run():
        wallet = Wallet()
        wallet.deposit(D("50"))
        engine = PaperExecutionEngine(wallet, fee_rate=D("0.001"), slippage_bps=D("7"))
        fill = engine.buy("BTC/MXN", D("10"), D("1234567"))
        engine.sell("BTC/MXN", fill.quantity, D("1300000"))
        return wallet.ledger

    expected = run()
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.rounding = ROUND_UP
        assert run() == expected


def test_fee_residual_explicit(wallet):
    engine = PaperExecutionEngine(wallet, fee_rate=D("0.003"))
    fill = engine.buy("BTC/MXN", D("10"), D("7"))
    with localcontext() as ctx:
        ctx.prec = 50
        assert fill.gross_notional_mxn + fill.fee_mxn == D("10")
        assert abs(fill.rounding_adjustment_mxn) < D("1e-46")
        assert wallet.ledger[-1].rounding_adjustment_mxn == fill.rounding_adjustment_mxn


def test_reopening_preserves_realized_pnl(wallet, engine):
    buy = engine.buy("BTC/MXN", D("10"), D("100"))
    engine.sell("BTC/MXN", buy.quantity, D("110"))
    engine.buy("BTC/MXN", D("5"), D("100"))
    assert wallet.positions["BTC/MXN"].cost_basis_mxn == D("5")
    assert wallet.realized_pnl() == D("1")
    wallet.assert_invariants()


def test_sell_fee_independent_exact_oracle(wallet):
    engine = PaperExecutionEngine(wallet, fee_rate=D("0.01"))
    buy = engine.buy("BTC/MXN", D("10.10"), D("100"))
    assert buy.quantity == D("0.1")
    assert buy.fee_mxn == D("0.10")
    sell = engine.sell("BTC/MXN", D("0.1"), D("110"))
    assert sell.gross_notional_mxn == D("11")
    assert sell.fee_mxn == D("0.11")
    assert sell.cash_delta_mxn == D("10.89")
    assert sell.realized_pnl_mxn == D("0.79")
    assert wallet.cash_mxn == D("50.79")


def test_repeated_partial_sales_remove_all_cost(wallet, engine):
    engine.buy("BTC/MXN", D("10"), D("100"))
    for _ in range(3):
        engine.sell("BTC/MXN", D("0.03"), D("110"))
        wallet.assert_invariants()
    engine.sell("BTC/MXN", wallet.positions["BTC/MXN"].quantity, D("110"))
    assert wallet.positions["BTC/MXN"].cost_basis_mxn == D("0")
    assert wallet.realized_pnl() == D("1")

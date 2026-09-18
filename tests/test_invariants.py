from dataclasses import replace
from decimal import Decimal as D
from random import Random

import pytest

from autofund import AccountingInvariantError, PaperExecutionEngine


@pytest.mark.parametrize(
    "corruption",
    ["cash", "quantity", "basis", "pnl", "sequence", "ledger_cash", "ledger_pnl"],
)
def test_corruption_detected(wallet, engine, corruption):
    engine.buy("BTC/MXN", D("10"), D("100"))
    if corruption == "cash":
        wallet._cash = D("999")
    elif corruption in ("quantity", "basis", "pnl"):
        field = {
            "quantity": "quantity",
            "basis": "cost_basis_mxn",
            "pnl": "realized_pnl_mxn",
        }[corruption]
        wallet._positions["BTC/MXN"] = replace(
            wallet.positions["BTC/MXN"], **{field: D("999")}
        )
    else:
        field = {
            "sequence": "entry_id",
            "ledger_cash": "cash_delta_mxn",
            "ledger_pnl": "realized_pnl_mxn",
        }[corruption]
        wallet._ledger = (
            *wallet.ledger[:-1],
            replace(
                wallet.ledger[-1], **{field: 999 if field == "entry_id" else D("999")}
            ),
        )
    with pytest.raises(AccountingInvariantError):
        wallet.assert_invariants()
    with pytest.raises(AccountingInvariantError):
        engine.buy("BTC/MXN", D("1"), D("100"))


def test_seeded_roundtrip_sequence(wallet):
    rng = Random(71)
    engine = PaperExecutionEngine(wallet, fee_rate=D("0.002"), slippage_bps=D("10"))
    for _ in range(100):
        buy = engine.buy("BTC/MXN", D("2"), D(rng.randint(100, 150)))
        engine.sell("BTC/MXN", buy.quantity, D(rng.randint(100, 150)))
        wallet.assert_invariants()
        assert wallet.positions["BTC/MXN"].quantity == D("0")
        assert wallet.positions["BTC/MXN"].cost_basis_mxn == D("0")
    assert len(wallet.ledger) == 201


def test_tiny_deposit_never_silently_disappears(wallet):
    from autofund import InvalidFinancialInput

    before = (wallet.cash_mxn, wallet.ledger)
    with pytest.raises(InvalidFinancialInput):
        wallet.deposit(D("1e-60"))
    assert before == (wallet.cash_mxn, wallet.ledger)


def test_failed_candidate_does_not_publish(wallet, monkeypatch):
    before = (wallet.cash_mxn, wallet.positions, wallet.ledger)
    original = wallet._check

    def reject_candidate(cash, positions, ledger):
        if len(ledger) > len(before[2]):
            raise AccountingInvariantError("injected candidate inconsistency")
        original(cash, positions, ledger)

    monkeypatch.setattr(wallet, "_check", reject_candidate)
    with pytest.raises(AccountingInvariantError):
        wallet.deposit(D("5"))
    assert before == (wallet.cash_mxn, wallet.positions, wallet.ledger)

from decimal import Decimal as D

from autofund import CapitalManager, PaperExecutionEngine, Wallet


def main() -> None:
    wallet = Wallet()
    wallet.deposit(D("50"))
    capital = CapitalManager()
    engine = PaperExecutionEngine(
        wallet, capital, fee_rate=D("0.001"), slippage_bps=D("5")
    )
    print("Initial equity:", wallet.equity({}), "MXN")
    print("Deployable:", capital.available_for_new_buys(wallet, {}), "MXN")
    buy = engine.buy("BTC/MXN", D("10"), D("1000000"))
    sell = engine.sell("BTC/MXN", buy.quantity, D("1040000"))
    wallet.assert_invariants({})
    print("Buy:", buy)
    print("Sell:", sell)
    print("Final equity:", wallet.equity({}), "MXN")
    print("Realized P&L:", wallet.realized_pnl(), "MXN")
    for entry in wallet.ledger:
        print(entry)
    print("Accounting invariants: PASS")


if __name__ == "__main__":
    main()

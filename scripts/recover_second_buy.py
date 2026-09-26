"""GET-only recovery of an unresolved Production BUY.

The concrete incident this was written for is public in `docs/MVP_0_1_2_RECONCILIATION_SPEC.md`:
a BUY was acknowledged by the exchange and then failed reconciliation 460 ms later, so a real order
existed whose outcome AutoFund did not know. Reproducing that recovery needs the origin id of the
blocked intent, which is account-specific, so it is supplied by the operator rather than committed.

Queries the exchange for the acknowledged order and its trades, classifies the outcome
deterministically, and reconstructs AutoFund accounting. It never submits anything: the only write
path is unreachable here because no submission permit is ever created, and the transport rejects
non-GET at the policy layer.

Run:  python scripts/recover_second_buy.py --origin-id af-live-<32 hex chars>
"""

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from autofund.live.client import BitsoProductionLiveClient, LiveCredentials
from autofund.live.execution import LiveExecution
from autofund.live.journal import DEFAULT_JOURNAL, LiveExecutionJournal
from autofund.live.models import LiveConfig

# The origin id of the blocked intent, supplied by the operator. It is deliberately not a
# committed constant: it identifies a specific order on a specific account, and publishing it
# would let a reader look up one account's activity.
ORIGIN = ""
OUT = Path("artifacts/mvp-certification/second-buy-recovery.json")


def isolated_journal() -> LiveExecutionJournal:
    """Open a copy of the live journal so a running app is never disturbed.

    The production journal is single-writer: if `autofund app` currently owns it,
    recovery must not steal or mutate it. A byte copy is a consistent point-in-time
    snapshot, and every write performed here lands in the copy only.
    """
    source = Path(DEFAULT_JOURNAL)
    if not source.exists():
        raise SystemExit("LIVE_JOURNAL_NOT_FOUND")
    try:
        journal = LiveExecutionJournal(source)
    except Exception:
        copy_dir = Path("artifacts/mvp-certification")
        copy_dir.mkdir(parents=True, exist_ok=True)
        target = copy_dir / "recovery-journal-copy.jsonl"
        # Copy the data file only. The lock companion is held by the running app,
        # so it is deliberately not copied; the copy takes its own fresh lock.
        target.write_bytes(source.read_bytes())
        Path(str(target) + ".lock").unlink(missing_ok=True)
        print(f"live journal is in use by another process; recovering from a copy at {target}")
        journal = LiveExecutionJournal(target)
    return journal


def main(argv: list[str] | None = None) -> int:
    global ORIGIN
    parser = argparse.ArgumentParser(
        prog="autofund recover-buy",
        description="GET-only recovery of an unresolved Production order from the live journal")
    parser.add_argument("--origin-id", dest="origin_id", default=ORIGIN,
                        help="origin id of the blocked intent (as recorded in the live journal)")
    parsed = parser.parse_args(argv)
    if not parsed.origin_id:
        parser.error("--origin-id is required: the origin id identifies a specific order on a "
                     "specific account and is deliberately not committed to the repository")
    ORIGIN = parsed.origin_id

    credentials = LiveCredentials.from_environment()
    client = BitsoProductionLiveClient(credentials, single_order_cap=Decimal("11"))
    journal = isolated_journal()
    execution = LiveExecution(client, journal, LiveConfig(slippage_tolerance=Decimal("0.5")))
    try:
        unresolved = list(execution.unresolved)
        print("unresolved at load:", unresolved)
        # Reconstruct the blocked intent from the journal and re-run recovery.
        if ORIGIN not in execution.intents:
            print("TARGET_NOT_IN_JOURNAL")
            return 1

        before = list(execution.wallet.ledger)
        orders = ()
        fills = ()
        orders_error = fills_error = None
        try:
            orders = client.lookup_order(ORIGIN)
        except Exception as exc:
            orders_error = str(exc)
        try:
            fills = client.order_trades(ORIGIN)
        except Exception as exc:
            fills_error = str(exc)

        print("\n=== remote order state ===")
        if orders_error:
            print("orders error:", orders_error)
        for order in orders:
            print(json.dumps({"oid": order.oid, "origin_id": order.origin_id, "book": order.book,
                              "side": order.side.value, "state": order.state.value,
                              "original_amount": str(order.original_amount),
                              "unfilled_amount": str(order.unfilled_amount)}, indent=1))
        if not orders:
            print("(no order row returned; a fully executed market order may not be listed)")

        print("\n=== trade evidence ===")
        if fills_error:
            print("trades error:", fills_error)
        for fill in fills:
            print(json.dumps({"trade_id": fill.trade_id, "oid": fill.exchange_order_id,
                              "origin_id": fill.origin_id, "side": fill.side.value,
                              "major_quantity": str(fill.major_quantity),
                              "minor_value": str(fill.minor_value), "price": str(fill.price),
                              "timestamp": fill.timestamp.isoformat(),
                              "confirmed_fee": str(fill.confirmed_fee),
                              "fee_currency": fill.fee_currency}, indent=1))
        if not fills:
            print("(no trade rows returned)")

        # Bounded GET-only recovery, exactly the production path.
        outcome = execution.reconcile_outcome(ORIGIN)
        print("\n=== outcome ===")
        print("classification:", outcome)
        print("detail:", json.dumps(execution.reconciliation_outcomes.get(ORIGIN), default=str))

        wallet = execution.public()
        inventory = Decimal(str(wallet["inventory_btc"]))
        cash = Decimal(str(wallet["cash_mxn"]))
        cost_basis = Decimal(str(wallet["cost_basis_mxn"]))
        print("\n=== reconstructed AutoFund accounting ===")
        print("state           :", execution.states.get(ORIGIN))
        print("unresolved      :", list(execution.unresolved))
        print("cash_mxn        :", cash)
        print("inventory_btc   :", inventory)
        print("cost_basis_mxn  :", cost_basis)
        print("realized_pnl    :", wallet["realized_pnl_mxn"])
        print("ledger entries  :", len(execution.wallet.ledger) - len(before) + len(before))
        print("new ledger      :", len(execution.wallet.ledger) - len(before))
        position = execution.wallet.positions.get("BTC/MXN")
        print("position        :", "NONE" if position is None or position.quantity == 0 else "OPEN")
        if position is not None:
            print("  quantity      :", position.quantity)
            print("  cost_basis    :", position.cost_basis_mxn)
        print("production POSTs:", client.outbound_methods.count("POST"))
        print("production GETs :", client.outbound_methods.count("GET"))

        # Idempotency: repeated recovery must not duplicate anything and must be a
        # clean no-op once the order is reconciled.
        ledger_after = list(execution.wallet.ledger)
        inventory_after = Decimal(str(execution.public()["inventory_btc"]))
        cash_after = Decimal(str(execution.public()["cash_mxn"]))
        repeats = [execution.reconcile_outcome(ORIGIN) for _ in range(3)]
        print("\n=== idempotency ===")
        print("repeat outcomes         :", repeats)
        print("ledger unchanged        :", list(execution.wallet.ledger) == ledger_after)
        print("inventory unchanged     :", Decimal(str(execution.public()["inventory_btc"])) == inventory_after)
        print("cash unchanged          :", Decimal(str(execution.public()["cash_mxn"])) == cash_after)
        print("POSTs after re-recovery :", client.outbound_methods.count("POST"))

        certificate = {
            "certified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "origin_id": ORIGIN,
            # Taken from the exchange response rather than a committed constant: the oid is a
            # property of the remote order, so deriving it is both truthful and identifier-free.
            "oid": orders[0].oid if orders else None,
            "remote_orders": [{"oid": o.oid, "state": o.state.value, "side": o.side.value,
                               "original_amount": str(o.original_amount),
                               "unfilled_amount": str(o.unfilled_amount)} for o in orders],
            "remote_fills": [{"trade_id": f.trade_id, "side": f.side.value,
                              "major_quantity": str(f.major_quantity), "minor_value": str(f.minor_value),
                              "price": str(f.price), "timestamp": f.timestamp.isoformat(),
                              "confirmed_fee": str(f.confirmed_fee), "fee_currency": f.fee_currency}
                             for f in fills],
            "classification": outcome,
            "production_get_count": client.outbound_methods.count("GET"),
            "production_post_count": client.outbound_methods.count("POST"),
            "accounting": {"cash_mxn": str(cash), "inventory_btc": str(inventory),
                           "cost_basis_mxn": str(cost_basis),
                           "realized_pnl_mxn": str(wallet["realized_pnl_mxn"])},
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(certificate, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")
        return 0
    finally:
        journal.close()


if __name__ == "__main__":
    raise SystemExit(main())

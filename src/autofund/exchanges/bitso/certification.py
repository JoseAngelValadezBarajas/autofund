from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from autofund.decimal_utils import financial
from autofund.models import Side
from autofund.replay.serialization import canonical_json, fingerprint
from autofund.replay.strategy import OrderIntent

from . import parsing
from .client import BitsoClient
from .errors import BitsoError, BitsoValidationError
from .execution import StageExecutionEngine
from .journal import ExecutionJournal
from .models import (
    ExecutionHealth,
    ExecutionInstruction,
    ExecutionPolicy,
    OrderState,
    OrderType,
)


@financial
def limit_instruction(
    client: BitsoClient, book_name: str, policy: ExecutionPolicy
) -> tuple[ExecutionInstruction, Decimal]:
    book, fees = client.get_book(book_name), client.get_fees(book_name)
    depth = client.get_depth(book_name)
    bids, asks = parsing.array(depth.get("bids")), parsing.array(depth.get("asks"))
    if not bids or not asks:
        raise BitsoValidationError("Stage book has no two-sided liquidity")
    stamp = datetime.fromisoformat(parsing.text(depth.get("updated_at")))
    if (
        stamp.tzinfo is None
        or not 0 <= (datetime.now(UTC) - stamp).total_seconds() <= 60
    ):
        raise BitsoValidationError("Stage depth is stale")
    bid = max(parsing.number(parsing.obj(b).get("price")) for b in bids)
    ask = min(parsing.number(parsing.obj(a).get("price")) for a in asks)
    if bid >= ask:
        raise BitsoValidationError("Stage book is crossed")
    price = min(bid, ask - book.tick_size)
    price = price // book.tick_size * book.tick_size
    if price < book.minimum_price:
        raise BitsoValidationError("no valid non-crossing Stage price")
    amount = max(book.minimum_amount, book.minimum_value / price).quantize(
        Decimal("0.00000001"), rounding=ROUND_CEILING
    )
    budget = (
        amount
        * price
        * (Decimal("1") + max(fees.maker_fee_decimal, fees.taker_fee_decimal))
    )
    if budget > policy.single_order_cap_mxn:
        raise BitsoValidationError("smallest valid Stage order exceeds configured cap")
    intent = OrderIntent(
        book_name.replace("_", "/").upper(), Side.BUY, budget_mxn=budget
    )
    return ExecutionInstruction(intent, OrderType.LIMIT, price, post_only=True), price


def certify(
    client: BitsoClient,
    *,
    book_name: str,
    journal_path: Path,
    output: Path,
    policy: ExecutionPolicy,
    confirm_stage: bool,
) -> dict[str, object]:
    started = datetime.now(UTC)
    artifact: dict[str, object] = {
        "schema_version": "autofund.bitso.certification.v1",
        "environment": "stage",
        "market": book_name,
        "started_at": started,
        "result": "FAIL",
        "live_fill": "SKIPPED",
    }
    with ExecutionJournal(journal_path) as journal:
        engine = StageExecutionEngine(client, journal, policy)
        try:
            engine.startup()
            balances_before, open_before = (
                client.get_balances(),
                client.get_open_orders(),
            )
            book, fees = client.get_book(book_name), client.get_fees(book_name)
            instruction, reference = limit_instruction(client, book_name, policy)
            prepared = engine.prepare(instruction, book, fees, reference)
            artifact.update(
                {
                    "requested_values": instruction,
                    "normalized_values": prepared.request.payload(),
                    "exchange_limits": book,
                    "fee_schedule": fees,
                    "adjustments": prepared.adjustments,
                    "balance_before": balances_before,
                    "open_orders_before": open_before,
                    "capital": "PASS",
                    "risk": "PASS",
                    "allocated_mxn": policy.allocated_mxn,
                    "single_order_cap_mxn": policy.single_order_cap_mxn,
                }
            )
            if not confirm_stage:
                artifact["result"] = "DRY_RUN"
                return artifact
            tracked = engine.submit(
                instruction,
                book,
                fees,
                reference,
                confirmed_stage=True,
                origin=prepared.request.origin_id,
            )
            artifact.update(
                {
                    "logical_order_id": tracked.request.origin_id,
                    "origin_id": tracked.request.origin_id,
                    "oid": tracked.oid,
                }
            )
            lookup_pass = False
            try:
                for attempt in range(policy.reconciliation_attempts):
                    rows = client.get_order(origin=tracked.request.origin_id)
                    if any(r.oid == tracked.oid for r in rows):
                        lookup_pass = True
                        break
                    if attempt + 1 < policy.reconciliation_attempts:
                        engine.sleep(policy.polling_seconds)
            finally:
                # Always attempt cleanup of our known order, including lookup failures.
                if tracked.oid is not None and tracked.state not in (
                    OrderState.CANCELLED,
                    OrderState.COMPLETED,
                    OrderState.REJECTED,
                ):
                    engine.cancel(tracked.request.origin_id)
            engine.reconcile()
            balances_after = client.get_balances()
            before = {b.currency: b.total for b in balances_before}
            artifact.update(
                {
                    "lookup": "PASS" if lookup_pass else "FAIL",
                    "cancel": "PASS"
                    if tracked.state is OrderState.CANCELLED
                    else "FAIL",
                    "balance_after": balances_after,
                    "open_orders_after": client.get_open_orders(),
                    "balance_deltas": {
                        b.currency: b.total - before.get(b.currency, Decimal("0"))
                        for b in balances_after
                        if b.currency in book_name.split("_")
                    },
                    "fills": tuple(engine.accounting.processed_fill_ids.values()),
                    "reconciliation_result": engine.last_reconciliation,
                    "result": "PASS"
                    if lookup_pass
                    and tracked.state is OrderState.CANCELLED
                    and engine.health is ExecutionHealth.HEALTHY
                    else "FAIL",
                }
            )
            return artifact
        except BitsoError as exc:
            artifact["error_type"] = type(exc).__name__
            raise
        finally:
            artifact["finished_at"] = datetime.now(UTC)
            artifact["state_transitions"] = [
                r["data"] for r in journal.records if r["kind"] == "state"
            ]
            semantic = {
                k: v
                for k, v in artifact.items()
                if k not in ("started_at", "finished_at")
            }
            artifact["semantic_fingerprint"] = fingerprint(semantic)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(canonical_json(artifact) + "\n", encoding="utf-8")

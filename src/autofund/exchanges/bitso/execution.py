import copy
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from functools import wraps
from typing import Concatenate

from autofund.capital import CapitalManager
from autofund.decimal_utils import financial
from autofund.models import Side
from autofund.replay.serialization import canonical_json, fingerprint
from autofund.risk import RiskEngine
from autofund.wallet import Wallet

from . import parsing
from .accounting import ConfirmedFillAccounting
from .client import BitsoClient, ExchangeExecutionPort
from .errors import (
    BitsoAuthenticationError,
    BitsoError,
    BitsoUnknownSubmissionOutcome,
    BitsoValidationError,
    ExchangeInvariantError,
    ExecutionHalted,
)
from .journal import ExecutionJournal
from .models import (
    BitsoOrderRequest,
    Book,
    ExchangeBalance,
    ExchangeTradeFill,
    ExecutionHealth,
    ExecutionInstruction,
    ExecutionPolicy,
    FeeSchedule,
    HaltReason,
    OrderState,
    OrderType,
    RemoteOrder,
    origin_id,
)
from .precision import ExchangePrecisionPolicy, PreparedOrder

TERMINAL = {OrderState.COMPLETED, OrderState.CANCELLED, OrderState.REJECTED}


def serialized[**P, T](
    method: Callable[Concatenate["StageExecutionEngine", P], T],
) -> Callable[Concatenate["StageExecutionEngine", P], T]:
    @wraps(method)
    def wrapper(
        self: "StageExecutionEngine", /, *args: P.args, **kwargs: P.kwargs
    ) -> T:
        with self._mutex:
            return method(self, *args, **kwargs)

    return wrapper


@dataclass
class TrackedOrder:
    request: BitsoOrderRequest
    reserved_mxn: Decimal
    state: OrderState = OrderState.SUBMITTED
    oid: str | None = None


def restore_request(row: dict[str, object]) -> BitsoOrderRequest:
    return BitsoOrderRequest(
        parsing.text(row.get("book")),
        parsing.side(row.get("side")),
        OrderType(parsing.text(row.get("type"))),
        parsing.text(row.get("origin_id")),
        parsing.number(row["major"]) if "major" in row else None,
        parsing.number(row["minor"]) if "minor" in row else None,
        parsing.number(row["price"]) if "price" in row else None,
        row.get("time_in_force") == "postonly",
        parsing.number(row["slippage_tolerance"])
        if "slippage_tolerance" in row
        else None,
    )


def restore_fill(row: dict[str, object]) -> ExchangeTradeFill:
    maker = row.get("is_maker")
    return ExchangeTradeFill(
        parsing.text(row.get("trade_id")),
        parsing.text(row.get("exchange_order_id")),
        parsing.text(row["origin_id"]) if row.get("origin_id") else None,
        parsing.text(row.get("book")),
        Side(parsing.text(row.get("side"))),
        parsing.number(row.get("major_quantity")),
        parsing.number(row.get("minor_value")),
        parsing.number(row.get("price")),
        datetime.fromisoformat(parsing.text(row.get("timestamp"))),
        maker if isinstance(maker, bool) else None,
        parsing.number(row["confirmed_fee"])
        if row.get("confirmed_fee") is not None
        else None,
        parsing.text(row["fee_currency"]) if row.get("fee_currency") else None,
    )


class StageExecutionEngine:
    """Serialized execution session. One unresolved order at a time.

    The journal's OS lock must be held for the lifetime of this engine. Different
    journals must not be used concurrently for one account (unknown namespace
    orders halt reconciliation, but are not a distributed account lock).
    """

    def __init__(
        self,
        client: ExchangeExecutionPort,
        journal: ExecutionJournal,
        policy: ExecutionPolicy = ExecutionPolicy(),
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._mutex = threading.RLock()
        self.client, self.journal, self.policy, self.sleep = (
            client,
            journal,
            policy,
            sleep,
        )
        self.wallet = Wallet()
        self.wallet.deposit(
            policy.allocated_mxn, "F3 allocated capital; not exchange total"
        )
        self.accounting = ConfirmedFillAccounting(self.wallet)
        self.orders: dict[str, TrackedOrder] = {}
        self.halts: set[HaltReason] = set()
        self.baseline: tuple[ExchangeBalance, ...] | None = None
        self.started = False
        self.last_reconciliation: tuple[str, ...] = ()
        policy_data = {
            "allocated_mxn": policy.allocated_mxn,
            "deployment_fraction": policy.deployment_fraction,
            "single_order_cap_mxn": policy.single_order_cap_mxn,
            "slippage_tolerance": policy.slippage_tolerance,
        }
        if not journal.records:
            journal.append(
                "session",
                {
                    "schema_version": "autofund.bitso.execution.v1",
                    "environment": "stage",
                    "policy": policy_data,
                },
            )
        header = parsing.obj(journal.records[0].get("data"))
        if header.get("environment") != "stage" or canonical_json(
            header.get("policy")
        ) != canonical_json(policy_data):
            raise ExchangeInvariantError("journal environment/capital policy mismatch")
        for event in journal.records[1:]:
            self._restore(
                parsing.text(event.get("kind")), parsing.obj(event.get("data"))
            )
        if isinstance(client, BitsoClient):
            for origin, tracked in self.orders.items():
                if tracked.oid:
                    client.register_known_order(tracked.oid, origin)

    @property
    def health(self) -> ExecutionHealth:
        return (
            ExecutionHealth.HALTED
            if self.halts or not self.started
            else ExecutionHealth.HEALTHY
        )

    def _restore(self, kind: str, row: dict[str, object]) -> None:
        if kind == "baseline":
            self.baseline = parsing.balances({"balances": row["balances"]})
        elif kind == "submit":
            request = restore_request(parsing.obj(row.get("request")))
            if request.origin_id in self.orders:
                raise ExchangeInvariantError("duplicate journal submission")
            self.orders[request.origin_id] = TrackedOrder(
                request, parsing.number(row.get("reserved_mxn"))
            )
        elif kind == "state":
            tracked = self.orders[parsing.text(row.get("origin_id"))]
            tracked.state = OrderState(parsing.text(row.get("state")))
            if row.get("oid"):
                oid = parsing.text(row.get("oid"))
                if tracked.oid and tracked.oid != oid:
                    raise ExchangeInvariantError("journal remote identity changed")
                tracked.oid = oid
        elif kind == "fill":
            self.accounting.apply(restore_fill(parsing.obj(row.get("fill"))))
            if fingerprint(self.wallet.ledger) != row.get("ledger_fingerprint"):
                raise ExchangeInvariantError("F0 ledger replay mismatch")
        elif kind == "halt":
            self.halts.add(HaltReason(parsing.text(row.get("reason"))))
        elif kind == "reconciliation":
            if row.get("match") is True:
                self.halts.discard(HaltReason.RECONCILIATION_HALT)
        else:
            raise ExchangeInvariantError("unknown execution journal event")

    @serialized
    def halt(self, reason: HaltReason) -> None:
        self.halts.add(reason)
        self.journal.append("halt", {"reason": reason})

    def _state(self, origin: str, state: OrderState, oid: str | None = None) -> None:
        tracked = self.orders[origin]
        if tracked.oid and oid and tracked.oid != oid:
            raise ExchangeInvariantError("remote order identity changed")
        self.journal.append(
            "state", {"origin_id": origin, "state": state, "oid": oid or tracked.oid}
        )
        tracked.state = state
        tracked.oid = oid or tracked.oid
        if isinstance(self.client, BitsoClient) and tracked.oid:
            self.client.register_known_order(tracked.oid, origin)

    @serialized
    def startup(self) -> tuple[str, ...]:
        result = self.reconcile()
        self.started = True
        return result

    @serialized
    @financial
    def prepare(
        self,
        instruction: ExecutionInstruction,
        book: Book,
        fees: FeeSchedule,
        reference_price: Decimal,
        *,
        origin: str | None = None,
    ) -> PreparedOrder:
        if self.health is not ExecutionHealth.HEALTHY:
            raise ExecutionHalted("execution must reconcile and be healthy")
        if any(o.state not in TERMINAL for o in self.orders.values()):
            raise ExecutionHalted("an unresolved order already reserves execution")
        prepared = ExchangePrecisionPolicy().prepare(
            instruction, book, fees, self.policy, origin_id(origin)
        )
        if prepared.request.origin_id in self.orders:
            raise BitsoValidationError(
                "logical identity already used; resubmission prohibited"
            )
        intent = instruction.intent
        capital = CapitalManager(self.policy.deployment_fraction)
        risk = RiskEngine(book.minimum_value)
        marks = {intent.market: reference_price}
        if intent.side is Side.BUY:
            assert intent.budget_mxn is not None
            risk.check_buy(
                self.wallet,
                capital,
                intent.market,
                intent.budget_mxn,
                reference_price,
                marks,
            )
            if (
                intent.budget_mxn + self.wallet.deployed_value(marks)
                > self.policy.allocated_mxn * self.policy.deployment_fraction
            ):
                raise BitsoValidationError("fixed allocated capital envelope exceeded")
            if (
                intent.budget_mxn > self.policy.single_order_cap_mxn
                or prepared.reserved_mxn > intent.budget_mxn
            ):
                raise BitsoValidationError(
                    "single-order cap or requested budget exceeded"
                )
        else:
            assert intent.quantity is not None
            risk.check_sell(
                self.wallet, intent.market, intent.quantity, reference_price
            )
            if (
                intent.quantity
                * max(reference_price, prepared.request.price or reference_price)
                > self.policy.single_order_cap_mxn
            ):
                raise BitsoValidationError("single-order cap exceeded")
        return prepared

    @serialized
    def submit(
        self,
        instruction: ExecutionInstruction,
        book: Book,
        fees: FeeSchedule,
        reference_price: Decimal,
        *,
        confirmed_stage: bool = False,
        authorized_by: str = "explicit-stage-command",
        origin: str | None = None,
    ) -> TrackedOrder:
        if not confirmed_stage:
            raise BitsoValidationError("Stage execution requires explicit confirmation")
        # Reconcile immediately before risk and submission, not just at startup.
        self.reconcile()
        prepared = self.prepare(instruction, book, fees, reference_price, origin=origin)
        request = prepared.request
        try:
            balances = {b.currency: b for b in self.client.get_balances()}
        except BitsoAuthenticationError:
            self.halt(HaltReason.AUTH_HALT)
            raise
        currency = (
            book.minor_currency if request.side is Side.BUY else book.major_currency
        )
        needed = prepared.reserved_mxn if request.side is Side.BUY else request.major
        if (
            currency not in balances
            or needed is None
            or balances[currency].available < needed
        ):
            raise BitsoValidationError(
                "exchange available balance cannot cover request"
            )
        self.journal.append(
            "submit",
            {
                "logical_order_id": request.origin_id,
                "origin_id": request.origin_id,
                "attempt": 1,
                "request": request.payload(),
                "requested": instruction,
                "reserved_mxn": prepared.reserved_mxn,
                "estimated_fee_mxn": prepared.estimated_fee_mxn,
                "adjustments": prepared.adjustments,
                "limits": book,
                "fees": fees,
                "allocated_cash": self.wallet.cash_mxn,
                "authorized_by": authorized_by,
                "risk": "PASS",
                "capital": "PASS",
            },
        )
        tracked = TrackedOrder(request, prepared.reserved_mxn)
        self.orders[request.origin_id] = tracked
        try:
            oid = self.client.place_order(request)
            self._state(request.origin_id, OrderState.ACKNOWLEDGED, oid)
        except BitsoUnknownSubmissionOutcome:
            self._state(request.origin_id, OrderState.UNKNOWN)
            self.halt(HaltReason.RECONCILIATION_HALT)
            for attempt in range(self.policy.reconciliation_attempts):
                self.reconcile()
                if tracked.oid is not None:
                    break
                if attempt + 1 < self.policy.reconciliation_attempts:
                    self.sleep(self.policy.polling_seconds)
        except BitsoAuthenticationError:
            self._state(request.origin_id, OrderState.REJECTED)
            self.halt(HaltReason.AUTH_HALT)
            raise
        except BitsoError:
            self._state(request.origin_id, OrderState.REJECTED)
            raise
        # Unexpected exceptions leave the durable SUBMITTED record unresolved.
        return tracked

    @serialized
    def cancel(self, origin: str) -> bool:
        origin_id(origin)
        tracked = self.orders.get(origin)
        if tracked is None or tracked.oid is None:
            raise BitsoValidationError("cannot cancel unknown AutoFund order")
        if tracked.state in TERMINAL:
            return False
        try:
            cancelled = self.client.cancel_order(tracked.oid, origin)
            if cancelled:
                self._state(origin, OrderState.CANCELLED)
            else:
                self.halt(HaltReason.RECONCILIATION_HALT)
            return cancelled
        except BitsoUnknownSubmissionOutcome:
            self.halt(HaltReason.RECONCILIATION_HALT)
            return False
        except BitsoAuthenticationError:
            self.halt(HaltReason.AUTH_HALT)
            raise
        finally:
            self.reconcile()

    def _match(self, origin: str, oid: str, book: str, side: Side) -> TrackedOrder:
        tracked = self.orders[origin]
        if (
            tracked.request.book != book
            or tracked.request.side != side
            or (tracked.oid and tracked.oid != oid)
        ):
            raise ExchangeInvariantError("remote/local order identity mismatch")
        if tracked.oid is None:
            self._state(origin, OrderState.ACKNOWLEDGED, oid)
        return tracked

    def _apply(self, fill: ExchangeTradeFill) -> bool:
        if fill.origin_id is None or fill.origin_id not in self.orders:
            raise ExchangeInvariantError("unowned fill cannot enter allocated wallet")
        tracked = self._match(
            fill.origin_id, fill.exchange_order_id, fill.book, fill.side
        )
        previous = self.accounting.processed_fill_ids.get(fill.trade_id)
        if previous is not None:
            if previous != fill:
                raise ExchangeInvariantError("contradictory duplicate fill")
            return False
        existing = [
            f
            for f in self.accounting.processed_fill_ids.values()
            if f.origin_id == fill.origin_id
        ]
        quantity = (
            sum((f.major_quantity for f in existing), Decimal("0"))
            + fill.major_quantity
        )
        value = sum((f.minor_value for f in existing), Decimal("0")) + fill.minor_value
        if tracked.request.major is not None and quantity > tracked.request.major:
            raise ExchangeInvariantError("fills exceed requested quantity")
        if tracked.request.minor is not None and value > tracked.request.minor:
            raise ExchangeInvariantError("fills exceed quote budget")
        if tracked.request.price is not None and (
            (fill.side is Side.BUY and fill.price > tracked.request.price)
            or (fill.side is Side.SELL and fill.price < tracked.request.price)
        ):
            raise ExchangeInvariantError("fill violates limit price")
        candidate = copy.deepcopy(self.accounting)
        candidate.apply(fill)
        self.journal.append(
            "fill",
            {"fill": fill, "ledger_fingerprint": fingerprint(candidate.wallet.ledger)},
        )
        self.accounting.apply(fill)
        if tracked.request.major is not None and quantity == tracked.request.major:
            self._state(fill.origin_id, OrderState.COMPLETED)
        elif tracked.request.minor is not None and value == tracked.request.minor:
            self._state(fill.origin_id, OrderState.COMPLETED)
        elif tracked.state not in TERMINAL:
            self._state(fill.origin_id, OrderState.PARTIALLY_FILLED)
        return True

    @serialized
    @financial
    def reconcile(self) -> tuple[str, ...]:
        findings: list[str] = []
        dangerous: list[str] = []
        try:
            remote_open = self.client.get_open_orders()
            user_fills = self.client.get_user_trades()
            remote_by_origin: dict[str, RemoteOrder] = {}
            for order in remote_open:
                if not order.origin_id or not order.origin_id.startswith("af-stage-"):
                    continue
                if order.origin_id not in self.orders:
                    dangerous.append("REMOTE_ORDER_UNKNOWN_LOCALLY")
                elif order.origin_id in remote_by_origin:
                    raise ExchangeInvariantError("multiple remote orders share origin")
                else:
                    remote_by_origin[order.origin_id] = order
            gathered: list[ExchangeTradeFill] = []
            for fill in user_fills:
                if fill.origin_id and fill.origin_id.startswith("af-stage-"):
                    if fill.origin_id not in self.orders:
                        dangerous.append("REMOTE_FILL_UNKNOWN_LOCALLY")
                    else:
                        gathered.append(fill)
            lookups: dict[str, RemoteOrder] = {}
            for origin, tracked in self.orders.items():
                rows = self.client.get_order(origin=origin)
                if len(rows) > 1:
                    raise ExchangeInvariantError("multiple order identities")
                if rows:
                    row = rows[0]
                    if row.origin_id != origin:
                        raise ExchangeInvariantError("lookup origin mismatch")
                    self._match(origin, row.oid, row.book, row.side)
                    lookups[origin] = row
                fills = self.client.get_order_trades(origin=origin)
                if any(f.origin_id != origin for f in fills):
                    raise ExchangeInvariantError("order-trades origin mismatch")
                gathered.extend(fills)
                if origin in remote_by_origin:
                    opened = remote_by_origin[origin]
                    self._match(origin, opened.oid, opened.book, opened.side)
                    if origin not in lookups:
                        lookups[origin] = opened
            for fill in sorted(gathered, key=lambda f: (f.timestamp, f.trade_id)):
                applied = self._apply(fill)
                findings.append("UNSEEN_REMOTE_FILL" if applied else "DUPLICATE_FILL")
            for origin, tracked in self.orders.items():
                observed = lookups.get(origin)
                related = [
                    f
                    for f in self.accounting.processed_fill_ids.values()
                    if f.origin_id == origin
                ]
                qty = sum((f.major_quantity for f in related), Decimal("0"))
                value = sum((f.minor_value for f in related), Decimal("0"))
                if (
                    tracked.state not in TERMINAL
                    and related
                    and (
                        (
                            tracked.request.major is not None
                            and qty == tracked.request.major
                        )
                        or (
                            tracked.request.minor is not None
                            and value == tracked.request.minor
                        )
                    )
                ):
                    self._state(origin, OrderState.COMPLETED)
                cash_spent = sum(
                    (
                        f.minor_value + (f.confirmed_fee or Decimal("0"))
                        if f.fee_currency == "mxn"
                        else f.minor_value
                        for f in related
                    ),
                    Decimal("0"),
                )
                if (
                    tracked.request.side is Side.BUY
                    and cash_spent > tracked.reserved_mxn
                ):
                    dangerous.append("ORDER_BUDGET_EXCEEDED")
                if observed is not None:
                    if (
                        tracked.request.major is not None
                        and observed.original_amount != tracked.request.major
                    ):
                        raise ExchangeInvariantError(
                            "remote order amount differs from request"
                        )
                    if (
                        tracked.request.price is not None
                        and observed.price != tracked.request.price
                    ):
                        raise ExchangeInvariantError(
                            "remote order price differs from request"
                        )
                    if observed.original_amount - observed.unfilled_amount != qty:
                        dangerous.append("FILL_QUANTITY_MISMATCH")
                    if tracked.state in TERMINAL and observed.state not in TERMINAL:
                        dangerous.append("TERMINAL_ORDER_REMOTE_OPEN")
                    elif observed.state is OrderState.COMPLETED and not related:
                        dangerous.append("COMPLETED_WITHOUT_CONFIRMED_FILLS")
                    else:
                        self._state(origin, observed.state, observed.oid)
                elif tracked.state not in TERMINAL:
                    dangerous.append("LOCAL_ORDER_MISSING_REMOTELY")
                if tracked.state is OrderState.UNKNOWN or (
                    tracked.oid is None and tracked.state not in TERMINAL
                ):
                    dangerous.append("AMBIGUOUS_ORDER_STATE")
            balances = self.client.get_balances()
            if self.baseline is None:
                if self.orders or self.accounting.processed_fill_ids:
                    raise ExchangeInvariantError(
                        "cannot invent initial balance snapshot"
                    )
                self.journal.append("baseline", {"balances": balances})
                self.baseline = balances
            else:
                expected = {b.currency: b.total for b in self.baseline}
                relevant = {b.currency for b in self.baseline} | {
                    b.currency for b in balances
                }
                for tracked in self.orders.values():
                    relevant.update(tracked.request.book.split("_"))
                for fill in self.accounting.processed_fill_ids.values():
                    major, minor = fill.book.split("_")
                    sign = Decimal("1") if fill.side is Side.BUY else Decimal("-1")
                    expected[major] = (
                        expected.get(major, Decimal("0")) + sign * fill.major_quantity
                    )
                    expected[minor] = (
                        expected.get(minor, Decimal("0")) - sign * fill.minor_value
                    )
                    assert (
                        fill.fee_currency is not None and fill.confirmed_fee is not None
                    )
                    expected[fill.fee_currency] = (
                        expected.get(fill.fee_currency, Decimal("0"))
                        - fill.confirmed_fee
                    )
                actual = {b.currency: b.total for b in balances}
                if any(
                    actual.get(c, Decimal("0")) != expected.get(c, Decimal("0"))
                    for c in relevant
                ):
                    dangerous.append("BALANCE_MISMATCH")
            self.wallet.assert_invariants()
            if dangerous:
                self.halt(HaltReason.RECONCILIATION_HALT)
            else:
                self.halts.discard(HaltReason.RECONCILIATION_HALT)
            result = tuple(dict.fromkeys([*findings, *dangerous])) or ("MATCH",)
            self.journal.append(
                "reconciliation",
                {
                    "match": not dangerous,
                    "findings": result,
                    "balances": balances,
                    "open_orders": remote_open,
                    "ledger_fingerprint": fingerprint(self.wallet.ledger),
                },
            )
            self.last_reconciliation = result
            return result
        except BitsoAuthenticationError:
            self.halt(HaltReason.AUTH_HALT)
            raise
        except Exception:
            self.halt(HaltReason.ACCOUNTING_HALT)
            raise

"""Manual single-order execution. Every uncertain write is recovered by GET."""

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from typing import Any, TextIO
from uuid import uuid4

from autofund.decimal_utils import decimal, financial
from autofund.exchanges.bitso import parsing
from autofund.exchanges.bitso.accounting import ConfirmedFillAccounting
from autofund.exchanges.bitso.models import (
    ExchangeTradeFill,
    OrderState,
    RemoteOrder,
    identifier,
)
from autofund.models import Side
from autofund.replay.serialization import canonical_json, fingerprint
from autofund.wallet import Wallet

from .client import ORIGIN, BitsoProductionLiveClient, _SubmissionPermit
from .journal import LiveExecutionJournal
from .models import LiveConfig, LiveError, LiveOrderIntent, LiveSellIntent, Preflight
from .preflight import preflight

STATES = {"INTENT_CREATED", "PREFLIGHT_PASS", "AWAITING_OPERATOR", "SUBMITTING", "SUBMITTED",
          "ACKNOWLEDGED", "OUTCOME_UNKNOWN", "PARTIALLY_FILLED", "FILLED", "RECONCILED", "HALTED"}

# Bounded, deterministic backoff for the ACK recovery window. Doubling from a
# small base bounded by a cap keeps the probe count low while still covering
# exchange trade-visibility lag. These are policy constants, not tuning knobs.
BACKOFF_BASE_SECONDS = Decimal("1")
BACKOFF_CAP_SECONDS = Decimal("10")


def restore_fill(row: dict[str, object]) -> ExchangeTradeFill:
    maker = row.get("is_maker")
    return ExchangeTradeFill(
        parsing.text(row.get("trade_id")), parsing.text(row.get("exchange_order_id")),
        parsing.text(row["origin_id"]) if row.get("origin_id") else None,
        parsing.text(row.get("book")), Side(parsing.text(row.get("side"))),
        parsing.number(row.get("major_quantity")), parsing.number(row.get("minor_value")), parsing.number(row.get("price")),
        datetime.fromisoformat(parsing.text(row.get("timestamp"))),
        maker if isinstance(maker, bool) else None,
        parsing.number(row["confirmed_fee"]) if row.get("confirmed_fee") is not None else None,
        parsing.text(row["fee_currency"]) if row.get("fee_currency") else None)


class LiveExecution:
    def __init__(self, client: BitsoProductionLiveClient, journal: LiveExecutionJournal,
                 config: LiveConfig, *, emit: Callable[[str, dict[str, Any]], None] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic_clock: Callable[[], float] = monotonic) -> None:
        self.client, self.journal, self.config = client, journal, config
        self._sleep = sleep
        self._clock = monotonic_clock
        self.wallet = Wallet()
        self.accounting = ConfirmedFillAccounting(self.wallet)
        self.intents: dict[str, dict[str, Any]] = {}
        self.states: dict[str, str] = {}
        self.state_metadata: dict[str, dict[str, object]] = {}
        # Last bounded-reconciliation result per origin (in-memory diagnostics only).
        self.reconciliation_outcomes: dict[str, dict[str, Any]] = {}
        self.submitting: set[str] = set()
        self.oids: dict[str, str] = {}
        self._last_orderbook_sequence: int | None = None
        self._emit = emit
        if not journal.records:
            journal.append("LIVE_ALLOCATION", {"allocated_capital": config.allocated_capital})
        try:
            for event in journal.records:
                self._restore(str(event["kind"]), parsing.obj(event["data"]))
            self.wallet.assert_invariants()
        except Exception:
            raise LiveError("LIVE_JOURNAL_REQUIRES_MANUAL_RECOVERY") from None

    def _restore(self, kind: str, data: dict[str, object]) -> None:
        if kind == "LIVE_ALLOCATION":
            if self.wallet.ledger or parsing.number(data.get("allocated_capital")) != self.config.allocated_capital:
                raise LiveError("LIVE_ALLOCATION_MISMATCH")
            self.wallet.deposit(self.config.allocated_capital, "AutoFund live allocation; no exchange transfer")
        elif kind == "LIVE_INTENT_CREATED":
            origin = parsing.text(data.get("origin_id"))
            if not ORIGIN.fullmatch(origin) or origin in self.intents or self.unresolved:
                raise LiveError("LIVE_SINGLE_UNRESOLVED_INVARIANT")
            if (data.get("book"), data.get("side"), data.get("order_type"), data.get("intent_source")) != (
                    "btc_mxn", "buy", "market", "OPERATOR_CERTIFICATION"):
                raise LiveError("INVALID_LIVE_INTENT")
            if origin != "af-live-" + parsing.text(data.get("intent_id")):
                raise LiveError("INVALID_LIVE_INTENT_NAMESPACE")
            if not 0 < parsing.number(data.get("minor_budget")) <= self.config.single_order_cap:
                raise LiveError("INVALID_LIVE_BUDGET")
            self.intents[origin] = data
            self.states[origin] = "INTENT_CREATED"
            self.client.register_origin(origin)
        elif kind == "LIVE_SELL_INTENT_CREATED":
            origin = parsing.text(data.get("origin_id"))
            quantity = parsing.number(data.get("major_quantity"))
            position = self.wallet.positions.get("BTC/MXN")
            if (not ORIGIN.fullmatch(origin) or origin in self.intents or self.unresolved
                    or not position or quantity <= 0 or quantity > position.quantity):
                raise LiveError("INVALID_LIVE_SELL_INTENT")
            if (data.get("book"), data.get("side"), data.get("order_type"), data.get("intent_source")) != (
                    "btc_mxn", "sell", "market", "MVP_AUTONOMOUS"):
                raise LiveError("INVALID_LIVE_SELL_INTENT")
            self.intents[origin] = data
            self.states[origin] = "INTENT_CREATED"
            self.client.register_origin(origin)
        elif kind == "LIVE_FILL":
            origin = parsing.text(data.get("origin_id"))
            fill = restore_fill(parsing.obj(data.get("fill")))
            self._validate_fill(origin, fill)
            self.accounting.apply(fill)
            self.oids[origin] = fill.exchange_order_id
        elif kind == "LIVE_STATE":
            origin, state = parsing.text(data.get("origin_id")), parsing.text(data.get("state"))
            if state not in STATES or origin not in self.intents or self.states[origin] == "RECONCILED":
                raise LiveError("INVALID_LIVE_STATE_SEQUENCE")
            if state == "PREFLIGHT_PASS" and self.states[origin] != "INTENT_CREATED":
                raise LiveError("INVALID_LIVE_STATE_SEQUENCE")
            if state == "AWAITING_OPERATOR" and self.states[origin] != "PREFLIGHT_PASS":
                raise LiveError("INVALID_LIVE_STATE_SEQUENCE")
            if state == "RECONCILED" and self.states[origin] not in {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED"}:
                raise LiveError("RECONCILIATION_WITHOUT_FILL_PROOF")
            if state == "SUBMITTING":
                if origin in self.submitting or self.states[origin] != "AWAITING_OPERATOR":
                    raise LiveError("LIVE_DUPLICATE_SUBMISSION")
                self.submitting.add(origin)
            if state in {"SUBMITTED", "ACKNOWLEDGED", "OUTCOME_UNKNOWN", "PARTIALLY_FILLED", "FILLED", "RECONCILED"} and origin not in self.submitting:
                raise LiveError("LIVE_STATE_WITHOUT_SUBMISSION")
            self.states[origin] = state
            self.state_metadata[origin] = dict(data)
            if data.get("oid"):
                oid = identifier(parsing.text(data["oid"]))
                if origin in self.oids and oid != self.oids[origin]:
                    raise LiveError("CONTRADICTORY_REAL_ORDER_ID")
                self.oids[origin] = oid
        elif kind == "LIVE_BALANCE_EVIDENCE":
            # Private evidence never creates or changes an F0 ledger entry.
            pass
        else:
            raise LiveError("UNKNOWN_LIVE_JOURNAL_EVENT")

    @property
    def unresolved(self) -> tuple[str, ...]:
        return tuple(origin for origin, state in self.states.items()
                     if state != "RECONCILED" and not (state == "HALTED" and origin not in self.submitting))

    @property
    def blocked(self) -> tuple[str, ...]:
        """Origins whose financial outcome is not established.

        Any entry here means new writes must not be admitted until a GET-only
        recovery resolves it. A blocked order never authorises another POST.
        """
        return tuple(origin for origin, state in self.states.items()
                     if state in {"ACKNOWLEDGED", "SUBMITTED", "OUTCOME_UNKNOWN", "PARTIALLY_FILLED"}
                     or (state == "HALTED" and origin in self.submitting
                         and self.state_metadata.get(origin, {}).get("reconciliation_state") == "BLOCKED"))

    def state(self, origin: str, state: str, **metadata: object) -> None:
        data = {"origin_id": origin, "state": state, **metadata}
        self.journal.append("LIVE_STATE", data)
        self._restore("LIVE_STATE", parsing.obj(json.loads(canonical_json(data))))
        if self._emit:
            try:
                self._emit(state, {**self.public(), **data})
            except Exception:
                pass  # Monitoring failure cannot change financial behavior.

    def emit(self, event: str, **fields: Any) -> None:
        if self._emit:
            try:
                self._emit(event, {**self.public(), **fields})
            except Exception:
                pass

    def check(self, budget: Decimal | None = None) -> Preflight:
        checked = preflight(self.client, self.config, self.wallet, unresolved=bool(self.unresolved), budget=budget,
                            prior_sequence=self._last_orderbook_sequence)
        if checked.checks["orderbook_sequence"]:
            self._last_orderbook_sequence = checked.depth.sequence
        return checked

    def create(self, checked: Preflight) -> LiveOrderIntent:
        if self.unresolved:
            raise LiveError("RECOVER_UNRESOLVED_LIVE_ORDER_FIRST")
        intent = LiveOrderIntent.create(checked)
        self.journal.append("LIVE_INTENT_CREATED", intent)
        self._restore("LIVE_INTENT_CREATED", parsing.obj(json.loads(canonical_json(intent))))
        if self._emit:
            try:
                self._emit("INTENT_CREATED", self.public())
            except Exception:
                pass
        self.state(intent.origin_id, "PREFLIGHT_PASS")
        self.state(intent.origin_id, "AWAITING_OPERATOR")
        return intent

    def _check_sell_market(self, quantity: Decimal) -> Any:
        position = self.wallet.positions.get("BTC/MXN")
        if not position or quantity <= 0 or quantity > position.quantity:
            raise LiveError("SELL_EXCEEDS_AUTOFUND_INVENTORY")
        fees = self.client.fees()
        limits = self.client.available_books()
        depth = self.client.order_book()
        received_monotonic = monotonic()
        now = datetime.now(UTC)
        future = (depth.timestamp - now).total_seconds()
        age = (now - depth.timestamp).total_seconds()
        value = quantity * depth.best_bid
        minimum_execution_price = depth.best_bid * (1 - (self.config.slippage_tolerance or Decimal("0")) / 100)
        bid_quantity = sum((level.amount for level in depth.bids if level.price >= minimum_execution_price), Decimal("0"))
        if (future > 2 or age > self.config.max_market_age_seconds or depth.spread_bps > self.config.max_spread_bps
                or Decimal(str(monotonic() - received_monotonic)) > self.config.max_local_snapshot_age_seconds
                or not limits.minimum_amount <= quantity <= limits.maximum_amount
                or not limits.minimum_value <= value <= limits.maximum_value
                or not limits.minimum_price <= minimum_execution_price <= depth.best_bid <= limits.maximum_price
                or bid_quantity < quantity or fees.taker_fee_decimal < 0):
            raise LiveError("FINAL_SELL_MARKET_CHECK_REJECTED")
        if self._last_orderbook_sequence is not None and depth.sequence < self._last_orderbook_sequence:
            raise LiveError("ORDERBOOK_SEQUENCE_REGRESSION")
        self._last_orderbook_sequence = depth.sequence
        return depth

    def create_sell(self, quantity: Decimal) -> LiveSellIntent:
        if self.unresolved:
            raise LiveError("RECOVER_UNRESOLVED_LIVE_ORDER_FIRST")
        depth = self._check_sell_market(quantity)
        intent_id = uuid4().hex
        intent = LiveSellIntent(intent_id, "af-live-" + intent_id, datetime.now(UTC), quantity, fingerprint(depth))
        self.journal.append("LIVE_SELL_INTENT_CREATED", intent)
        self._restore("LIVE_SELL_INTENT_CREATED", parsing.obj(json.loads(canonical_json(intent))))
        self.state(intent.origin_id, "PREFLIGHT_PASS")
        self.state(intent.origin_id, "AWAITING_OPERATOR")
        return intent

    def submit_sell_authorized(self, intent: LiveSellIntent) -> None:
        origin = intent.origin_id
        if origin not in self.intents or self.states.get(origin) != "AWAITING_OPERATOR" or origin in self.submitting:
            raise LiveError("INTENT_NOT_AWAITING_OPERATOR")
        self._check_sell_market(intent.major_quantity)
        payload = {"book": "btc_mxn", "side": "sell", "type": "market", "major": str(intent.major_quantity),
                   "origin_id": origin, "slippage_tolerance": str(self.config.slippage_tolerance)}
        self.state(origin, "SUBMITTING")
        permit = _SubmissionPermit(json.dumps(payload, separators=(",", ":")).encode())
        try:
            oid = self.client.place_market_sell(payload, permit)
        except Exception:
            self.state(origin, "OUTCOME_UNKNOWN")
            self.recover(origin)
            return
        self.state(origin, "SUBMITTED", oid=oid)
        self.state(origin, "ACKNOWLEDGED", oid=oid)
        self.recover(origin)

    def confirm_and_submit(self, intent: LiveOrderIntent, *, confirm_real_money: bool,
                           stdin: TextIO, stdout: TextIO) -> None:
        origin = intent.origin_id
        if origin not in self.intents or self.states.get(origin) != "AWAITING_OPERATOR" or origin in self.submitting:
            raise LiveError("INTENT_NOT_AWAITING_OPERATOR")
        if canonical_json(intent) != canonical_json(self.intents[origin]):
            raise LiveError("INTENT_CHANGED_AFTER_JOURNAL_COMMIT")
        blocked = any(os.getenv(name) for name in (
            "CI", "PYTEST_CURRENT_TEST", "PLAYWRIGHT_TEST", "AUTOFUND_AUTO_CONFIRM", "CODEX_THREAD_ID"))
        if not confirm_real_money or not stdin.isatty() or not stdout.isatty() or blocked:
            self.state(origin, "HALTED", reason="INTERACTIVE_OPERATOR_REQUIRED")
            raise LiveError("INTERACTIVE_OPERATOR_REQUIRED")
        print("REAL MONEY — BITSO PRODUCTION — BTC/MXN — BUY", file=stdout)
        print(canonical_json(intent.preflight), file=stdout)
        print("origin_id: " + origin, file=stdout)
        print("Type exactly: CONFIRM " + origin, file=stdout, flush=True)
        if stdin.readline().rstrip("\r\n") != "CONFIRM " + origin:
            self.state(origin, "HALTED", reason="OPERATOR_CONFIRMATION_REJECTED")
            raise LiveError("OPERATOR_CONFIRMATION_REJECTED")
        self.submit_authorized(intent)

    def submit_authorized(self, intent: LiveOrderIntent) -> None:
        """Submit one BUY after a containing session has already been strongly authorized."""
        origin = intent.origin_id
        if origin not in self.intents or self.states.get(origin) != "AWAITING_OPERATOR" or origin in self.submitting:
            raise LiveError("INTENT_NOT_AWAITING_OPERATOR")
        if canonical_json(intent) != canonical_json(self.intents[origin]):
            raise LiveError("INTENT_CHANGED_AFTER_JOURNAL_COMMIT")
        if not self.client.credentials.permissions_confirmed or self.config.slippage_tolerance is None:
            self.state(origin, "HALTED", reason="LIVE_POLICY_MISSING")
            raise LiveError("LIVE_POLICY_MISSING")
        # Confirmation authorizes one attempt, but never authorizes use of the
        # previously displayed quote. Re-read every market/economic input and
        # re-run all gates before creating a submission permit.
        final = preflight(self.client, self.config, self.wallet, unresolved=False,
                          budget=intent.minor_budget, prior_sequence=self._last_orderbook_sequence)
        if not final.ready:
            reason = final.failures[0] if final.failures else "FINAL_PREFLIGHT_FAILED"
            self.state(origin, "HALTED", reason=reason)
            raise LiveError(reason)
        self._last_orderbook_sequence = final.depth.sequence
        self._balance_evidence(origin, "BEFORE")
        payload = {"book": "btc_mxn", "side": "buy", "type": "market", "minor": str(intent.minor_budget),
                   "origin_id": origin, "slippage_tolerance": str(self.config.slippage_tolerance)}
        self.state(origin, "SUBMITTING")  # Durable reservation BEFORE any POST.
        permit = _SubmissionPermit(json.dumps(payload, separators=(",", ":")).encode())
        try:
            oid = self.client.place_market_order(payload, permit)
        except Exception:
            self.state(origin, "OUTCOME_UNKNOWN")
            self.recover(origin)
            return
        self.state(origin, "SUBMITTED", oid=oid)
        self.state(origin, "ACKNOWLEDGED", oid=oid)
        self.recover(origin)

    def _balance_evidence(self, origin: str, phase: str) -> None:
        self.journal.append("LIVE_BALANCE_EVIDENCE", {"origin_id": origin, "phase": phase,
                                                     "balances": self.client.balances()})

    @financial
    def _validate_fill(self, origin: str, fill: ExchangeTradeFill) -> None:
        expected_side = Side(str(self.intents.get(origin, {}).get("side", "buy")).upper())
        if origin not in self.intents or origin not in self.submitting or fill.book != "btc_mxn" or fill.side is not expected_side:
            raise LiveError("UNRELATED_REAL_FILL_REJECTED")
        if fill.origin_id is not None and fill.origin_id != origin:
            raise LiveError("UNRELATED_REAL_FILL_REJECTED")
        if origin in self.oids and self.oids[origin] != fill.exchange_order_id:
            raise LiveError("CONTRADICTORY_REAL_ORDER_ID")
        if fill.timestamp < datetime.fromisoformat(str(self.intents[origin]["created_at"])):
            raise LiveError("PREEXISTING_REAL_FILL_REJECTED")
        old = self.accounting.processed_fill_ids.get(fill.trade_id)
        if old is not None:
            if old != fill:
                raise LiveError("CONTRADICTORY_DUPLICATE_REAL_FILL")
            return
        own = [x for x in self.accounting.processed_fill_ids.values() if x.exchange_order_id == fill.exchange_order_id]
        if expected_side is Side.BUY:
            gross = sum((x.minor_value for x in own), Decimal("0")) + fill.minor_value
            if gross > Decimal(str(self.intents[origin]["minor_budget"])):
                raise LiveError("REAL_FILL_EXCEEDS_INTENT_BUDGET")
        else:
            quantity = sum((x.major_quantity for x in own), Decimal("0")) + fill.major_quantity
            if quantity > Decimal(str(self.intents[origin]["major_quantity"])):
                raise LiveError("REAL_FILL_EXCEEDS_OWNED_SELL_QUANTITY")
        quote_fees = sum((x.confirmed_fee or Decimal("0") for x in (*own, fill) if x.fee_currency == "mxn"), Decimal("0"))
        if expected_side is Side.BUY and gross + quote_fees > self.config.single_order_cap:
            raise LiveError("REAL_FILL_EXCEEDS_ALL_IN_CAP")

    def recover(self, origin: str | None = None) -> None:
        origins = (origin,) if origin else self.unresolved
        for known in origins:
            if known not in self.intents:
                raise LiveError("UNKNOWN_LIVE_ORDER")
            if known not in self.submitting:
                self.state(known, "HALTED", reason="ABANDONED_BEFORE_POST")
                continue
            if self.states[known] == "RECONCILED":
                continue
            historical = self.states[known] == "HALTED"
            self.emit("ORDER_RECOVERY_STARTED" if historical else "RECONCILIATION_STARTED",
                      origin_id=known)
            try:
                self._recover_one(known)
            except Exception as exc:
                message = str(exc)
                reason = ("CONTRADICTORY_REMOTE_STATE" if "CONTRADICTORY" in message or "UNRELATED" in message
                          else "HALTED_UNCERTAIN_ORDER")
                self.state(known, "HALTED", reason=reason)

    @financial
    def reconcile_outcome(self, origin: str, *, deadline: float | None = None) -> str:
        """Bounded GET-only reconciliation after an acknowledged order.

        Returns one deterministic terminal outcome:

        - ``EXECUTED_RECOVERED``    the order is fully filled and committed
        - ``PARTIALLY_FILLED_RECOVERED`` terminal remote evidence with partial fills
        - ``OPEN_ORDER``            the exchange still reports it live at deadline
        - ``CANCELLED`` / ``REJECTED`` proven by remote state without fills
        - ``CONTRADICTORY_REMOTE_STATE`` remote evidence contradicts local state
        - ``UNKNOWN``               no conclusive evidence within the window

        The exchange may acknowledge an order before its trades become visible, so
        a single immediate lookup is not sufficient evidence. This polls with a
        deterministic backoff for at most ``reconciliation_window_seconds`` and
        then stops. It never submits anything: the transport rejects any non-GET
        here, and a permit is never created.
        """
        started = self._monotonic()
        window = float(decimal(self.config.reconciliation_window_seconds,
                               "reconciliation_window_seconds"))
        limit = deadline if deadline is not None else started + window
        attempt = 0
        last: tuple[tuple[RemoteOrder, ...], tuple[ExchangeTradeFill, ...]] = ((), ())
        while True:
            attempt += 1
            # Already reconciled: recovery is a clean no-op. Re-entering the state
            # machine would be both pointless and unsafe.
            if self.states.get(origin) == "RECONCILED":
                self._record_outcome(origin, attempt, "ALREADY_RECONCILED")
                return "ALREADY_RECONCILED"
            orders: tuple[RemoteOrder, ...] = ()
            fills: tuple[ExchangeTradeFill, ...] = ()
            try:
                orders = self.client.lookup_order(origin)
            except LiveError:
                # Two distinct real cases reach here and both are expected:
                #  - a fully executed market order is no longer served by the
                #    order lookup endpoint (Production returns 0312 for completed
                #    market orders, verified against the successful SELL too);
                #  - the order is still propagating.
                # Trade evidence is the authoritative source and is always queried.
                orders = ()
            for order in orders:
                expected_side = Side(str(self.intents[origin].get("side", "buy")).upper())
                if order.origin_id != origin or order.book != "btc_mxn" or order.side is not expected_side:
                    self._record_outcome(origin, attempt, "CONTRADICTORY_REMOTE_STATE")
                    self._emit_reconciliation(origin, attempt, 0, "CONTRADICTORY_REMOTE_STATE")
                    return "CONTRADICTORY_REMOTE_STATE"
                if order.state is OrderState.REJECTED:
                    self._record_outcome(origin, attempt, "REJECTED")
                    self._emit_reconciliation(origin, attempt, 0, "REJECTED")
                    return "REJECTED"
                self.state(origin, "ACKNOWLEDGED", oid=order.oid)
            try:
                fills = self.client.order_trades(origin)
            except LiveError:
                fills = ()
            committed = self._commit_fills(origin, fills)
            last = (orders, fills)
            if committed:
                self._emit_reconciliation(origin, attempt, committed, "FILLS_OBSERVED")
            outcome = self._classify_outcome(origin, orders, fills)
            if outcome is not None:
                self._record_outcome(origin, attempt, outcome)
                return outcome
            remaining = limit - self._monotonic()
            if remaining <= 0:
                break
            # Deterministic bounded backoff: 1s, 2s, 4s, capped at 10s.
            self._sleep(min(float(BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))), float(BACKOFF_CAP_SECONDS), remaining))
        orders, fills = last
        outcome = "OPEN_ORDER" if any(order.state in {OrderState.OPEN, OrderState.PARTIALLY_FILLED,
                                                     OrderState.ACKNOWLEDGED, OrderState.SUBMITTED}
                                     for order in orders) else "UNKNOWN"
        self.reconciliation_outcomes[origin] = {"attempts": attempt, "outcome": outcome,
                                                "window_seconds": str(self.config.reconciliation_window_seconds)}
        self._emit_reconciliation(origin, attempt, 0, outcome)
        return outcome

    def _record_outcome(self, origin: str, attempts: int, outcome: str) -> None:
        """In-memory diagnostics only; never a durable state transition."""
        self.reconciliation_outcomes[origin] = {"attempts": attempts, "outcome": outcome,
                                                "window_seconds": str(self.config.reconciliation_window_seconds)}

    def _monotonic(self) -> float:
        return self._clock()

    def _commit_fills(self, origin: str, fills: tuple[ExchangeTradeFill, ...]) -> int:
        """Commit new fills once, idempotently. Returns the number committed."""
        committed = 0
        for fill in fills:
            self._validate_fill(origin, fill)
            if fill.trade_id in self.accounting.processed_fill_ids:
                continue
            candidate = Wallet()
            candidate.deposit(self.config.allocated_capital)
            probe = ConfirmedFillAccounting(candidate)
            for previous in self.accounting.processed_fill_ids.values():
                probe.apply(previous)
            probe.apply(fill)
            self.journal.append("LIVE_FILL", {"origin_id": origin, "fill": fill})
            self.accounting.apply(fill)
            committed += 1
            self.emit("FILL_RECOVERED", origin_id=origin, trade_id=fill.trade_id,
                      oid=fill.exchange_order_id, side=fill.side.value,
                      major_quantity=fill.major_quantity, minor_value=fill.minor_value,
                      confirmed_fee=fill.confirmed_fee, fee_currency=fill.fee_currency)
            self.state(origin, "PARTIALLY_FILLED", oid=fill.exchange_order_id)
        return committed

    def _classify_outcome(self, origin: str, orders: tuple[RemoteOrder, ...],
                          fills: tuple[ExchangeTradeFill, ...]) -> str | None:
        """Terminal classification, or None when more evidence is needed."""
        own = [x for x in self.accounting.processed_fill_ids.values()
               if x.exchange_order_id == self.oids.get(origin)]
        expected_side = Side(str(self.intents[origin].get("side", "buy")).upper())
        if expected_side is Side.BUY:
            complete = sum((x.minor_value for x in own), Decimal("0")) == Decimal(str(self.intents[origin]["minor_budget"]))
        else:
            complete = sum((x.major_quantity for x in own), Decimal("0")) == Decimal(str(self.intents[origin]["major_quantity"]))
        cancelled = any(order.state is OrderState.CANCELLED for order in orders)
        remote_terminal = any(order.state is OrderState.COMPLETED for order in orders)
        # A market BUY commits the requested budget; a market SELL commits the
        # requested quantity. Either is complete only with matching fill evidence.
        if complete:
            self._finalize_reconciled(origin, own, "BUDGET_FULLY_FILLED")
            return "EXECUTED_RECOVERED"
        # Cancellation is authoritative terminal evidence. Any fills already
        # observed are real and stand; a cancelled order never fills further.
        if cancelled:
            self._finalize_reconciled(origin, own, "REMOTE_CANCELLED")
            return "PARTIALLY_FILLED_RECOVERED" if own else "CANCELLED"
        if remote_terminal and own:
            self._finalize_reconciled(origin, own, "REMOTE_COMPLETED_WITH_CONFIRMED_FILLS")
            return "PARTIALLY_FILLED_RECOVERED"
        return None

    def _finalize_reconciled(self, origin: str, own: list[ExchangeTradeFill], evidence: str) -> None:
        if own:
            self.state(origin, "FILLED")
        self._balance_evidence(origin, "AFTER")
        self.wallet.assert_invariants()
        self.state(origin, "RECONCILED",
                   provenance=fingerprint({"intent": self.intents[origin], "fills": own,
                                           "ledger": self.wallet.ledger}),
                   terminal_evidence=evidence,
                   balance_reconciliation="PRIVATE_EVIDENCE_ONLY_EXTERNAL_ACTIVITY_NOT_ATTRIBUTED")
        self.emit("ORDER_RECOVERED", origin_id=origin, oid=self.oids.get(origin),
                  fills=len(own), state="RECONCILED")
        self.emit("POSITION_RECOVERED", origin_id=origin,
                  inventory_btc=self.public()["inventory_btc"], cash_mxn=self.public()["cash_mxn"])

    def _emit_reconciliation(self, origin: str, attempts: int, fills: int, outcome: str) -> None:
        self.emit("RECONCILIATION_PROBE", origin_id=origin, attempts=attempts,
                  fills_committed=fills, outcome=outcome,
                  window_seconds=str(self.config.reconciliation_window_seconds))

    def _recover_one(self, origin: str) -> None:
        outcome = self.reconcile_outcome(origin)
        if outcome in {"EXECUTED_RECOVERED", "PARTIALLY_FILLED_RECOVERED", "CANCELLED"}:
            return
        if outcome == "REJECTED":
            self.state(origin, "HALTED", reason="REMOTE_REJECTED_NO_FILLS")
            return
        if outcome == "CONTRADICTORY_REMOTE_STATE":
            self.state(origin, "HALTED", reason="CONTRADICTORY_REMOTE_STATE")
            return
        # OPEN_ORDER or UNKNOWN: the outcome is not established. Fail closed and
        # never retry the POST.
        self.state(origin, "HALTED", reason="HALTED_UNCERTAIN_ORDER", reconciliation_state="BLOCKED",
                   reconciliation_outcome=outcome)

    @financial
    def public(self) -> dict[str, Any]:
        position = self.wallet.positions.get("BTC/MXN")
        fills = tuple(self.accounting.processed_fill_ids.values())
        buys = tuple(fill for fill in fills if fill.side is Side.BUY)
        sells = tuple(fill for fill in fills if fill.side is Side.SELL)
        fees: dict[str, Decimal] = {}
        for fill in fills:
            if fill.confirmed_fee is not None and fill.fee_currency is not None:
                fees[fill.fee_currency] = fees.get(fill.fee_currency, Decimal("0")) + fill.confirmed_fee
        fees_mxn = sum((entry.fee_mxn for entry in self.wallet.ledger), Decimal("0"))
        sell_fees_mxn = sum((entry.fee_mxn for entry in self.wallet.ledger
                             if entry.type.value == "SELL"), Decimal("0"))
        net_realized = self.wallet.realized_pnl()
        def aggregate(rows: tuple[ExchangeTradeFill, ...]) -> dict[str, Any]:
            major = sum((fill.major_quantity for fill in rows), Decimal("0"))
            minor = sum((fill.minor_value for fill in rows), Decimal("0"))
            return {"quantity": major, "value_mxn": minor,
                    "vwap": minor / major if major else None}
        return {"mode": "MICRO-LIVE", "real_money": True, "auto_execution": "DISABLED",
                "allocated_capital": self.config.allocated_capital,
                "max_deployment_mxn": self.config.allocated_capital * self.config.max_deployment,
                "single_order_cap": self.config.single_order_cap, "cash_mxn": self.wallet.cash_mxn,
                "inventory_btc": position.quantity if position else Decimal("0"),
                "cost_basis_mxn": position.cost_basis_mxn if position else Decimal("0"),
                "realized_pnl_mxn": net_realized,
                "gross_realized_pnl_mxn": net_realized + sell_fees_mxn,
                "fees_mxn": fees_mxn, "ledger": self.wallet.ledger,
                "fills": fills, "fill_count": len(fills), "order_count": len(self.intents),
                "buy": aggregate(buys), "sell": aggregate(sells),
                "fees_by_currency": fees,
                "reconciliation_state": "PASS" if not self.unresolved else "BLOCKED",
                "unresolved_orders": self.unresolved,
                "orders": [{"origin_id": key, "state": state, "oid": self.oids.get(key),
                            "recovery_state": (
                                "RECONCILED" if state == "RECONCILED" else
                                "PARTIALLY_FILLED_RECOVERED" if state == "PARTIALLY_FILLED" else
                                "INTENT_NOT_SUBMITTED" if key not in self.submitting else
                                "CONTRADICTORY_REMOTE_STATE" if self.state_metadata.get(key, {}).get("reason") == "CONTRADICTORY_REMOTE_STATE" else
                                "POST_OUTCOME_UNKNOWN")}
                           for key, state in self.states.items()]}

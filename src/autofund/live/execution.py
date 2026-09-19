"""Manual single-order execution. Every uncertain write is recovered by GET."""

import json
import os
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any, TextIO

from autofund.decimal_utils import financial
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
from .models import LiveConfig, LiveError, LiveOrderIntent, Preflight
from .preflight import preflight

STATES = {"INTENT_CREATED", "PREFLIGHT_PASS", "AWAITING_OPERATOR", "SUBMITTING", "SUBMITTED",
          "ACKNOWLEDGED", "OUTCOME_UNKNOWN", "PARTIALLY_FILLED", "FILLED", "RECONCILED", "HALTED"}


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
                 config: LiveConfig, *, emit: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        self.client, self.journal, self.config = client, journal, config
        self.wallet = Wallet()
        self.accounting = ConfirmedFillAccounting(self.wallet)
        self.intents: dict[str, dict[str, Any]] = {}
        self.states: dict[str, str] = {}
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

    def state(self, origin: str, state: str, **metadata: object) -> None:
        data = {"origin_id": origin, "state": state, **metadata}
        self.journal.append("LIVE_STATE", data)
        self._restore("LIVE_STATE", parsing.obj(json.loads(canonical_json(data))))
        if self._emit:
            try:
                self._emit(state, self.public())
            except Exception:
                pass  # Monitoring failure cannot change financial behavior.

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
        if origin not in self.intents or origin not in self.submitting or fill.book != "btc_mxn" or fill.side is not Side.BUY:
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
        gross = sum((x.minor_value for x in own), Decimal("0")) + fill.minor_value
        if gross > Decimal(str(self.intents[origin]["minor_budget"])):
            raise LiveError("REAL_FILL_EXCEEDS_INTENT_BUDGET")
        quote_fees = sum((x.confirmed_fee or Decimal("0") for x in (*own, fill) if x.fee_currency == "mxn"), Decimal("0"))
        if gross + quote_fees > self.config.single_order_cap:
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
            try:
                self._recover_one(known)
            except Exception:
                self.state(known, "HALTED", reason="HALTED_UNCERTAIN_ORDER")

    @financial
    def _recover_one(self, origin: str) -> None:
        for _ in range(self.config.reconciliation_attempts):
            orders: tuple[RemoteOrder, ...] = ()
            try:
                orders = self.client.lookup_order(origin)
            except LiveError:
                pass  # Filled orders may be absent; always query trade evidence.
            for order in orders:
                if order.origin_id != origin or order.book != "btc_mxn" or order.side is not Side.BUY:
                    raise LiveError("UNRELATED_ORDER_LOOKUP_REJECTED")
                self.state(origin, "ACKNOWLEDGED", oid=order.oid)
            try:
                fills = self.client.order_trades(origin)
            except LiveError:
                continue
            for fill in fills:
                self._validate_fill(origin, fill)
                if fill.trade_id in self.accounting.processed_fill_ids:
                    continue
                # Validate the whole F0 application before committing. Recovery
                # uses the same primitive and committed fills after a crash.
                candidate = Wallet()
                candidate.deposit(self.config.allocated_capital)
                probe = ConfirmedFillAccounting(candidate)
                for previous in self.accounting.processed_fill_ids.values():
                    probe.apply(previous)
                probe.apply(fill)
                self.journal.append("LIVE_FILL", {"origin_id": origin, "fill": fill})
                self.accounting.apply(fill)
                self.state(origin, "PARTIALLY_FILLED", oid=fill.exchange_order_id)
            own = [x for x in self.accounting.processed_fill_ids.values() if x.exchange_order_id == self.oids.get(origin)]
            gross = sum((x.minor_value for x in own), Decimal("0"))
            complete_budget = gross == Decimal(str(self.intents[origin]["minor_budget"]))
            cancelled = any(order.state is OrderState.CANCELLED for order in orders)
            completed_with_fills = bool(own) and any(order.state is OrderState.COMPLETED for order in orders)
            if complete_budget or cancelled or completed_with_fills:
                if complete_budget:
                    self.state(origin, "FILLED")
                self._balance_evidence(origin, "AFTER")
                self.wallet.assert_invariants()
                self.state(origin, "RECONCILED", provenance=fingerprint({"intent": self.intents[origin],
                           "fills": own, "ledger": self.wallet.ledger}),
                           terminal_evidence="BUDGET_FULLY_FILLED" if complete_budget else "REMOTE_CANCELLED" if cancelled else "REMOTE_COMPLETED_WITH_CONFIRMED_FILLS",
                           balance_reconciliation="PRIVATE_EVIDENCE_ONLY_EXTERNAL_ACTIVITY_NOT_ATTRIBUTED")
                return
        self.state(origin, "HALTED", reason="HALTED_UNCERTAIN_ORDER")

    @financial
    def public(self) -> dict[str, Any]:
        position = self.wallet.positions.get("BTC/MXN")
        return {"mode": "MICRO-LIVE", "real_money": True, "auto_execution": "DISABLED",
                "allocated_capital": self.config.allocated_capital,
                "max_deployment_mxn": self.config.allocated_capital * self.config.max_deployment,
                "single_order_cap": self.config.single_order_cap, "cash_mxn": self.wallet.cash_mxn,
                "inventory_btc": position.quantity if position else Decimal("0"),
                "cost_basis_mxn": position.cost_basis_mxn if position else Decimal("0"),
                "realized_pnl_mxn": self.wallet.realized_pnl(), "ledger": self.wallet.ledger,
                "unresolved_orders": self.unresolved,
                "orders": [{"origin_id": key, "state": state, "oid": self.oids.get(key)}
                           for key, state in self.states.items()]}

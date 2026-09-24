# AutoFund MVP 0.1.2 — Acknowledged-Order Reconciliation Hardening

Baseline `ceb370b`.

## The defect

During the real Production session `mvp-20260923T122029-896130b5`, the second
autonomous BUY was acknowledged by the exchange and then failed reconciliation
approximately 460 ms later:

```
13:09:06.311448Z  ORDER_SUBMITTED        oid e4noqPIc3YmZVm0E
13:09:06.316739Z  ORDER_ACKNOWLEDGED
13:09:06.319474Z  RECONCILIATION_STARTED
13:09:06.776519Z  RECONCILIATION_FAIL    -> HALTED (HALTED_UNCERTAIN_ORDER)
```

The order was real, the money moved, and AutoFund did not know the outcome.
`report.json` recorded `reconciliation_state = BLOCKED` and `portfolio = null`.

## Root cause

`reconciliation_attempts` is a **count, not a window**. `_recover_one` ran its
three attempts back to back with no delay, so the entire recovery completed in
about 150 ms per attempt — far shorter than the exchange's eventual trade
visibility. Verified read-only against Production:

| endpoint | result |
| --- | --- |
| `GET /api/v3/orders?origin_ids=<origin>` | HTTP 400 code `0312`, "OID incorrecto" — also for the **successfully filled SELL** |
| `GET /api/v3/open_orders` | `[]` — not open |
| `GET /api/v3/order_trades?origin_id=<origin>` | `200` with the trade, but only after the visibility lag |

So a completed market order is served by neither the order lookup nor the
open-order list, and its trade evidence is not immediately available. The old
code concluded "unresolved" from incomplete evidence and halted. It was not a
parsing bug, not a lookup-ordering bug, and not a wrong API shape: it was a
recovery window that was too short.

## The fix

`LiveExecution.reconcile_outcome(origin)` performs a **bounded, deterministic,
GET-only** reconciliation:

- polls for at most `reconciliation_window_seconds` (default 90 s, hard maximum
  `MAX_RECONCILIATION_WINDOW_SECONDS` = 300 s, enforced in `LiveConfig`);
- backs off deterministically 1, 2, 4, 8, then 10 s, never exceeding the
  remaining window;
- always queries trade evidence, which is authoritative; order lookup is
  best-effort because completed market orders are not served there;
- never creates a submission permit, so a second POST is structurally impossible.

It returns exactly one deterministic classification:

| outcome | meaning |
| --- | --- |
| `EXECUTED_RECOVERED` | filled and committed; budget or quantity fully matched |
| `PARTIALLY_FILLED_RECOVERED` | terminal remote evidence with partial fills |
| `CANCELLED` | remote cancellation with no fills |
| `REJECTED` | remote rejection without fills |
| `OPEN_ORDER` | exchange still reports it live at the deadline |
| `CONTRADICTORY_REMOTE_STATE` | remote evidence contradicts local state |
| `UNKNOWN` | no conclusive evidence within the window |
| `ALREADY_RECONCILED` | idempotent no-op on re-entry |

If the window expires without a conclusive result, the order becomes
`HALTED` with `reconciliation_state = BLOCKED` and the application fails closed.

## Fail-closed application state

An acknowledged order with no established outcome must not keep presenting a
normally RUNNING session. `AutoFundOrchestrator._on_order_blocked` disables
automatic execution, records `AUTO_HALT_TRIGGERED` with
`reconciliation_state = BLOCKED`, and enters `HALTED` with stop reason
`BLOCKED_RECOVERY_UNRESOLVED_ORDER`. Market observation and the read-only market
scanner continue; no new Production order is possible in that state.

## Signal suppression

Actionable strategy decisions are counted independently of admission, so a
suppressed signal is never silently discarded. `SIGNAL_SUPPRESSED_PENDING_ORDER`
is emitted when a decision cannot be admitted because an unresolved order blocks
new financial intents, and the report carries `signal_admission` with
`strategy_buy_decisions`, `strategy_sell_decisions`, `signals_admitted`,
`signals_suppressed_pending_order` and `actionable_decisions_not_admitted`.

For the real session this reports 61 BUY decisions, 1 SELL decision, 2 admitted
signals and 60 BUY decisions not admitted, with
`suppression_basis = DERIVED_FROM_DECISION_AND_ADMISSION_COUNTS` (the dedicated
checkpoint did not exist in 0.1.1).

## Wallet freshness

The exported wallet view carries `observed_at` and is refreshed before a
diagnostics export and at finalization. A startup snapshot is never presented as
current. The wallet remains read-only.

## Round-trip economic evidence

`round_trip_economics` reports the factual completed round trip and raises
`GROSS_PROFIT_CONSUMED_BY_FEES` when `gross_pnl > 0` and `net_pnl <= 0`:

- buy cost `10.91486406 MXN`, sell gross `10.97745480 MXN`
- gross realized `+0.06259074 MXN`, total fees `0.17370475 MXN`
- net realized `-0.02303341 MXN`

Positive gross price movement was insufficient to cover observed execution
costs. **No threshold is changed automatically.**

## Runtime gaps

`runtime_gaps` reports telemetry gaps beyond 600 s with the last event, next
event and duration. The real session shows a 47,838 s gap (host suspend) plus a
3,674 s gap, so its reported `actual_runtime_seconds` of 69,772 s overstates real
monitoring for a 36,000 s configured duration. `monitored_seconds` subtracts
detected gaps.

An internal watchdog evaluates the duration guard every 5 s independently of HTTP
polling, so the guard fires on resume rather than waiting for a client poll. A
gap larger than 60 s is recorded as a `RUNTIME_GAP` checkpoint. Time with no
executing process is never treated as successful market monitoring.

The specific cause of the real session's gap is **not** proven by the available
evidence; the observation is consistent with host suspend, but the artifacts do
not rule out event-loop starvation or process starvation, so no cause is
asserted.

## Recovery of the second BUY (GET-only, 0 POSTs)

```
origin_id: af-live-300062ff2d0e40299cf47d1045be4b04
oid:       e4noqPIc3YmZVm0E
trade_id:  201590729
result:    EXECUTED_RECOVERED
```

| field | value |
| --- | --- |
| side | BUY |
| major quantity | `0.00000733` BTC |
| minor value | `10.91486406 MXN` |
| price | `1488470` |
| fee | `0.00000006 BTC` |
| executed_at | `2026-09-23T13:09:06Z` |

Reconstructed AutoFund accounting (independently verified from the journal):

| field | value |
| --- | --- |
| cash | `39.06210253 MXN` |
| AutoFund-owned BTC | `0.00000727` |
| position | OPEN, cost basis `10.91486406 MXN` |
| realized P&L | `-0.02303341 MXN` (belongs to the closed SELL round trip) |

Repeated recovery returns `ALREADY_RECONCILED` and duplicates no fill, ledger
entry or inventory. Production POST count during this implementation: **0**.

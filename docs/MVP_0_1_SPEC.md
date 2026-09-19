# AutoFund MVP 0.1

`autofund app` is the normal product entry point. It runs one localhost FastAPI process, serves the compiled React SPA, creates the authoritative `AutoFundOrchestrator`, performs startup recovery, and opens the default browser once. It binds `127.0.0.1:8000` by default and never starts automatic execution on boot.

The application state machine is `BOOTING -> RECOVERING -> STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED`, with `HALTED` and `ERROR` fail-safe states. Restart always clears session authorization. `START` requires the exact phrase `START AUTOFUND REAL 50`; `STOP` blocks new work and finalizes telemetry; `KILL` immediately blocks new writes while preserving the process and reconciliation capability. Kill never liquidates.

Hard product bounds are 50 MXN authorized capital, 25 MXN deployment and 11 MXN per order. Session loss, duration and order count are bounded by the backend. The browser has no order, cancellation, transfer, withdrawal or exchange endpoint. Its only mutations are `POST /api/v1/control/start`, `/stop` and `/kill`, protected by same-origin validation and a random in-memory control token.

Production orders remain server-side. F5's one-intent/one-POST rule, final GET validation, origin IDs, single unresolved order, ambiguous outcome recovery and confirmed-fill accounting remain authoritative. AutoFund inventory comes from its journal and ledger, never total exchange balances. A non-executable capital floor yields no order.

## Production readiness inside the product lifecycle

Production preflight is part of the application, not a separate CLI step. After credentials, connectivity, startup reconciliation and accounting pass with zero unresolved orders, `autofund app` runs the existing F5 GET-only Production preflight once and then reaches `STOPPED`. Readiness is exposed in the MVP read model as `production_preflight` (`PASS` / `FAIL` plus the exact blocker names) and rendered in Overview as `Production preflight READY / BLOCKED`. Startup never performs an exchange POST and never enables automatic execution.

`POST /api/v1/control/start` does not rely on that earlier result. After the operator authorizes the real-money session it re-runs the same GET-only preflight against fresh market data and applies the same existing guards (connectivity, reconciliation, market and local snapshot freshness, exchange timestamp sanity, sequence, spread, depth, dynamic minimum, fee/cap executability and slippage). Only if it passes does the application go `STARTING -> RUNNING` with automatic execution on. No order POST happens merely by starting a session; intents remain gated by the existing server-side policy.

If the fresh preflight fails, automatic execution stays off, zero order POSTs occur, the application returns to `STOPPED` (not `HALTED`, since nothing was written and no safety semantic requires a halt) and the control plane answers `409` with `PRODUCTION_PREFLIGHT_BLOCKED` plus the precise blocker list. `HALTED` remains reserved for genuine safety conditions such as reconciliation failure, key switch and loss limit. There is no bypass, no reusable stale result and no manual "skip preflight" control.

The deterministic `--demo` application uses the real control API without importing the Bitso client. It certifies autonomous BUY/fill/ledger and SELL/fill/P&L presentation without real money or network access.

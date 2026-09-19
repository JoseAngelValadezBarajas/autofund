# AutoFund MVP 0.1

`autofund app` is the normal product entry point. It runs one localhost FastAPI process, serves the compiled React SPA, creates the authoritative `AutoFundOrchestrator`, performs startup recovery, and opens the default browser once. It binds `127.0.0.1:8000` by default and never starts automatic execution on boot.

The application state machine is `BOOTING -> RECOVERING -> STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED`, with `HALTED` and `ERROR` fail-safe states. Restart always clears session authorization. `START` requires the exact phrase `START AUTOFUND REAL 50`; `STOP` blocks new work and finalizes telemetry; `KILL` immediately blocks new writes while preserving the process and reconciliation capability. Kill never liquidates.

Hard product bounds are 50 MXN authorized capital, 25 MXN deployment and 11 MXN per order. Session loss, duration and order count are bounded by the backend. The browser has no order, cancellation, transfer, withdrawal or exchange endpoint. Its only mutations are `POST /api/v1/control/start`, `/stop` and `/kill`, protected by same-origin validation and a random in-memory control token.

Production orders remain server-side. F5's one-intent/one-POST rule, final GET validation, origin IDs, single unresolved order, ambiguous outcome recovery and confirmed-fill accounting remain authoritative. AutoFund inventory comes from its journal and ledger, never total exchange balances. A non-executable capital floor yields no order.

## Live operator observability

A REAL MONEY session must be understandable from the browser alone. The MVP does not add a second observability subsystem: `MvpObservability` is a read-only projection that reuses the F4.6 runtime contract (`RuntimeView` / `RuntimeEvent`, including the open-candle shape, heartbeat semantics and bounded event buffer) and publishes it in the MVP read model as `runtime`, `observability`, `market_state`, `pipeline`, `strategy`, `candles`, `metrics` and `session`.

Overview becomes a live operator view while RUNNING: session (state, id, elapsed, remaining, orders used vs max, session loss limit), market (reference, bid, ask, spread, quality, last market event age, last closed candle, sequence), a candlestick chart of recent closed 1m candles with the current open candle visually distinguished, strategy (champion, regime, last evaluation, last decision, signal, reason), the full pipeline MARKET → CANDLE → STRATEGY → SIGNAL → CAPITAL → RISK → FINAL MARKET CHECK → ORDER → FILL → RECONCILIATION → LEDGER with each stage's latest result, a compact activity tail, and operational metrics. Market, Activity and Telemetry become real live pages; Activity is filterable by component, level and event with a newest-first toggle. Positions shows AutoFund-owned quantity, average cost, mark, market value, realized and unrealized P&L and fees, or an explicit NONE — never unrelated Bitso inventory.

The strategy continues to consume only closed candles. The open candle is folded from real polled prices and is explicitly OBSERVATIONAL ONLY; it is never a strategy input, and the pipeline, decisions and accounting are computed exactly as before.

No data is fabricated. In REAL MONEY mode nothing is synthesized to make the dashboard look active: when a value is unavailable the UI shows UNKNOWN or WAITING FOR DATA, and the first minute is explicitly distinguished as RUNNING BUT WAITING FOR FIRST MARKET EVENT, RUNNING AND PROCESSING MARKET DATA, MARKET DATA STALE or MARKET DATA DISCONNECTED.

An observability health guard reports OBSERVABILITY DEGRADED when a RUNNING session loses BOTH internal runtime heartbeat visibility and operator publication beyond the timeout. It is backend-authoritative: it concerns backend telemetry/runtime health, never whether a browser tab is open, and it has no trading effect. Closing the browser never halts trading.

## Production readiness inside the product lifecycle

Production preflight is part of the application, not a separate CLI step. After credentials, connectivity, startup reconciliation and accounting pass with zero unresolved orders, `autofund app` runs the existing F5 GET-only Production preflight once and then reaches `STOPPED`. Readiness is exposed in the MVP read model as `production_preflight` (`PASS` / `FAIL` plus the exact blocker names) and rendered in Overview as `Production preflight READY / BLOCKED`. Startup never performs an exchange POST and never enables automatic execution.

`POST /api/v1/control/start` does not rely on that earlier result. After the operator authorizes the real-money session it re-runs the same GET-only preflight against fresh market data and applies the same existing guards (connectivity, reconciliation, market and local snapshot freshness, exchange timestamp sanity, sequence, spread, depth, dynamic minimum, fee/cap executability and slippage). Only if it passes does the application go `STARTING -> RUNNING` with automatic execution on. No order POST happens merely by starting a session; intents remain gated by the existing server-side policy.

If the fresh preflight fails, automatic execution stays off, zero order POSTs occur, the application returns to `STOPPED` (not `HALTED`, since nothing was written and no safety semantic requires a halt) and the control plane answers `409` with `PRODUCTION_PREFLIGHT_BLOCKED` plus the precise blocker list. `HALTED` remains reserved for genuine safety conditions such as reconciliation failure, key switch and loss limit. There is no bypass, no reusable stale result and no manual "skip preflight" control.

The deterministic `--demo` application uses the real control API without importing the Bitso client. It certifies autonomous BUY/fill/ledger and SELL/fill/P&L presentation without real money or network access.

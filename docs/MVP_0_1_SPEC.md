# AutoFund MVP 0.1

`autofund app` is the normal product entry point. It runs one localhost FastAPI process, serves the compiled React SPA, creates the authoritative `AutoFundOrchestrator`, performs startup recovery, and opens the default browser once. It binds `127.0.0.1:8000` by default and never starts automatic execution on boot.

The application state machine is `BOOTING -> RECOVERING -> STOPPED -> STARTING -> RUNNING -> STOPPING -> STOPPED`, with `HALTED` and `ERROR` fail-safe states. Restart always clears session authorization. `START` requires the exact phrase `START AUTOFUND REAL 50`; `STOP` blocks new work and finalizes telemetry; `KILL` immediately blocks new writes while preserving the process and reconciliation capability. Kill never liquidates.

Hard product bounds are 50 MXN authorized capital, 25 MXN deployment and 11 MXN per order. Session loss, duration and order count are bounded by the backend. The browser has no order, cancellation, transfer, withdrawal or exchange endpoint. Its only mutations are `POST /api/v1/control/start`, `/stop` and `/kill`, protected by same-origin validation and a random in-memory control token.

Production orders remain server-side. F5's one-intent/one-POST rule, final GET validation, origin IDs, single unresolved order, ambiguous outcome recovery and confirmed-fill accounting remain authoritative. AutoFund inventory comes from its journal and ledger, never total exchange balances. A non-executable capital floor yields no order.

The deterministic `--demo` application uses the real control API without importing the Bitso client. It certifies autonomous BUY/fill/ledger and SELL/fill/P&L presentation without real money or network access.

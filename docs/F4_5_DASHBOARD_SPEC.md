# AutoFund 0.6 / F4.5 Dashboard

F4.5 is a local, read-only monitoring SPA. `DashboardDataProvider` reads and
integrity-validates persisted F4 shadow artifacts, projects explicit Pydantic
read models, and never invokes a strategy, wallet mutation or exchange client.
It uses a small mtime/size cache and only complete JSONL records accepted by F4.

FastAPI exposes only versioned GET routes under `/api/v1`, plus GET SSE
`/api/v1/stream`. No mutation route or trading, cancellation, transfer,
withdrawal or configuration handler exists. Defaults bind to `127.0.0.1`.
Security headers prevent MIME sniffing and cross-site referrer disclosure; no
wildcard CORS is configured. API models use decimal strings and UTC timestamps.
Real account balances, credentials and authorization values are not projected.

Run `autofund dashboard --demo` for offline golden data, or `autofund dashboard
--session artifacts/f4/live_session` for the certified capture. The React/Vite
build is served from `frontend/dist`; development uses `npm run dev` in
`frontend/`. The demo labels the same SHADOW and disabled-trading safety state.

The frontend presents Overview, Market, Activity, Ledger, Sessions and System
tabs, quality counters, equity and candle visualization panels. It is a SPA,
has no SSR, and has no financial action buttons. SSE supplies snapshot updates;
the client retains the last REST snapshot if the connection fails.

Known limits: F4 artifacts contain closed candles only, so no open candle is
invented. The current UI uses lightweight built-in visualization panels; a
future read-only refinement can replace them with a chart library without
changing the API. F5 remains outside this package and must never be enabled by
this dashboard.

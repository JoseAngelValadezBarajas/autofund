# F4.6 — Live Runtime Observability

Baseline `b355ea3`. SHADOW monitoring only. No F5 or real-money functionality.

## Runtime and durable truth

Wallet, Ledger, journals, closed candles and manifests remain financial truth.
`RuntimePublisher` observes F4 **after** capture consumption and copies the existing
`session.result()` into an ephemeral projection. It never feeds execution, recovery
or replay. Atomic replacements in `artifacts/runtime/current.json` are not a new
journal/database. Session UUID, timing, browser state and SSE are excluded from
financial fingerprints. Frozen financial core files are unchanged.

`GET /api/v1/runtime` is a versioned Pydantic model: session ID, mode SHADOW,
started UTC, elapsed seconds, market/interval/strategy, quality, risk/accounting,
last_event_at, last_market_event_at, last_closed_candle_at, last_state_update_at,
market snapshot, open candle and activity. Decimal values are strings; times UTC.
Missing accounting is UNKNOWN. Completed artifact-only views still validate F4
manifest integrity and deterministic replay.

STARTING means setup; RUNNING means public processing; STOPPING means finalization;
STOPPED means clean finalization; HALTED reflects a core halt; DISCONNECTED means
interrupted reads or an expired active producer heartbeat. Failed startup/finalization is HALTED,
with UNKNOWN accounting if no authoritative accounting snapshot exists. Fast STOPPING may be
coalesced into STOPPED.

## Open candle and heartbeat

OPEN CANDLE uses accepted, deduplicated pending trades in the current UTC minute,
sorted by timestamp/trade ID. OHLC/last, Decimal volume/count and interval bounds
use only those trades. The excluded initial partial minute and other buckets are
not projected. No trades means null. It is informational, never a strategy input
or durable CLOSED candle. Closed candlestick charts render API closed OHLC.

The writer heartbeats every 0.5 seconds. Backend-defined semantics: active producer
older than 10 seconds is DISCONNECTED; market event older than 15 seconds while
the producer is healthy is STALE; otherwise LIVE. STOPPED/HALTED retain their
terminal status and STALE heartbeat. React derives display age/countdown only;
countdown is hidden unless RUNNING/LIVE/connected and never affects strategy.

## Events, SSE and isolation

Session-local monotonic IDs and a buffer of the newest 500 events are used.
Events: SESSION_STARTING, SESSION_STARTED, MARKET_EVENT, MARKET_UNAVAILABLE,
CANDLE_CLOSED, STRATEGY_EVALUATED, NO_SIGNAL, SIGNAL_GENERATED,
SHADOW_ORDER_INTENT, CAPITAL_CHECK, RISK_CHECK, SHADOW_FILL, SHADOW_REJECTED,
LEDGER_UPDATED, QUALITY_CHANGED, SESSION_HALTED, SESSION_STOPPED, RECOVERY.
Successful core fills prove capital/risk PASS. Combined rejection results do not
invent all intermediate decisions. No F5 real-order events are added.

A writer thread consumes one coalesced immutable snapshot slot. File I/O is
outside the runner loop; publication failures cannot affect durable accounting.
Transient Windows file-sharing conflicts are coalesced into the next writer tick;
terminal publication has five bounded retry attempts. Important events remain in the bounded buffer when updates coalesce; evicted
durable financial history remains available from F4 artifacts. SSE consumers
only read snapshots and have no callback/queue into the runner. Informal projection
measurement on 31 captured frames: median 0.782 ms, maximum 1.111 ms, versus the
default 5-second poll interval.

Existing `/api/v1/stream` sends named `snapshot` envelopes with runtime, overview
and quality every three seconds. Native EventSource reconnect triggers a REST
reload; replacing snapshots avoids duplicate activity. Missing financial data
does not terminate SSE. Disconnect keeps the last financial snapshot; no false
zeroes. The ephemeral buffer is not a durable event-delivery guarantee.

## Dashboard lifecycle and demo

`shadow run` starts/reuses localhost dashboard, waits for health readiness, opens
the OS default browser once, then starts public processing. `--no-open-dashboard`
skips server/browser startup while publishing state for an existing dashboard.
Replay, reports, tests and administrative commands do not launch a browser.
Build once in frontend with `npm ci` / `npm run build`; SPA assets load locally.

The runner terminates only its own dashboard process; reused servers remain
available showing STOPPED. Run `autofund dashboard` separately to retain monitoring
after completion. Browser/server failures are best effort and cannot change risk
or execution behavior. Readiness refuses demo/foreign servers. Default bind is
127.0.0.1, URL http://127.0.0.1:8000.

`dashboard --demo` retains static goldens. `dashboard --demo-live --port 8001`
adds a repeating 24-second display scenario: running/trades/open, close/evaluation/
NO_SIGNAL, quality change, stop. It invokes no engine/exchange. Open candle values
are in a fixture, UTC timestamps are fixed, and DEMO DATA stays visible.

## Validation and security

```powershell
.venv\Scripts\python -m pytest
.venv\Scripts\python -m ruff check .
.venv\Scripts\python -m mypy --strict src/autofund examples
cd frontend
npm ci
npx playwright install chromium
npm run test
npm run lint
npm run build
npm run test:e2e
```

Playwright starts isolated demo servers on 8010/8001, never reusing real servers.
Desktop 1440x900/narrow 390x844 exercise read-only journeys, eight static golden
captures, two OPEN candle goldens and live lifecycle. Browser installation is a
setup step; tests require no market Internet/credentials. Vitest never launches
Chromium. Failure traces and review captures remain ignored local artifacts.
Disconnect/reconnect retention is tested at component level through the actual
SPA callbacks and mocked EventSource/fetch, avoiding invasive E2E transport hacks.
Backend tests cover SSE envelopes, corrupt-file retention, candle derivation,
bounds, I/O isolation, launcher ownership and exact financial parity.

Dashboard endpoints remain GET/SSE only, no wildcard CORS, no trading controls,
write capability, real Bitso balances or credentials. Production reads remain
unchanged GET-only. No keys.json/credentials are used in F4.6 certification.

Limitations: one operational session per workspace owns the shared local snapshot;
it is trusted local output, not an authenticated remote protocol. Resume recovers
from durable F4 artifacts and creates a new observability ID. No new durable
storage, full telemetry platform or F5 functionality is introduced.

## Certification record

Offline gates: 470 backend passed / 3 opt-in network tests deselected; 10 frontend
passed; Chromium E2E 6 passed / 0 failed. Lint/build/Ruff/full-source mypy pass.
Public run: 2026-09-18 20:48:49–20:52:08 UTC, 198 seconds, 31 frames, 3 closed
candles, 3 evaluations, 0 actionable signals/fills, 45 runtime events. Chromium
observed RUNNING, OPEN, CLOSED and STOPPED; zero page/console errors or frontend
mutation requests. Replay fingerprint:
`7d22f28e94d77072fa5be100b284f5812701e027491c15518ec6e2cebb878e37`.

Visual review: eight static goldens, two OPEN candle goldens, desktop/narrow runtime
captures, and real running/open/activity/stopped captures in artifacts/f46/browser.
Final public verification: 20:57:51?21:00:09 UTC (137 seconds), 22 frames,
2 closed candles/evaluations, 0 actionable signals/fills, 33 events. Browser-open
count was exactly 1; Chromium again observed RUNNING/OPEN/CLOSED/STOPPED with
zero errors/writes. Final replay fingerprint:
`3e7dcf0a64a723f667157873ee3c5494a82dd2df4b76f1003f2754cdf6b54fc6`.
Final-build captures are in artifacts/f46/browser-final.

Issues fixed: overly tall narrow panel, wrong equity empty label on Market, and a
line mislabeled candlestick. Terminal STOPPED no longer suggests LIVE market data. Review checks clipping, contrast, chart sizing,
wrapping, local table scrolling, safety hierarchy and responsive composition;
no screenshot tolerance was increased.

No F5 work. F5 may be considered only after F4.6 PASS.

# AutoFund MVP operations runbook

## Start

Build once with `cd frontend; npm ci; npm run build`, configure dedicated live credentials in environment variables, then run `autofund app`. The app reconciles, runs the GET-only Production preflight itself and remains STOPPED. No separate CLI or preflight command is required before using the web application. In the browser review the 50/25/11 MXN envelope, the **Production preflight READY / BLOCKED** indicator and its exact blocker, then press **START AUTOFUND** and type `START AUTOFUND REAL 50`. START re-runs a fresh GET-only preflight before enabling automatic execution; if it fails, the app stays STOPPED with automatic execution off and shows the precise blocker, and the operator can simply retry.

## Live operator view

While RUNNING, Overview becomes a live operator view: session progress, live market (price, bid, ask, spread, quality, last market event age), a closed-candle chart with the current open candle, strategy evaluation and its reason, the MARKET → … → LEDGER pipeline with each stage's latest result, a live activity tail and operational metrics. **Market**, **Activity** (filterable by component, level and event) and **Telemetry** are live pages. UNKNOWN or WAITING FOR DATA means no data yet; nothing is invented. The open candle is observational only and never a strategy input.

`OBSERVABILITY DEGRADED` means the backend lost both runtime heartbeat and operator publication; it never halts trading and does not depend on whether a browser tab is open.

## Post-session review

After a STOP the session is frozen: `elapsed`, `ended_at` and `stop_reason` are
final and never advance again, and the runtime reports INACTIVE rather than STALE.
Review the **Sessions** page for start/end/actual duration/stop reason, and the
**Telemetry** page for the pipeline. Stages a completed NO_SIGNAL path never
reached show `NOT_APPLICABLE`; `UNKNOWN` means genuinely undeterminable.

`report.json`, `checkpoint_summary.json` and `handoff.json` are written per session
and follow the diagnostics export. `handoff.json` carries deterministic
`candidate_improvement_signals` such as `ZERO_SIGNALS_ACROSS_ELIGIBLE_EVALUATIONS`
and `EXECUTION_PATH_NOT_EXERCISED`. These are facts, not recommendations.

## Market scanner

The **Scanner** page and Learning page market section show read-only research
across the exchange's MXN books. It is GET-only, ranks research candidates (never
financial actions) and refreshes every `--scan-interval` seconds (default 300).
`--no-scanner` disables it. `MARKET_SCANNER_DEGRADED` means research is
unavailable; Production trading is unaffected. The Production market remains
BTC/MXN and cannot be changed from the browser.

## Stop and kill

**STOP SESSION** prevents new intents, finishes known reconciliation, flushes artifacts and returns to STOPPED. **EMERGENCY KILL** immediately blocks new writes, disables strategy execution and enters HALTED; it does not sell an open position. Keep the application alive for inspection and recovery.

## Restart and reconciliation

After process exit, launch `autofund app` again. Automatic execution is OFF. Startup loads durable state and reconciles any known origin before STOPPED. An unresolved/ambiguous order keeps the app HALTED. Never retry its POST; query order and trade evidence by origin ID.

Journal corruption or contradictory fills require manual artifact preservation and investigation. Do not delete the journal or attribute unrelated Bitso balances to AutoFund. A Bitso or market-data outage blocks new orders; retain read/reconciliation attempts and restart only after health recovers. A loss-limit halt cannot be bypassed in the same session.

## Diagnostics

Open Sessions and choose **Export session diagnostics**. Preserve the ZIP plus the session directory. It contains sanitized report, checkpoint summary, handoff and telemetry. It excludes keys, Authorization and unrelated balances.

For credential-free UX validation use `autofund app --demo --no-open-browser`. Demo data must never be treated as Production evidence.

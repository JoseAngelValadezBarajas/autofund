# AutoFund MVP operations runbook

## Start

Build once with `cd frontend; npm ci; npm run build`, configure dedicated live credentials in environment variables, then run `autofund app`. The app reconciles, runs the GET-only Production preflight itself and remains STOPPED. No separate CLI or preflight command is required before using the web application. In the browser review the 50/25/11 MXN envelope, the **Production preflight READY / BLOCKED** indicator and its exact blocker, then press **START AUTOFUND** and type `START AUTOFUND REAL 50`. START re-runs a fresh GET-only preflight before enabling automatic execution; if it fails, the app stays STOPPED with automatic execution off and shows the precise blocker, and the operator can simply retry.

## Stop and kill

**STOP SESSION** prevents new intents, finishes known reconciliation, flushes artifacts and returns to STOPPED. **EMERGENCY KILL** immediately blocks new writes, disables strategy execution and enters HALTED; it does not sell an open position. Keep the application alive for inspection and recovery.

## Restart and reconciliation

After process exit, launch `autofund app` again. Automatic execution is OFF. Startup loads durable state and reconciles any known origin before STOPPED. An unresolved/ambiguous order keeps the app HALTED. Never retry its POST; query order and trade evidence by origin ID.

Journal corruption or contradictory fills require manual artifact preservation and investigation. Do not delete the journal or attribute unrelated Bitso balances to AutoFund. A Bitso or market-data outage blocks new orders; retain read/reconciliation attempts and restart only after health recovers. A loss-limit halt cannot be bypassed in the same session.

## Diagnostics

Open Sessions and choose **Export session diagnostics**. Preserve the ZIP plus the session directory. It contains sanitized report, checkpoint summary, handoff and telemetry. It excludes keys, Authorization and unrelated balances.

For credential-free UX validation use `autofund app --demo --no-open-browser`. Demo data must never be treated as Production evidence.

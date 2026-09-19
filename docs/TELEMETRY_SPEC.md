# MVP telemetry

Each session writes beneath `artifacts/mvp/<session_id>/`: `telemetry.jsonl`, `application.log.jsonl`, `report.json`, `checkpoint_summary.json` and `handoff.json`. Global JSONL logs rotate at 2 MB with five retained files. Secrets, Authorization, control tokens, full Bitso balances and unrelated account data are excluded.

Every checkpoint has UTC time, monotonic offset, `CP-000001` sequence, session/run IDs, market, strategy version, component, level, message and correlation ID. Trade chains reuse one correlation ID from signal through intent, order, fill and ledger. Durations use monotonic clocks.

The taxonomy covers application recovery; session controls; market connection and quality; candles and strategy; capital/risk/final market decisions; order lifecycle; fills, reconciliation and ledger; positions and loss limits; and research challenger events. Session summaries contain deterministic counts, P&L, fees, warnings, halts and the largest observed latency. `handoff.json` derives only from recorded facts.

`GET /api/v1/sessions/{id}/diagnostics` exports a sanitized ZIP containing session telemetry, report, summary, handoff and a public config fingerprint. The Telemetry page shows the checkpoint timeline, component, severity, correlations and aggregate health without raw tick floods.

## Live operator observability

The MVP reuses the F4.6 runtime contract rather than defining a second event model. `MvpObservability` projects the same authoritative telemetry checkpoints into the F4.6 `RuntimeView` / `RuntimeEvent` shape and additionally derives, per checkpoint: pipeline stage result for MARKET, CANDLE, STRATEGY, SIGNAL, CAPITAL, RISK, FINAL_MARKET_CHECK, ORDER, FILL, RECONCILIATION and LEDGER; last strategy decision with its reason; and operational counters (orders, fills, signals, rejections, halts, reconciliations, ledger updates, market events). Latency is recorded for market request RTT, strategy evaluation, final market preflight and reconciliation.

Projection is best-effort and read-only. It never feeds strategy, risk, execution or accounting, and any failure inside it is contained so the durable session is unaffected. No demo or invented data is published in REAL MONEY mode; unavailable values render as UNKNOWN or WAITING FOR DATA.

Observability health is backend-authoritative. OBSERVABILITY DEGRADED is reported when a RUNNING session loses BOTH internal runtime heartbeat visibility and operator publication past the timeout. Browser tab closure is not an input, and the guard has no trading effect.

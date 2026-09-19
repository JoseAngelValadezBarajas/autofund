# MVP telemetry

Each session writes beneath `artifacts/mvp/<session_id>/`: `telemetry.jsonl`, `application.log.jsonl`, `report.json`, `checkpoint_summary.json` and `handoff.json`. Global JSONL logs rotate at 2 MB with five retained files. Secrets, Authorization, control tokens, full Bitso balances and unrelated account data are excluded.

Every checkpoint has UTC time, monotonic offset, `CP-000001` sequence, session/run IDs, market, strategy version, component, level, message and correlation ID. Trade chains reuse one correlation ID from signal through intent, order, fill and ledger. Durations use monotonic clocks.

The taxonomy covers application recovery; session controls; market connection and quality; candles and strategy; capital/risk/final market decisions; order lifecycle; fills, reconciliation and ledger; positions and loss limits; and research challenger events. Session summaries contain deterministic counts, P&L, fees, warnings, halts and the largest observed latency. `handoff.json` derives only from recorded facts.

`GET /api/v1/sessions/{id}/diagnostics` exports a sanitized ZIP containing session telemetry, report, summary, handoff and a public config fingerprint. The Telemetry page shows the checkpoint timeline, component, severity, correlations and aggregate health without raw tick floods.

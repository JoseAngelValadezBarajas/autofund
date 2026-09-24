"""Deterministic, secret-free session telemetry and handoff artifacts."""

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

CHECKPOINTS = frozenset({
    "APP_BOOT", "RECOVERY_STARTED", "RECOVERY_COMPLETED",
    "SESSION_START_REQUESTED", "SESSION_STARTED", "SESSION_START_BLOCKED",
    "SESSION_STOP_REQUESTED", "SESSION_STOPPED", "PRODUCTION_PREFLIGHT_PASS", "PRODUCTION_PREFLIGHT_FAIL",
    "KILL_SWITCH_TRIGGERED", "AUTO_HALT_TRIGGERED", "MARKET_CONNECTED", "MARKET_DISCONNECTED",
    "MARKET_QUALITY_CHANGED", "CANDLE_CLOSED", "STRATEGY_EVALUATED", "SIGNAL_GENERATED", "NO_SIGNAL",
    "CAPITAL_CHECK_PASS", "CAPITAL_CHECK_REJECT", "RISK_CHECK_PASS", "RISK_CHECK_REJECT",
    "FINAL_MARKET_CHECK_PASS", "FINAL_MARKET_CHECK_REJECT", "ORDER_INTENT_CREATED", "ORDER_SUBMITTING",
    "ORDER_SUBMITTED", "ORDER_ACKNOWLEDGED", "ORDER_OUTCOME_UNKNOWN", "PARTIAL_FILL", "FILL", "RECONCILIATION_STARTED",
    "RECONCILIATION_PASS", "RECONCILIATION_FAIL", "LEDGER_UPDATED", "POSITION_OPENED", "POSITION_CLOSED",
    "POSITION_REDUCED", "REALIZED_PNL_UPDATED", "ORDER_RECOVERY_STARTED", "ORDER_RECOVERED",
    "FILL_RECOVERED", "POSITION_RECOVERED",
    "LOSS_LIMIT_WARNING", "LOSS_LIMIT_HIT", "LEARNING_OBSERVATION", "CHALLENGER_CREATED",
    "CHALLENGER_EVALUATED", "CHALLENGER_PROMOTED", "CHALLENGER_REJECTED",
    "MARKET_SCAN_STARTED", "MARKET_SCAN_COMPLETED", "MARKET_CANDIDATE_ELIGIBLE",
    "MARKET_CANDIDATE_REJECTED", "MARKET_SHADOW_STARTED", "MARKET_SHADOW_EVALUATED",
    "MARKET_SCANNER_DEGRADED", "RUNTIME_GAP", "SIGNAL_SUPPRESSED_PENDING_ORDER",
    "RECONCILIATION_PROBE",
})
SECRET_KEYS = {"api_key", "api_secret", "authorization", "control_token", "token"}


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items() if str(k).lower() not in SECRET_KEYS}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class SessionTelemetry:
    def __init__(self, root: Path, session_id: str, *, strategy_version: str = "champion-0.1") -> None:
        self.session_id = session_id
        self.run_id = uuid4().hex
        self.strategy_version = strategy_version
        self.path = root / session_id
        self.path.mkdir(parents=True, exist_ok=True)
        self.telemetry_path = self.path / "telemetry.jsonl"
        self.log_path = self.path / "application.log.jsonl"
        self.started_at = datetime.now(UTC)
        self.started_monotonic = monotonic()
        self.sequence = 0
        self.rows: list[dict[str, Any]] = []
        self.durations: dict[str, list[float]] = {}
        self.warnings: list[str] = []

    def checkpoint(self, event: str, *, component: str, level: str = "INFO",
                   correlation_id: str | None = None, message: str = "", duration_ms: float | None = None,
                   **data: Any) -> dict[str, Any]:
        if event not in CHECKPOINTS:
            raise ValueError("unknown telemetry checkpoint")
        self.sequence += 1
        row = {
            "timestamp_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "monotonic_offset_ms": round((monotonic() - self.started_monotonic) * 1000, 3),
            "checkpoint_id": f"CP-{self.sequence:06d}", "checkpoint_sequence": self.sequence,
            "level": level, "event": event, "session_id": self.session_id, "run_id": self.run_id,
            "market": "btc_mxn", "strategy_version": self.strategy_version, "component": component,
            "message": message, "correlation_id": correlation_id, **_clean(data),
        }
        if duration_ms is not None:
            row["duration_ms"] = round(duration_ms, 3)
            self.durations.setdefault(event, []).append(duration_ms)
        with self.telemetry_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_clean(row), sort_keys=True, separators=(",", ":")) + "\n")
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_clean({k: row[k] for k in (
                "timestamp_utc", "level", "event", "session_id", "run_id", "market",
                "strategy_version", "component", "message", "correlation_id")}),
                sort_keys=True, separators=(",", ":")) + "\n")
        self.rows.append(row)
        if level in {"WARNING", "ERROR", "CRITICAL"}:
            self.warnings.append(event)
        return row

    def finalize(self, *, result: str, stop_reason: str | None = None, facts: dict[str, Any],
                 identity: dict[str, Any] | None = None, config: dict[str, Any] | None = None,
                 time_facts: dict[str, Any] | None = None, learning: dict[str, Any] | None = None) -> None:
        """Write deterministic report, checkpoint summary and handoff."""
        from .postmortem import (
            build_checkpoint_summary,
            build_handoff,
            build_report,
            scanner_evidence,
        )

        rows = list(self.rows)
        largest = max((value for values in self.durations.values() for value in values), default=0.0)
        ended = datetime.now(UTC)
        time_block = {"started_at": self.started_at.isoformat().replace("+00:00", "Z"),
                      "ended_at": ended.isoformat().replace("+00:00", "Z"),
                      "stop_reason": stop_reason, **(time_facts or {})}
        identity_block = {"session_id": self.session_id, "run_id": self.run_id,
                          "product_version": "AutoFund MVP 0.1.2",
                          "strategy_version": self.strategy_version, **(identity or {})}
        scanner = scanner_evidence(rows)
        facts_with_latency = {**facts, "largest_latency_ms": round(largest, 3)}
        learning_block = learning or {"classification": "NOT_EVALUATED", "observations": []}
        report = build_report(rows, identity=identity_block, time_facts=time_block,
                              config=config or {}, facts=facts_with_latency, scanner=scanner)
        summary = build_checkpoint_summary(rows, session_id=self.session_id, run_id=self.run_id, result=result,
                                           warnings=self.warnings, facts=facts_with_latency,
                                           stop_reason=stop_reason, time_facts=time_block, learning=learning_block)
        handoff = build_handoff(rows, report=report, result=result, stop_reason=stop_reason,
                                scanner=scanner, learning=learning_block)
        for name, value in (("checkpoint_summary.json", summary), ("handoff.json", handoff),
                            ("report.json", report)):
            (self.path / name).write_text(
                json.dumps(_clean(value), sort_keys=True, separators=(",", ":"), default=str) + "\n",
                encoding="utf-8")


def configure_rotating_log(root: Path, *, debug: bool = False) -> logging.Logger:
    root.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("autofund.mvp")
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(root / "autofund.jsonl", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        class JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:
                return json.dumps({"timestamp_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "level": record.levelname, "component": record.name, "message": record.getMessage()},
                    sort_keys=True, separators=(",", ":"))
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
    return logger

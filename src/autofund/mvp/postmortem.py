"""Deterministic post-session evidence.

Aggregates authoritative telemetry rows into the session report, checkpoint
summary and engineering handoff. Everything here is derived from recorded facts:
no wall clock, no randomness, no natural-language speculation. The same rows
always produce byte-identical aggregates.
"""

from collections import Counter
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial

REPORT_SCHEMA = "autofund.session-report.v2"
SUMMARY_SCHEMA = "autofund.checkpoint-summary.v2"
HANDOFF_SCHEMA = "autofund.handoff.v2"
SCANNER_SCHEMA = "autofund.scanner-evidence.v1"

STRATEGY_EVALUATED = "STRATEGY_EVALUATED"
NO_SIGNAL = "NO_SIGNAL"
SIGNAL_GENERATED = "SIGNAL_GENERATED"
CANDLE_CLOSED = "CANDLE_CLOSED"

MARKET_EVENTS = frozenset({"MARKET_CONNECTED", "MARKET_DISCONNECTED", "CANDLE_CLOSED",
                           "MARKET_QUALITY_CHANGED"})
EXECUTION_EVENTS = ("ORDER_INTENT_CREATED", "ORDER_SUBMITTING", "ORDER_ACKNOWLEDGED",
                    "ORDER_OUTCOME_UNKNOWN", "PARTIAL_FILL", "FILL", "RECONCILIATION_STARTED",
                    "RECONCILIATION_PASS", "RECONCILIATION_FAIL", "LEDGER_UPDATED",
                    "POSITION_OPENED", "POSITION_CLOSED")
REJECTION_EVENTS = ("CAPITAL_CHECK_REJECT", "RISK_CHECK_REJECT", "FINAL_MARKET_CHECK_REJECT")
HALT_EVENTS = ("AUTO_HALT_TRIGGERED", "LOSS_LIMIT_HIT", "KILL_SWITCH_TRIGGERED")
LEARNING_EVENTS = ("LEARNING_OBSERVATION", "CHALLENGER_CREATED", "CHALLENGER_EVALUATED",
                   "CHALLENGER_PROMOTED", "CHALLENGER_REJECTED")
SCANNER_EVENTS = ("MARKET_SCAN_STARTED", "MARKET_SCAN_COMPLETED", "MARKET_CANDIDATE_ELIGIBLE",
                  "MARKET_CANDIDATE_REJECTED", "MARKET_SHADOW_STARTED", "MARKET_SHADOW_EVALUATED",
                  "MARKET_SCANNER_DEGRADED")

# Deterministic improvement signals. These are facts about the session, not advice.
ZERO_SIGNALS = "ZERO_SIGNALS_ACROSS_ELIGIBLE_EVALUATIONS"
NEAR_SIGNAL = "NEAR_SIGNAL_EVALUATIONS_OBSERVED"
EXECUTION_NOT_EXERCISED = "EXECUTION_PATH_NOT_EXERCISED"
MARKET_DEGRADATIONS = "MARKET_DATA_DEGRADATIONS"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
CHALLENGER_DEFERRED = "CHALLENGER_GENERATION_DEFERRED"
SCANNER_UNAVAILABLE = "MARKET_SCANNER_EVIDENCE_UNAVAILABLE"


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


@financial
def _distance_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Distance-to-signal statistics over eligible evaluations only."""
    values = [_decimal(row.get("distance_to_signal")) for row in rows
              if row.get("event") == STRATEGY_EVALUATED and row.get("eligible") is True]
    usable = [value for value in values if value is not None]
    if not usable:
        return {"count": 0, "minimum": None, "maximum": None, "mean": None,
                "near_signal_count": 0, "closest_to_entry": None,
                "semantics": "NEGATIVE_BOUNDARY_EXCEEDED_ZERO_AT_BOUNDARY_POSITIVE_REMAINING",
                "units": "FRACTION_OF_DECISION_BOUNDARY_PRICE"}
    near = sum(1 for row in rows if row.get("event") == STRATEGY_EVALUATED and row.get("near_signal") is True)
    return {"count": len(usable), "minimum": str(min(usable)), "maximum": str(max(usable)),
            "mean": str(sum(usable, ZERO) / Decimal(len(usable))), "near_signal_count": near,
            "closest_to_entry": str(min(usable)),
            "semantics": "NEGATIVE_BOUNDARY_EXCEEDED_ZERO_AT_BOUNDARY_POSITIVE_REMAINING",
            "units": "FRACTION_OF_DECISION_BOUNDARY_PRICE"}


def _reason_distribution(rows: list[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(str(row.get("reason_code")) for row in rows
                      if row.get("event") in {NO_SIGNAL, SIGNAL_GENERATED} and row.get("reason_code"))
    return dict(sorted(counter.items()))


def _regime_distribution(rows: list[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(str(row.get("market_regime")) for row in rows
                      if row.get("event") == STRATEGY_EVALUATED and row.get("market_regime"))
    return dict(sorted(counter.items()))


def _quality_transitions(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [{"at": str(row["timestamp_utc"]), "from": str(row.get("previous", "UNKNOWN")),
             "to": str(row.get("quality", "UNKNOWN"))}
            for row in rows if row.get("event") == "MARKET_QUALITY_CHANGED"]


@financial
def _latency_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[Decimal]] = {}
    for row in rows:
        value = _decimal(row.get("duration_ms"))
        if value is not None:
            groups.setdefault(str(row.get("event")), []).append(value)
    return {event: {"count": len(values), "last_ms": str(values[-1]), "max_ms": str(max(values)),
                    "mean_ms": str(sum(values, ZERO) / Decimal(len(values)))}
            for event, values in sorted(groups.items())}


@financial
def _spread_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [_decimal(row.get("spread_bps")) for row in rows if row.get("spread_bps") is not None]
    usable = [value for value in values if value is not None]
    if not usable:
        return {"count": 0, "minimum": None, "maximum": None, "mean": None}
    return {"count": len(usable), "minimum": str(min(usable)), "maximum": str(max(usable)),
            "mean": str(sum(usable, ZERO) / Decimal(len(usable)))}


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(row["event"]) for row in rows).items()))


def _component_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get("component", "unknown")) for row in rows).items()))


def _levels(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get("level", "INFO")) for row in rows).items()))


def scanner_evidence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Scanner research evidence, isolated from the financial session report."""
    scan_rows = [row for row in rows if row.get("event") in SCANNER_EVENTS]
    eligible = [row for row in scan_rows if row.get("event") == "MARKET_CANDIDATE_ELIGIBLE"]
    rejected = [row for row in scan_rows if row.get("event") == "MARKET_CANDIDATE_REJECTED"]
    shadow = [row for row in scan_rows if row.get("event") == "MARKET_SHADOW_EVALUATED"]
    return {"schema": SCANNER_SCHEMA,
            "scanner_ran": any(row.get("event") == "MARKET_SCAN_COMPLETED" for row in scan_rows),
            "degraded": any(row.get("event") == "MARKET_SCANNER_DEGRADED" for row in scan_rows),
            "universe_size": max((int(row.get("universe", 0)) for row in scan_rows
                                  if row.get("event") == "MARKET_SCAN_COMPLETED"), default=0),
            "eligible_count": len(eligible), "rejected_count": len(rejected),
            "shadow_evaluations": len(shadow),
            "candidates": sorted(({"market": str(row.get("market")), "score": str(row.get("score")),
                                   "score_version": str(row.get("score_version")),
                                   "status": "ELIGIBLE",
                                   "data_fingerprint": str(row.get("data_fingerprint"))} for row in eligible),
                                 key=lambda item: item["market"]),
            "rejections": sorted(({"market": str(row.get("market")), "reason": str(row.get("reason"))}
                                  for row in rejected), key=lambda item: item["market"])}


def candidate_signals(*, report: dict[str, Any], scanner: dict[str, Any]) -> list[str]:
    """Deterministic facts about the session. Never natural-language advice."""
    signals: list[str] = []
    strategy = report["strategy"]
    eligible = int(strategy.get("eligible_evaluations", 0))
    if eligible > 0 and int(strategy.get("signals", 0)) == 0:
        signals.append(ZERO_SIGNALS)
    if int(strategy.get("near_signal_count", 0)) > 0:
        signals.append(NEAR_SIGNAL)
    if not report["execution"]["exercised"]:
        signals.append(EXECUTION_NOT_EXERCISED)
    if int(report["market"].get("degradation_count", 0)) > 0:
        signals.append(MARKET_DEGRADATIONS)
    if not scanner.get("scanner_ran"):
        signals.append(SCANNER_UNAVAILABLE)
    return signals


@financial
def build_report(rows: list[dict[str, Any]], *, identity: dict[str, Any], time_facts: dict[str, Any],
                 config: dict[str, Any], facts: dict[str, Any], scanner: dict[str, Any]) -> dict[str, Any]:
    events = _counts(rows)
    evaluations = [row for row in rows if row.get("event") == STRATEGY_EVALUATED]
    metrics: dict[str, Any] = dict(facts.get("metrics", {}))
    execution_exercised = any(events.get(name, 0) > 0 for name in EXECUTION_EVENTS)
    return {
        "schema": REPORT_SCHEMA,
        "identity": {**identity, "config": config},
        "time": time_facts,
        "market": {"market": identity.get("market", "btc_mxn"),
                   "market_events": events.get("MARKET_CONNECTED", 0),
                   "candles": events.get(CANDLE_CLOSED, 0),
                   "disconnections": events.get("MARKET_DISCONNECTED", 0),
                   "degradation_count": events.get("MARKET_QUALITY_CHANGED", 0),
                   "quality_transitions": _quality_transitions(rows),
                   "latency": _latency_summary(rows),
                   "spread_bps": _spread_summary(rows)},
        "strategy": {"evaluations": len(evaluations),
                     "eligible_evaluations": sum(1 for row in evaluations if row.get("eligible") is True),
                     "no_signal": events.get(NO_SIGNAL, 0), "signals": events.get(SIGNAL_GENERATED, 0),
                     "reason_distribution": _reason_distribution(rows),
                     "distance_to_signal": _distance_summary(rows),
                     "near_signal_count": _distance_summary(rows)["near_signal_count"],
                     "regime_distribution": _regime_distribution(rows)},
        "execution": {name.lower(): events.get(name, 0) for name in EXECUTION_EVENTS}
                     | {"exercised": execution_exercised},
        "risk": {name.lower(): events.get(name, 0) for name in REJECTION_EVENTS},
        "financial": {"initial_equity_mxn": str(facts.get("initial_equity_mxn", "50")),
                      "final_equity_mxn": str(facts.get("final_equity_mxn", metrics.get("net_pnl_mxn", "0"))),
                      "realized_pnl_mxn": str(metrics.get("net_pnl_mxn", "0")),
                      "unrealized_pnl_mxn": str(facts.get("unrealized_pnl_mxn", "0")),
                      "net_pnl_mxn": str(metrics.get("net_pnl_mxn", "0")),
                      "fees_mxn": str(metrics.get("fees_mxn", "0")),
                      "max_drawdown_mxn": str(facts.get("max_drawdown_mxn", "0")),
                      "max_deployment_mxn": str(facts.get("max_deployment_mxn", "0"))},
        "operations": {"warnings": len([row for row in rows if row.get("level") == "WARNING"]),
                       "errors": len([row for row in rows if row.get("level") in {"ERROR", "CRITICAL"}]),
                       "halts": sum(events.get(name, 0) for name in HALT_EVENTS),
                       "kill_switch_activations": events.get("KILL_SWITCH_TRIGGERED", 0)},
        "scanner": scanner,
    }


def build_checkpoint_summary(rows: list[dict[str, Any]], *, session_id: str, run_id: str, result: str,
                             warnings: list[str], facts: dict[str, Any], stop_reason: str | None,
                             time_facts: dict[str, Any], learning: dict[str, Any]) -> dict[str, Any]:
    events = _counts(rows)
    lifecycle = {name: events[name] for name in ("SESSION_START_REQUESTED", "SESSION_STARTED",
                                                 "SESSION_STOP_REQUESTED", "SESSION_STOPPED",
                                                 "KILL_SWITCH_TRIGGERED", "AUTO_HALT_TRIGGERED")
                 if name in events}
    return {"schema": SUMMARY_SCHEMA, "session_id": session_id, "run_id": run_id, "result": result,
            "stop_reason": stop_reason, **time_facts,
            "checkpoint_count": len(rows), "counts_by_event": events,
            "counts_by_component": _component_counts(rows), "counts_by_level": _levels(rows),
            "warnings": sorted(set(warnings)),
            "errors": sorted({str(row["event"]) for row in rows if row.get("level") in {"ERROR", "CRITICAL"}}),
            "session_lifecycle": lifecycle,
            "strategy": {"reason_distribution": _reason_distribution(rows),
                         "eligible_evaluations": sum(1 for row in rows if row.get("event") == STRATEGY_EVALUATED
                                                     and row.get("eligible") is True),
                         "signals": events.get(SIGNAL_GENERATED, 0)},
            "execution_lifecycle": {name: events.get(name, 0) for name in EXECUTION_EVENTS},
            "execution_exercised": any(events.get(name, 0) > 0 for name in EXECUTION_EVENTS),
            "risk_rejections": {name: events.get(name, 0) for name in REJECTION_EVENTS},
            "market_quality_transitions": _quality_transitions(rows),
            "learning_checkpoints": {name: events.get(name, 0) for name in LEARNING_EVENTS},
            "learning": learning,
            "latency": _latency_summary(rows),
            "largest_latency_ms": facts.get("largest_latency_ms")}


def build_handoff(rows: list[dict[str, Any]], *, report: dict[str, Any], result: str,
                  stop_reason: str | None, scanner: dict[str, Any], learning: dict[str, Any]) -> dict[str, Any]:
    signals = candidate_signals(report=report, scanner=scanner)
    strategy, execution, market = report["strategy"], report["execution"], report["market"]
    return {
        "schema": HANDOFF_SCHEMA, "what_happened": result, "stop_reason": stop_reason,
        "what_failed": sorted({str(row["event"]) for row in rows if row.get("level") in {"ERROR", "CRITICAL"}}),
        "strategy_observations": {
            "evaluations": strategy["evaluations"],
            "eligible_evaluations": strategy["eligible_evaluations"],
            "no_signal": strategy["no_signal"], "signals": strategy["signals"],
            "reason_distribution": strategy["reason_distribution"],
            "distance_to_signal": strategy["distance_to_signal"],
            "near_signal_count": strategy["near_signal_count"],
            "regime_distribution": strategy["regime_distribution"],
            "conclusion": ("NO_ACTIONABLE_SIGNAL" if strategy["signals"] == 0 and strategy["eligible_evaluations"] > 0
                           else "SIGNALS_OBSERVED" if strategy["signals"] > 0 else "INSUFFICIENT_HISTORY")},
        "execution_observations": {"exercised": execution["exercised"],
                                   "order_intents": execution["order_intent_created"],
                                   "orders_acknowledged": execution["order_acknowledged"],
                                   "fills": execution["fill"], "partial_fills": execution["partial_fill"],
                                   "reconciliations": execution["reconciliation_pass"],
                                   "ambiguous_outcomes": execution["order_outcome_unknown"]},
        "risk_observations": {**report["risk"], "halts": report["operations"]["halts"],
                              "kill_switch_activations": report["operations"]["kill_switch_activations"]},
        "market_observations": {key: market[key] for key in
                                ("market", "market_events", "candles", "disconnections",
                                 "degradation_count", "spread_bps")},
        "telemetry_observations": {"checkpoints": len(rows), "warnings": report["operations"]["warnings"],
                                   "errors": report["operations"]["errors"],
                                   "latency": market["latency"]},
        "learning_observations": learning,
        "scanner_observations": scanner,
        # Deterministic facts only; no natural-language recommendations.
        "candidate_improvement_signals": signals,
    }

"""Deterministic post-session evidence.

Aggregates authoritative telemetry rows into the session report, checkpoint
summary and engineering handoff. Everything here is derived from recorded facts:
no wall clock, no randomness, no natural-language speculation. The same rows
always produce byte-identical aggregates.
"""

from collections import Counter
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
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
                    "ORDER_SUBMITTED", "ORDER_OUTCOME_UNKNOWN", "PARTIAL_FILL", "FILL", "RECONCILIATION_STARTED",
                    "RECONCILIATION_PASS", "RECONCILIATION_FAIL", "LEDGER_UPDATED",
                    "POSITION_OPENED", "POSITION_REDUCED", "POSITION_CLOSED",
                    "REALIZED_PNL_UPDATED", "ORDER_RECOVERY_STARTED", "ORDER_RECOVERED",
                    "FILL_RECOVERED", "POSITION_RECOVERED")
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
# Gross price movement that did not survive observed execution costs.
GROSS_PROFIT_CONSUMED_BY_FEES = "GROSS_PROFIT_CONSUMED_BY_FEES"
SIGNALS_SUPPRESSED = "SIGNALS_SUPPRESSED_PENDING_UNRESOLVED_ORDER"
RUNTIME_GAP_DETECTED = "RUNTIME_GAP_DETECTED"
RECONCILIATION_BLOCKED = "RECONCILIATION_BLOCKED"

# A telemetry gap larger than this cannot be explained by normal loop cadence
# (5s poll + 1m candles + 300s scans) and indicates suspended or starved execution.
RUNTIME_GAP_THRESHOLD_SECONDS = Decimal("600")


@financial
def runtime_gaps(rows: list[dict[str, Any]],
                 threshold: Decimal = RUNTIME_GAP_THRESHOLD_SECONDS) -> dict[str, Any]:
    """Large telemetry gaps, which mean no process was monitoring the market.

    Used to distinguish real monitoring from wall-clock elapsed time. Evidence
    only; the cause (suspend, blocking, starvation) is deliberately not inferred
    unless the observation itself proves it.
    """
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    for row in rows:
        value = row.get("timestamp_utc")
        if isinstance(value, str) and value:
            try:
                parsed.append((datetime.fromisoformat(value), row))
            except ValueError:
                continue
    gaps: list[dict[str, Any]] = []
    for (before_at, before), (after_at, after) in pairwise(parsed):
        seconds = Decimal(str((after_at - before_at).total_seconds()))
        if seconds > threshold:
            gaps.append({"last_event_at": before.get("timestamp_utc"), "last_event": str(before.get("event")),
                         "next_event_at": after.get("timestamp_utc"), "next_event": str(after.get("event")),
                         "gap_seconds": str(seconds)})
    total = sum((Decimal(gap["gap_seconds"]) for gap in gaps), ZERO)
    largest = max((Decimal(gap["gap_seconds"]) for gap in gaps), default=ZERO)
    return {"count": len(gaps), "total_gap_seconds": str(total), "largest_gap_seconds": str(largest),
            "threshold_seconds": str(threshold), "gaps": sorted(gaps, key=lambda g: g["last_event_at"])}


@financial
def round_trip_economics(financial_truth: dict[str, Any] | None) -> dict[str, Any]:
    """Factual completed-round-trip economics from the execution projection.

    Reports whether positive gross movement survived observed execution costs.
    It never proposes a threshold change.
    """
    truth = financial_truth or {}
    gross = _decimal(truth.get("gross_realized_pnl_mxn")) or ZERO
    net = _decimal(truth.get("net_realized_pnl_mxn")) or ZERO
    fees = _decimal(truth.get("fees_mxn")) or ZERO
    consumed = bool(gross > ZERO and net <= ZERO)
    return {"round_trip_completed": bool(truth.get("sell") or truth.get("fills")),
            "gross_realized_pnl_mxn": str(gross), "net_realized_pnl_mxn": str(net),
            "total_fees_mxn": str(fees), "gross_profit_consumed_by_fees": consumed,
            "facts": [GROSS_PROFIT_CONSUMED_BY_FEES] if consumed else []}


def signal_admission(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Report actionable decisions against admission so none are silently lost.

    Suppression is reported two ways: explicitly (from the dedicated checkpoint
    added in 0.1.2) and as the derived count of actionable decisions that were
    never admitted. The derived figure is an upper bound and is labelled as such,
    because a decision can also be declined for a non-blocking reason.
    """
    evaluations = [row for row in rows if row.get("event") == STRATEGY_EVALUATED]
    buys = sum(1 for row in evaluations if row.get("decision") == "BUY")
    sells = sum(1 for row in evaluations if row.get("decision") == "SELL")
    admitted = sum(1 for row in rows if row.get("event") == SIGNAL_GENERATED)
    explicit = sum(1 for row in rows if row.get("event") == "SIGNAL_SUPPRESSED_PENDING_ORDER")
    return {"strategy_buy_decisions": buys, "strategy_sell_decisions": sells,
            "actionable_decisions": buys + sells, "signals_admitted": admitted,
            "signals_suppressed_pending_order": explicit,
            "actionable_decisions_not_admitted": max(0, buys + sells - admitted),
            "suppression_basis": ("EXPLICIT_CHECKPOINT" if explicit else
                                  "DERIVED_FROM_DECISION_AND_ADMISSION_COUNTS")}


def _latest(rows: list[dict[str, Any]], key: str) -> Any:
    for row in reversed(rows):
        if row.get(key) not in (None, ""):
            return row[key]
    return None


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
    actionable = int(strategy.get("buy_decisions", 0)) + int(strategy.get("sell_decisions", 0))
    admitted = int(strategy.get("signals", 0))
    # Actionable decisions that were never admitted are reported explicitly rather
    # than being silently dropped from the record.
    if eligible > 0 and actionable > admitted:
        signals.append(SIGNALS_SUPPRESSED)
    elif eligible > 0 and actionable == 0:
        signals.append(ZERO_SIGNALS)
    if int(strategy.get("near_signal_count", 0)) > 0:
        signals.append(NEAR_SIGNAL)
    if not report["execution"]["exercised"]:
        signals.append(EXECUTION_NOT_EXERCISED)
    if int(report["market"].get("degradation_count", 0)) > 0:
        signals.append(MARKET_DEGRADATIONS)
    if int(report.get("runtime", {}).get("gaps", {}).get("count", 0)) > 0:
        signals.append(RUNTIME_GAP_DETECTED)
    if report.get("runtime", {}).get("reconciliation_state") == "BLOCKED":
        signals.append(RECONCILIATION_BLOCKED)
    signals.extend(report.get("round_trip", {}).get("facts", []))
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
    execution_counts = {name.lower(): events.get(name, 0) for name in EXECUTION_EVENTS}
    execution_facts = dict(facts.get("execution") or {})
    net_pnl = _decimal(metrics.get("net_pnl_mxn")) or ZERO
    fees_mxn = _decimal(metrics.get("fees_mxn")) or ZERO
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
                     "buy_decisions": sum(1 for row in evaluations if row.get("decision") == "BUY"),
                     "sell_decisions": sum(1 for row in evaluations if row.get("decision") == "SELL"),
                     "signals_suppressed_pending_order": events.get("SIGNAL_SUPPRESSED_PENDING_ORDER", 0),
                     "reason_distribution": _reason_distribution(rows),
                     "distance_to_signal": _distance_summary(rows),
                     "near_signal_count": _distance_summary(rows)["near_signal_count"],
                     "regime_distribution": _regime_distribution(rows)},
        "execution": execution_counts | {"exercised": execution_exercised,
                     "intents": execution_counts.get("order_intent_created", 0),
                     "submissions": execution_counts.get("order_submitting", 0),
                     "acks": execution_counts.get("order_acknowledged", 0),
                     "unknown_outcomes": execution_counts.get("order_outcome_unknown", 0),
                     "orders": execution_facts.get("orders", execution_counts.get("order_acknowledged", 0)),
                     "fills": execution_facts.get("fills", execution_counts.get("fill_recovered", 0)),
                     "recovered_fills": execution_counts.get("fill_recovered", 0),
                     "financial_truth": execution_facts},
        "risk": {name.lower(): events.get(name, 0) for name in REJECTION_EVENTS},
        "financial": {"initial_equity_mxn": str(facts.get("initial_equity_mxn", "50")),
                      "final_equity_mxn": str(facts.get("final_equity_mxn", metrics.get("net_pnl_mxn", "0"))),
                      "gross_pnl_mxn": str(net_pnl + fees_mxn),
                      "realized_pnl_mxn": str(facts.get("realized_pnl_mxn", "0")),
                      "unrealized_pnl_mxn": str(facts.get("unrealized_pnl_mxn", "0")),
                      "net_pnl_mxn": str(net_pnl),
                      "fees_mxn": str(fees_mxn),
                      "max_drawdown_mxn": str(facts.get("max_drawdown_mxn", "0")),
                      "max_deployment_mxn": str(facts.get("max_deployment_mxn", "0"))},
        "operations": {"warnings": len([row for row in rows if row.get("level") == "WARNING"]),
                       "errors": len([row for row in rows if row.get("level") in {"ERROR", "CRITICAL"}]),
                       "halts": sum(events.get(name, 0) for name in HALT_EVENTS),
                       "kill_switch_activations": events.get("KILL_SWITCH_TRIGGERED", 0)},
        # Wall-clock runtime can include periods with no executing process, so the
        # gaps are reported explicitly rather than being counted as monitoring.
        "runtime": {"gaps": runtime_gaps(rows)}, "round_trip": round_trip_economics(facts.get("execution_financial_truth")),
        "signal_admission": signal_admission(rows),
        "scanner": scanner,
        "portfolio": facts.get("portfolio"),
        "wallet": facts.get("wallet"),
        "execution_financial_truth": facts.get("execution_financial_truth"),
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
            "signal_admission": signal_admission(rows),
            "runtime_gaps": runtime_gaps(rows),
            "round_trip": round_trip_economics(facts.get("execution_financial_truth")),
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
            "conclusion": ("SIGNALS_SUPPRESSED" if strategy["signals"] < strategy.get("buy_decisions", 0)
                           + strategy.get("sell_decisions", 0)
                           else "NO_ACTIONABLE_SIGNAL" if strategy["signals"] == 0
                           and strategy["eligible_evaluations"] > 0
                           else "SIGNALS_OBSERVED" if strategy["signals"] > 0 else "INSUFFICIENT_HISTORY")},
        "execution_observations": {"exercised": execution["exercised"],
                                   "order_intents": execution["order_intent_created"],
                                   "orders_acknowledged": execution["order_acknowledged"],
                                   "fills": execution["fills"], "partial_fills": execution["partial_fill"],
                                   "recovered_fills": execution["recovered_fills"],
                                   "reconciliations": execution["reconciliation_pass"],
                                   "ambiguous_outcomes": execution["order_outcome_unknown"]},
        "risk_observations": {**report["risk"], "halts": report["operations"]["halts"],
                              "kill_switch_activations": report["operations"]["kill_switch_activations"]},
        "market_observations": {key: market[key] for key in
                                ("market", "market_events", "candles", "disconnections",
                                 "degradation_count", "spread_bps")},
        "telemetry_observations": {"checkpoints": len(rows), "warnings": report["operations"]["warnings"],
                                   "errors": report["operations"]["errors"],
                                   "latency": market["latency"],
                                   "runtime_gaps": report.get("runtime", {}).get("gaps"),
                                   "reconciliation_state": report.get("execution_financial_truth", {}) or {}},
        "signal_admission": report.get("signal_admission", {}),
        "round_trip": report.get("round_trip", {}),
        "learning_observations": learning,
        "scanner_observations": scanner,
        "financial_truth": {"portfolio": report.get("portfolio"), "wallet": report.get("wallet"),
                            "execution": execution.get("financial_truth")},
        # Deterministic facts only; no natural-language recommendations.
        "candidate_improvement_signals": signals,
    }

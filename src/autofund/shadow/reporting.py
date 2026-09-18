from collections import Counter
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from autofund.decimal_utils import financial
from autofund.observer import parsing
from autofund.observer.errors import MarketDataInvalid
from autofund.replay.serialization import canonical_value, fingerprint

from .capture import read_capture, replay


def session_report(path: Path) -> dict[str, object]:
    result = parsing.obj(canonical_value(replay(path)))
    header, frames, _ = read_capture(path)
    reasons = Counter(
        parsing.text(parsing.obj(r).get("reason"))
        for r in parsing.array(result["rejections"])
    )
    return {
        "title": "AutoFund Shadow Report",
        "capital_type": "VIRTUAL_ONLY",
        "period_start": header["start"],
        "period_end": frames[-1].observed_at if frames else header["start"],
        "book": result["book"],
        "metrics": result["metrics"],
        "closed_candles": len(parsing.array(result["candles"])),
        "signals": sum(
            parsing.obj(s).get("intent") is not None
            for s in parsing.array(result["signals"])
        ),
        "shadow_fills": len(parsing.array(result["executions"])),
        "rejections_by_reason": dict(reasons),
        "quality": result["quality"],
        "benchmark_candidate": result["benchmark_candidate"],
        "fee_source": parsing.obj(result["fee"])["source"],
        "strategy_fingerprint": result["strategy_fingerprint"],
        "config_fingerprint": result["config_fingerprint"],
        "data_fingerprint": result["data_fingerprint"],
        "result_fingerprint": result["result_fingerprint"],
        "open_position": any(
            parsing.number(parsing.obj(p)["quantity"]) > 0
            for p in parsing.obj(result["positions"]).values()
        ),
        "parity": "PASS",
        "interpretation": "Observed virtual experiment; not a profitability guarantee.",
    }


@financial
def aggregate_reports(directory: Path, period: str = "weekly") -> dict[str, object]:
    if period not in ("daily", "weekly"):
        raise MarketDataInvalid("period must be daily or weekly")
    paths = sorted({p.parent for p in directory.rglob("manifest.json")})
    if not paths:
        raise MarketDataInvalid("no complete shadow sessions")
    reports = [parsing.obj(canonical_value(session_report(p))) for p in paths]
    reports.sort(key=lambda r: parsing.timestamp(r["period_start"]))
    compatibility = [
        (r["book"], r["strategy_fingerprint"], r["config_fingerprint"], r["fee_source"])
        for r in reports
    ]
    if len(set(compatibility)) != 1:
        raise MarketDataInvalid("incompatible sessions cannot be aggregated")
    identities = [r["result_fingerprint"] for r in reports]
    if len(set(identities)) != len(identities):
        raise MarketDataInvalid("duplicate sessions would double-count performance")
    for previous, following in pairwise(reports):
        if parsing.timestamp(previous["period_end"]) >= parsing.timestamp(
            following["period_start"]
        ):
            raise MarketDataInvalid("overlapping sessions cannot be aggregated")
    groups: dict[str, list[dict[str, object]]] = {}
    for report in reports:
        first = parsing.timestamp(report["period_start"])
        last = parsing.timestamp(report["period_end"])
        first_key = (
            first.date()
            if period == "daily"
            else (first - timedelta(days=first.weekday())).date()
        )
        last_key = (
            last.date()
            if period == "daily"
            else (last - timedelta(days=last.weekday())).date()
        )
        if first_key != last_key:
            raise MarketDataInvalid(
                "session crosses reporting boundary; report individually"
            )
        groups.setdefault(str(first_key), []).append(report)
    summary = []
    for label, members in groups.items():
        metrics = [parsing.obj(r["metrics"]) for r in members]
        allocated = sum(
            (parsing.number(m["initial_equity"]) for m in metrics), Decimal("0")
        )
        net = sum((parsing.number(m["net_pnl"]) for m in metrics), Decimal("0"))
        summary.append(
            {
                "period": label,
                "sessions": len(members),
                "accounting": "independent virtual allocations; not compounded or continuous equity",
                "allocated_total": allocated,
                "net_pnl_sum": net,
                "return_on_allocated_sum": net / allocated,
                "fees_sum": sum(
                    (parsing.number(m["fees"]) for m in metrics), Decimal("0")
                ),
                "worst_session_drawdown": max(
                    parsing.number(m["max_drawdown"]) for m in metrics
                ),
                "benchmark_candidate": all(
                    r["benchmark_candidate"] is True for r in members
                ),
            }
        )
    return {
        "title": "AutoFund Shadow Report",
        "period": period,
        "groups": summary,
        "sessions": reports,
        "aggregate_fingerprint": fingerprint(
            {"period": period, "sessions": identities}
        ),
    }

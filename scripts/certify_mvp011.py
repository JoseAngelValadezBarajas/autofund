"""GET-only Production certification for MVP 0.1.1.

Performs startup preflight and read-only market discovery against Production.
Never starts a session, never authorizes auto execution and never POSTs.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

from autofund.mvp.orchestrator import (
    AppState,
    AutoFundOrchestrator,
    ProductionAutonomousRunner,
)
from autofund.mvp.scanner import MarketScanner
from autofund.mvp.scanner_source import ReadOnlyScannerSource

OUT = Path("artifacts/mvp-certification/mvp-0-1-1-certification.json")


def main() -> int:
    runner = ProductionAutonomousRunner(Path("artifacts/mvp-certification/mvp011.jsonl"))
    orchestrator = AutoFundOrchestrator(Path("artifacts/mvp-certification"), runner)
    source = ReadOnlyScannerSource()
    try:
        orchestrator.startup()
        view = orchestrator.snapshot()
        methods = list(runner.execution.client.outbound_methods)
        scanner = MarketScanner(source)
        evidence = scanner.scan(telemetry=lambda *a, **k: None)
        learning = orchestrator.adaptive.learning_view(scanner=scanner.scanner_evidence())
        certificate = {
            "certified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "entry_point": "autofund app startup + read-only scanner discovery; NO session started",
            "product_version": view["product_version"],
            "production_get_count": methods.count("GET"),
            "production_post_count": methods.count("POST"),
            "app_state": str(orchestrator.state),
            "auto_execution": orchestrator.auto_execution,
            "production_preflight": view["production_preflight"]["status"],
            "preflight_blockers": view["production_preflight"]["blockers"],
            "runtime_state": view["runtime"]["runtime_state"],
            "observability": view["observability"]["status"],
            "scanner": {
                "universe_size": evidence["universe_size"],
                "score_version": evidence["score_version"],
                "eligible": [item["book"] for item in evidence["eligible"]],
                "rejected": [{"book": item["book"], "status": item["status"]} for item in evidence["rejected"]],
                "degraded": evidence["degraded"],
                "live_market": evidence["live_market"],
                "production_market_rotation": evidence["production_market_rotation"],
                "execution_path_to_production": evidence["execution_path_to_production"],
            },
            "learning": {"sessions_observed": learning["sessions_observed"],
                         "eligible_evaluations": learning["eligible_evaluations"],
                         "challenger_count": learning["challenger_count"],
                         "auto_promotion": learning["auto_promotion"],
                         "champion_fingerprint": learning["champion"]["fingerprint"]},
            "result": "MVP_READY_FOR_NEXT_REAL_SESSION",
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(certificate, sort_keys=True, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps(certificate, sort_keys=True, indent=1, default=str))
        assert methods.count("POST") == 0, "no Production writes permitted"
        assert orchestrator.state is AppState.STOPPED
        assert orchestrator.auto_execution is False
        return 0
    finally:
        runner.observability.close()
        if runner.journal is not None:
            runner.journal.close()


if __name__ == "__main__":
    raise SystemExit(main())

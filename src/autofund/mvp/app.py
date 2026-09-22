"""One-command AutoFund MVP application."""

import argparse
import threading
import webbrowser
from pathlib import Path

import uvicorn

from autofund.live.journal import DEFAULT_JOURNAL
from autofund.observer.client import BitsoProductionReadOnlyClient

from .api import create_mvp_app
from .orchestrator import (
    AutoFundOrchestrator,
    DemoAutonomousRunner,
    ProductionAutonomousRunner,
)
from .scanner import MarketScanner
from .scanner_demo import DemoScannerSource
from .scanner_source import ReadOnlyScannerSource
from .telemetry import configure_rotating_log


def production_scanner_source(runner: ProductionAutonomousRunner,
                              market_client: BitsoProductionReadOnlyClient | None = None) -> ReadOnlyScannerSource:
    """Build the real-app scanner source from the initialized F5 fee client."""
    return ReadOnlyScannerSource(market_client, account_fee_source=runner.account_fee_schedules)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autofund app")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-open-browser", action="store_true")
    parser.add_argument("--demo", action="store_true", help="deterministic browser certification; never contacts Bitso")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/mvp"))
    parser.add_argument("--scan-interval", type=int, default=300,
                        help="read-only market scanner refresh cadence in seconds")
    parser.add_argument("--no-scanner", action="store_true", help="disable research-only market scanning")
    args = parser.parse_args(argv)
    configure_rotating_log(args.artifacts)
    runner = DemoAutonomousRunner() if args.demo else ProductionAutonomousRunner(DEFAULT_JOURNAL)
    orchestrator = AutoFundOrchestrator(args.artifacts, runner, demo=args.demo)
    orchestrator.startup()
    if not args.no_scanner and not args.demo:
        # Research-only: GET-only discovery beside Production, never inside it.
        # Demo mode must never contact the exchange, so it never starts a scanner.
        try:
            assert isinstance(runner, ProductionAutonomousRunner)
            orchestrator.start_scanner(MarketScanner(production_scanner_source(runner),
                                                      interval_seconds=args.scan_interval),
                                       interval_seconds=args.scan_interval)
        except Exception:
            pass  # Scanner availability never blocks the product.
    elif args.demo:
        # Deterministic, network-free research fixture for browser certification.
        # A single synchronous scan keeps demo bytes reproducible; the background
        # refresh loop would emit timing-dependent checkpoints.
        demo_scanner = MarketScanner(DemoScannerSource())
        orchestrator._scanner = demo_scanner
        demo_scanner.scan(telemetry=lambda *a, **k: None)
    dist = Path(__file__).parents[3] / "frontend" / "dist"
    app = create_mvp_app(orchestrator, dist, host=args.host, port=args.port)
    url = f"http://{args.host}:{args.port}"
    print("AutoFund MVP 0.1")
    print("Dashboard available at: " + url)
    if not args.no_open_browser:
        def open_browser() -> None:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Timer(0.8, open_browser).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0

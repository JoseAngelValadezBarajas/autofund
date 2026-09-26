"""One-command AutoFund application.

The runtime mode is resolved through `autofund.demo.mode`, which fails closed and defaults to
DEMO. A clone of this repository therefore starts in a safe, credential-free, network-free mode
without any configuration, and reaching Production requires an explicit, named choice.
"""

import argparse
import threading
import webbrowser
from pathlib import Path

import uvicorn

from autofund.demo import (
    DemoIsolationError,
    IsolationReport,
    ModeError,
    RuntimeMode,
    assert_demo_is_isolated,
    credential_isolation,
    generate,
    resolve_mode,
)
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

#: Where a demo run writes its synthetic artifacts. Deliberately NOT `artifacts/mvp`, which holds
#: private runtime state: a demo must not be able to write into the directory a real run reads.
DEMO_ARTIFACTS = Path("artifacts/demo")


def production_scanner_source(runner: ProductionAutonomousRunner,
                              market_client: BitsoProductionReadOnlyClient | None = None) -> ReadOnlyScannerSource:
    """Build the real-app scanner source from the initialized F5 fee client."""
    return ReadOnlyScannerSource(market_client, account_fee_source=runner.account_fee_schedules)


def main(argv: list[str] | None = None, *, prog: str = "autofund app") -> int:
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-open-browser", action="store_true")
    parser.add_argument("--mode", default=None,
                        help="runtime mode: demo (default), shadow or production. "
                             "AUTOFUND_MODE is used when this is omitted")
    parser.add_argument("--demo", action="store_true",
                        help="shorthand for --mode demo (the default)")
    parser.add_argument("--artifacts", type=Path, default=None,
                        help="artifact root; defaults to artifacts/demo in demo mode and "
                             "artifacts/mvp otherwise")
    parser.add_argument("--scan-interval", type=int, default=300,
                        help="read-only market scanner refresh cadence in seconds")
    parser.add_argument("--no-scanner", action="store_true", help="disable research-only market scanning")
    args = parser.parse_args(argv)

    try:
        mode = resolve_mode("demo" if args.demo else args.mode)
    except ModeError as exc:
        # Fail closed and loud. A mode we cannot parse must never become a live run.
        parser.error(str(exc))
        return 2

    artifacts = args.artifacts or (DEMO_ARTIFACTS if mode.is_public_safe else Path("artifacts/mvp"))

    if mode.is_public_safe:
        # Strip every exchange credential and transport override before anything else runs. On a
        # machine where the operator has keys exported, demo must still be demo.
        with credential_isolation() as isolation:
            return _run(args=args, mode=mode, artifacts=artifacts, isolation=isolation)

    return _run(args=args, mode=mode, artifacts=artifacts, isolation=None)


def _run(*, args: argparse.Namespace, mode: RuntimeMode, artifacts: Path,
         isolation: IsolationReport | None) -> int:
    configure_rotating_log(artifacts)
    demo = mode.is_public_safe
    if demo:
        # Materialise the synthetic dataset the Control Center reads. Deterministic and
        # idempotent, so a repeat run does not change the registry digest.
        generate(artifacts_root=artifacts)
    runner = DemoAutonomousRunner() if demo else ProductionAutonomousRunner(DEFAULT_JOURNAL)
    orchestrator = AutoFundOrchestrator(artifacts, runner, demo=demo)
    orchestrator.startup()
    if not args.no_scanner and not demo:
        # Research-only: GET-only discovery beside Production, never inside it.
        # Demo mode must never contact the exchange, so it never starts a scanner.
        try:
            assert isinstance(runner, ProductionAutonomousRunner)
            orchestrator.start_scanner(MarketScanner(production_scanner_source(runner),
                                                      interval_seconds=args.scan_interval),
                                       interval_seconds=args.scan_interval)
        except Exception:
            pass  # Scanner availability never blocks the product.
    elif demo:
        # Deterministic, network-free research fixture for browser certification.
        # A single synchronous scan keeps demo bytes reproducible; the background
        # refresh loop would emit timing-dependent checkpoints.
        demo_scanner = MarketScanner(DemoScannerSource())
        orchestrator._scanner = demo_scanner
        demo_scanner.scan(telemetry=lambda *a, **k: None)

    if isolation is not None:
        # Verify the boundary held rather than assuming it: a demo run that acquired a live
        # capability is a defect, not a warning.
        try:
            assert_demo_is_isolated(report=isolation, snapshot=orchestrator.snapshot())
        except DemoIsolationError as exc:
            print(f"DEMO SAFETY VIOLATION: {exc}")
            return 3

    dist = Path(__file__).parents[3] / "frontend" / "dist"
    app = create_mvp_app(orchestrator, dist, host=args.host, port=args.port,
                         artifacts_root=artifacts)
    url = f"http://{args.host}:{args.port}"
    print(f"AutoFund MVP 0.3.0 — mode {mode.value}")
    if demo:
        print("DEMO MODE: synthetic data, no exchange credentials, no exchange access.")
        print("  Credentials present in this shell were removed for the duration of this run.")
        if isolation is not None:
            removed = isolation.public()["removed_credential_variables"]
            print(f"  Credential variables isolated: {removed or 'none were present'}")
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


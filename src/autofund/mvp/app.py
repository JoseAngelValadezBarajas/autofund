"""One-command AutoFund MVP application."""

import argparse
import threading
import webbrowser
from pathlib import Path

import uvicorn

from autofund.live.journal import DEFAULT_JOURNAL

from .api import create_mvp_app
from .orchestrator import (
    AutoFundOrchestrator,
    DemoAutonomousRunner,
    ProductionAutonomousRunner,
)
from .telemetry import configure_rotating_log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autofund app")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-open-browser", action="store_true")
    parser.add_argument("--demo", action="store_true", help="deterministic browser certification; never contacts Bitso")
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/mvp"))
    args = parser.parse_args(argv)
    configure_rotating_log(args.artifacts)
    runner = DemoAutonomousRunner() if args.demo else ProductionAutonomousRunner(DEFAULT_JOURNAL)
    orchestrator = AutoFundOrchestrator(args.artifacts, runner, demo=args.demo)
    orchestrator.startup()
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

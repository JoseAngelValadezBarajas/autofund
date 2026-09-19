import argparse
import logging
from pathlib import Path

import uvicorn

from .api import create_app
from .live import DEFAULT_RUNTIME_PATH
from .service import DashboardDataProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only AutoFund monitoring dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/f4"))
    parser.add_argument("--session", type=Path)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--demo-live", action="store_true")
    parser.add_argument("--demo-micro-live", action="store_true")
    parser.add_argument("--live-runtime", action="store_true")
    parser.add_argument("--runtime-path", type=Path)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    target = args.session or args.artifacts / "live_session"
    dist = Path(__file__).parents[3] / "frontend" / "dist"
    runtime_path = args.runtime_path or (None if args.demo or args.demo_live or args.demo_micro_live else DEFAULT_RUNTIME_PATH)
    uvicorn.run(create_app(DashboardDataProvider(target, demo=args.demo or args.demo_live or args.demo_micro_live, runtime_path=runtime_path, demo_live=args.demo_live, demo_micro_live=args.demo_micro_live), dist), host=args.host, port=args.port, log_level="info")
    return 0

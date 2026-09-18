import argparse
import logging
from pathlib import Path

import uvicorn

from .api import create_app
from .service import DashboardDataProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only AutoFund monitoring dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/f4"))
    parser.add_argument("--session", type=Path)
    parser.add_argument("--demo", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    target = args.session or args.artifacts / "live_session"
    dist = Path(__file__).parents[3] / "frontend" / "dist"
    uvicorn.run(create_app(DashboardDataProvider(target, demo=args.demo), dist), host=args.host, port=args.port, log_level="info")
    return 0

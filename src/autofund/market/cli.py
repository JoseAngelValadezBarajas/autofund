"""F2 HAS NO ORDER CAPABILITY. Public observation and offline data replay only."""

import argparse
import asyncio
import logging
from pathlib import Path

from autofund.replay.serialization import canonical_json

from .binance import BinancePublicMarketDataSource
from .clock import NetworkPolicy
from .errors import MarketDataError
from .models import QualityStatus
from .runner import LiveMarketRunner, replay_capture


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO"
    )
    root = parser.add_subparsers(dest="group", required=True)
    market = root.add_parser("market")
    commands = market.add_subparsers(dest="command", required=True)
    info = commands.add_parser("info")
    capture = commands.add_parser("capture")
    for command in (info, capture):
        command.add_argument("--source", choices=("binance",), default="binance")
        command.add_argument(
            "--symbol",
            default="BTCMXN",
            help="Verified via public exchangeInfo; never assumed available",
        )
    capture.add_argument("--interval", choices=("1s", "1m"), default="1m")
    capture.add_argument("--closed-candles", type=int, required=True)
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--max-seconds", type=int, default=900)
    capture.add_argument("--stale-after", type=int, default=15)
    playback = commands.add_parser("replay")
    playback.add_argument("capture", type=Path)
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "replay":
        result = await replay_capture(args.capture)
        print(
            canonical_json(
                {
                    "parity": "PASS",
                    "report": result.report(),
                    "signals": result.decisions,
                }
            )
        )
        return 1 if result.quality.status is QualityStatus.INVALID else 0
    policy = NetworkPolicy(stale_after=getattr(args, "stale_after", 15))
    source = BinancePublicMarketDataSource(
        args.symbol, getattr(args, "interval", "1m"), policy=policy
    )
    if args.command == "info":
        print(canonical_json(await source.get_market_info()))
        return 0
    result = await LiveMarketRunner().run(
        source,
        output=args.output,
        closed_candles=args.closed_candles,
        max_seconds=args.max_seconds,
    )
    print(canonical_json(result.report()))
    if result.stop_reason == "interrupted":
        return 130
    return (
        0
        if result.closed_candles == args.closed_candles
        and result.quality.status is not QualityStatus.INVALID
        else 1
    )


def main() -> int:
    args = _parser().parse_args()
    logging.basicConfig(
        level=args.log_level, format="%(levelname)s %(name)s %(message)s"
    )
    try:
        return asyncio.run(_run(args))
    except (MarketDataError, OSError) as exc:
        logging.error("market_data_error type=%s reason=%s", type(exc).__name__, exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

import argparse
from decimal import Decimal
from pathlib import Path

from autofund.observer.client import (
    BitsoProductionReadOnlyClient,
    ProductionCredentials,
)
from autofund.observer.errors import ObserverError, StrictReadOnlyLimitation
from autofund.observer.models import FeeSource, ShadowFee
from autofund.replay.serialization import canonical_json

from .capture import replay
from .config import ShadowConfig
from .reporting import aggregate_reports, session_report
from .runner import run_public


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Virtual shadow trading; no real execution"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--source", choices=("bitso",), default="bitso")
    run.add_argument("--book", default="btc_mxn")
    run.add_argument("--interval", choices=("1m",), default="1m")
    run.add_argument("--initial-equity", type=Decimal, default=Decimal("50"))
    run.add_argument("--max-deployment", type=Decimal, default=Decimal("0.50"))
    run.add_argument("--single-order-cap", type=Decimal, default=Decimal("10"))
    run.add_argument("--extra-slippage-bps", type=Decimal, default=Decimal("0"))
    run.add_argument("--max-spread-bps", type=Decimal, default=Decimal("100"))
    run.add_argument("--max-orderbook-age", type=int, default=15)
    run.add_argument("--duration", type=int, default=1800, help="seconds")
    run.add_argument("--closed-candles", type=int)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--resume", action="store_true")
    run.add_argument(
        "--account-fees",
        action="store_true",
        help="read account fees with operator-confirmed Production read-only credentials",
    )
    run.add_argument(
        "--estimated-fee",
        type=Decimal,
        help="explicit shadow estimate; never account-confirmed",
    )
    playback = commands.add_parser("replay")
    playback.add_argument("session", type=Path)
    report = commands.add_parser("report")
    report.add_argument("session", type=Path)
    report.add_argument("--period", choices=("daily", "weekly"), default="weekly")
    try:
        args = parser.parse_args(argv)
        if args.command == "run":
            config = ShadowConfig(
                initial_equity=args.initial_equity,
                max_deployment=args.max_deployment,
                single_order_cap=args.single_order_cap,
                extra_slippage_bps=args.extra_slippage_bps,
                max_spread_bps=args.max_spread_bps,
                max_orderbook_age_seconds=args.max_orderbook_age,
                interval=args.interval,
            )
            fee = (
                ShadowFee(args.estimated_fee, FeeSource.CONFIGURED_ESTIMATE)
                if args.estimated_fee is not None
                else None
            )
            if args.account_fees:
                if args.estimated_fee is not None:
                    raise ValueError("choose account fees or configured estimate")
                try:
                    fee = BitsoProductionReadOnlyClient(
                        ProductionCredentials.from_environment()
                    ).fee_schedule(args.book)
                except StrictReadOnlyLimitation:
                    fee = None  # Public fee schedule, no permission escalation.
            result = run_public(
                output=args.output,
                book=args.book,
                config=config,
                duration=args.duration,
                closed_candles=args.closed_candles,
                fee=fee,
                resume=args.resume,
            )
            print(
                canonical_json(
                    {
                        "result_fingerprint": result["result_fingerprint"],
                        "quality": result["quality"],
                        "metrics": result["metrics"],
                    }
                )
            )
        elif args.command == "replay":
            result = replay(args.session)
            print(
                canonical_json(
                    {
                        "parity": "PASS",
                        "result_fingerprint": result["result_fingerprint"],
                    }
                )
            )
        else:
            result = (
                session_report(args.session)
                if (args.session / "manifest.json").exists()
                else aggregate_reports(args.session, args.period)
            )
            print(canonical_json(result))
        return 1 if result.get("quality") == "INVALID" else 0
    except (ObserverError, OSError, ValueError) as exc:
        print(canonical_json({"error_type": type(exc).__name__, "result": "BLOCKED"}))
        return 2

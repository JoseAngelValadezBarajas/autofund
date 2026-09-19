"""Live CLI: preflight and recovery are GET-only; BUY requires a human TTY."""

import argparse
import sys
from decimal import Decimal

from autofund.dashboard.runtime import ensure_dashboard
from autofund.replay.serialization import canonical_json

from .client import BitsoProductionLiveClient, LiveCredentials
from .execution import LiveExecution
from .journal import LiveExecutionJournal
from .models import LiveConfig
from .observability import LivePublisher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autofund live")
    parser.add_argument("command", choices=("preflight", "recover", "certify-buy"))
    parser.add_argument("--book", choices=("btc_mxn",), default="btc_mxn")
    parser.add_argument("--allocated-capital", type=Decimal, default=Decimal("50"))
    parser.add_argument("--max-deployment", type=Decimal, default=Decimal("0.50"))
    parser.add_argument("--single-order-cap", type=Decimal, default=Decimal("11"))
    parser.add_argument("--max-local-snapshot-age", type=Decimal, default=Decimal("1"))
    parser.add_argument("--slippage-tolerance", type=Decimal, required=True)
    parser.add_argument("--minor-budget", type=Decimal)
    parser.add_argument("--confirm-real-money", action="store_true")
    parser.add_argument("--no-open-dashboard", action="store_true")
    args = parser.parse_args(argv)
    publisher: LivePublisher | None = None
    try:
        config = LiveConfig(args.allocated_capital, args.max_deployment, args.single_order_cap,
                            args.slippage_tolerance,
                            max_local_snapshot_age_seconds=args.max_local_snapshot_age)
        client = BitsoProductionLiveClient(LiveCredentials.from_environment(), single_order_cap=config.single_order_cap)
        with LiveExecutionJournal() as journal:
            publisher = LivePublisher()
            execution = LiveExecution(client, journal, config, emit=publisher.event)
            if args.command == "certify-buy":
                publisher.start()
                publisher.event("PREFLIGHT_STARTED", execution.public())
                print("Dashboard available at: " + ensure_dashboard(journal.path.parent, open_browser=not args.no_open_dashboard))
                if execution.unresolved:
                    execution.recover()
            if args.command == "recover":
                execution.recover()
                print(canonical_json(execution.public()))
                return 2 if execution.unresolved else 0
            checked = execution.check(args.minor_budget)
            print("LIVE PREFLIGHT")
            print(canonical_json(checked.public()))
            if not checked.ready:
                if args.command == "certify-buy":
                    publisher.market_at = checked.depth.timestamp
                    publisher.event("PREFLIGHT_REJECTED", execution.public(), status="HALTED")
                return 2
            if args.command == "preflight":
                return 0
            if args.minor_budget is None:
                parser.error("certify-buy requires an explicit --minor-budget")
            publisher.market_at = checked.depth.timestamp
            publisher.event("PREFLIGHT_PASS", execution.public())
            intent = execution.create(checked)
            execution.confirm_and_submit(intent, confirm_real_money=args.confirm_real_money,
                                         stdin=sys.stdin, stdout=sys.stdout)
            print(canonical_json(execution.public()))
            publisher.event("STOPPED", execution.public(), status="STOPPED")
            return 2 if execution.unresolved else 0
    except Exception:
        print("LIVE_OPERATION_BLOCKED — details withheld to protect credentials", file=sys.stderr)
        return 2
    finally:
        if publisher:
            publisher.close()

import argparse
import json

from autofund.replay.serialization import canonical_json

from .client import BitsoProductionReadOnlyClient, ProductionCredentials
from .errors import ObserverError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Bitso Production observer; strictly GET only"
    )
    parser.add_argument(
        "command", choices=("status", "market-info", "balances", "fees")
    )
    parser.add_argument("--book", default="btc_mxn")
    parser.add_argument("--show-balances", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "market-info":
            limits, fee = BitsoProductionReadOnlyClient().market_info(args.book)
            result = {"limits": limits, "fee": fee}
        else:
            client = BitsoProductionReadOnlyClient(
                ProductionCredentials.from_environment()
            )
            if args.command == "status":
                result = client.status(args.book, show_balances=args.show_balances)
            elif args.command == "balances":
                balances = client.balances()
                result = {
                    "real_exchange_balances": balances
                    if args.show_balances
                    else "AVAILABLE_REDACTED"
                }
            else:
                result = {"fee": client.fee_schedule(args.book)}
        print(canonical_json(result))
        return 0
    except ObserverError as exc:
        # Locally fixed safe messages only; never serialize credentials/HTTP errors.
        print(
            json.dumps(
                {
                    "environment": "PRODUCTION",
                    "mode": "READ_ONLY",
                    "error": type(exc).__name__,
                    "reason": str(exc),
                }
            )
        )
        return 2

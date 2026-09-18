import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from autofund.errors import AutoFundError
from autofund.replay.serialization import canonical_json, fingerprint

from .auth import BitsoCredentials
from .certification import certify
from .client import BitsoClient
from .errors import BitsoAuthenticationError, BitsoError
from .execution import StageExecutionEngine
from .journal import ExecutionJournal
from .models import ExecutionPolicy


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Bitso Stage only; no production capability"
    )
    commands = root.add_subparsers(dest="command", required=True)
    for name in ("status", "market-info", "balances", "fees"):
        command = commands.add_parser(name)
        if name in ("market-info", "fees"):
            command.add_argument("book", nargs="?", default="btc_mxn")
    for name in ("order-test", "reconcile", "cancel"):
        command = commands.add_parser(name)
        command.add_argument(
            "--journal", type=Path, default=Path("artifacts/f3_execution.jsonl")
        )
        command.add_argument("--single-order-cap", type=Decimal, default=Decimal("10"))
        if name == "order-test":
            command.add_argument("--book", default="btc_mxn")
            command.add_argument("--confirm-stage", action="store_true")
            command.add_argument(
                "--output",
                type=Path,
                default=Path("artifacts/f3_stage_certification.json"),
            )
        if name == "cancel":
            command.add_argument("origin_id")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "market-info":
            print(canonical_json(BitsoClient().get_book(args.book)))
            return 0
        try:
            credentials = BitsoCredentials.from_environment()
        except BitsoAuthenticationError:
            if args.command == "order-test":
                skipped = {
                    "schema_version": "autofund.bitso.certification.v1",
                    "environment": "stage",
                    "market": args.book,
                    "result": "SKIPPED",
                    "reason": "Stage credentials absent",
                    "order_lifecycle": "NOT_EXECUTED",
                    "live_fill": "SKIPPED",
                }
                evidence = skipped | {
                    "timestamp": datetime.now(UTC),
                    "semantic_fingerprint": fingerprint(skipped),
                }
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(
                    canonical_json(evidence) + "\n", encoding="utf-8"
                )
            raise
        client = BitsoClient(credentials)
        if args.command == "status":
            result = {
                "environment": "stage",
                "credentials_present": True,
                "market": client.get_book("btc_mxn"),
                "balance_reachable": bool(client.get_balances()),
                "fees": client.get_fees("btc_mxn"),
                "authentication": "PASS",
            }
        elif args.command == "balances":
            result = {"balances": client.get_balances()}
        elif args.command == "fees":
            result = {"fees": client.get_fees(args.book)}
        else:
            policy = ExecutionPolicy(single_order_cap_mxn=args.single_order_cap)
            if args.command == "order-test":
                result = certify(
                    client,
                    book_name=args.book,
                    journal_path=args.journal,
                    output=args.output,
                    policy=policy,
                    confirm_stage=args.confirm_stage,
                )
            else:
                with ExecutionJournal(args.journal) as journal:
                    engine = StageExecutionEngine(client, journal, policy)
                    engine.startup()
                    if args.command == "cancel":
                        engine.cancel(args.origin_id)
                    result = {
                        "health": engine.health,
                        "reconciliation": engine.last_reconciliation,
                    }
        print(canonical_json(result))
        return (
            1
            if result.get("result") == "FAIL" or result.get("health") == "HALTED"
            else 0
        )
    except (BitsoError, AutoFundError, OSError, InvalidOperation, ValueError) as exc:
        # Never echo external payloads, request objects, or exception chains.
        print(
            json.dumps(
                {
                    "environment": "stage",
                    "error_type": type(exc).__name__,
                    "result": "BLOCKED",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

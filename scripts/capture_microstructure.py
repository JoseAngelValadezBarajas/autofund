"""Bounded forward microstructure collector runner.

Run this to collect the evidence that candle history structurally cannot provide. See
`autofund.mvp.microstructure` for why that evidence is needed and what it may not be used
for.

**This process has no order capability.** It holds a GET-only client, touches only
`order_book` and `trades`, and cannot place, cancel or replace anything. It is also kept
entirely separate from the Production ledger and the shadow accounting: microstructure
evidence answers an execution question and has no authority over financial truth, so a
research capture failure must not be able to perturb a financial record.

**Duration is always explicit.** An unbounded collector on a machine with finite disk is a
latent outage, so a duration is required and retention is enforced on every append.

    python scripts/capture_microstructure.py --seconds 3600
"""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from autofund.mvp.microstructure import MicrostructureStore
from autofund.mvp.microstructure_bitso import (
    DEFAULT_DEPTH_LEVELS,
    build_collector,
    resolve_books,
)
from autofund.observer.client import (
    BitsoProductionReadOnlyClient,
    ProductionCredentials,
)
from autofund.observer.errors import AuthenticationUnavailable

ROOT = Path(__file__).resolve().parents[1] / "artifacts/mvp/horizon-microstructure"
CAPTURE_ROOT = ROOT / "microstructure"
REPORT = ROOT / "microstructure-capture.json"

DEFAULT_BOOKS = ("btc_mxn", "eth_mxn", "sol_mxn", "xrp_mxn")
DEFAULT_INTERVAL_SECONDS = 5


def production_client() -> BitsoProductionReadOnlyClient:
    from os import environ

    key = environ.get("AUTOFUND_BITSO_PROD_API_KEY", "")
    secret = environ.get("AUTOFUND_BITSO_PROD_API_SECRET", "")
    confirmed = environ.get("AUTOFUND_BITSO_PROD_READONLY_CONFIRMED") == "true"
    if not (key and secret and confirmed):
        raise AuthenticationUnavailable("Production read-only credentials not confirmed")
    return BitsoProductionReadOnlyClient(
        ProductionCredentials(api_key=key, api_secret=secret, readonly_confirmed=True))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, required=True,
                        help="explicit capture duration; there is no unbounded mode")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--books", nargs="*", default=list(DEFAULT_BOOKS))
    parser.add_argument("--depth-levels", type=int, default=DEFAULT_DEPTH_LEVELS)
    parser.add_argument("--retention-hours", type=int, default=None)
    args = parser.parse_args()

    if args.seconds <= 0:
        raise SystemExit("capture duration must be positive")
    if args.interval <= 0:
        raise SystemExit("capture interval must be positive")

    ROOT.mkdir(parents=True, exist_ok=True)
    client = production_client()

    kwargs = {} if args.retention_hours is None else {
        "retention_hours": args.retention_hours}
    store = MicrostructureStore(root=CAPTURE_ROOT, **kwargs)
    books = resolve_books(client, tuple(args.books))
    collector = build_collector(client=client, store=store, books=books,
                               depth_levels=args.depth_levels)

    started = datetime.now(UTC)
    deadline = time.monotonic() + args.seconds
    passes = 0
    while time.monotonic() < deadline:
        collector.capture_once()
        passes += 1
        time.sleep(min(args.interval, max(0.0, deadline - time.monotonic())))
    ended = datetime.now(UTC)

    quality = collector.quality.public()
    payload = {
        "captured_at": ended.isoformat(), "started_at": started.isoformat(),
        "duration_seconds": int((ended - started).total_seconds()),
        "capture_passes": passes, "interval_seconds": args.interval,
        "books": list(books), "collector": collector.public(), "quality": quality,
        "manifest": store.manifest(),
        "order_capability": "NONE", "production_mutation": "NONE",
        "credentials_scope": "READ_ONLY_PUBLIC_ENDPOINTS",
        "separate_from_production_ledger": True,
        "queue_position_observable": False,
        "passive_fill_exactness": "BOUNDED_ONLY",
        "adverse_selection_analysis_possible": quality["events_stored"] > len(books),
        "anomalies_repaired": 0,
    }
    REPORT.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"capture_passes": passes, "quality": quality,
                      "books": list(books)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

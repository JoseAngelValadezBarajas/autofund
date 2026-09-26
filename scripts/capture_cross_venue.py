"""Bounded, read-only cross-venue capture (MVP 0.2.8).

Reads the incumbent's public ticker and one or more reference venues' public top of book, pairs
them into synchronized observations and persists them. There is no order path, no credential and
no authenticated call: the sources are a single public endpoint each and the collector exposes no
method that mutates anything.

`--cycles` is required rather than defaulted to `None`. An unbounded capture loop is not a
research instrument; it is an unattended process that quietly fills a disk and produces evidence
nobody can attribute to a stated window. A bounded run always has a report with a start, an end
and a failure count.

Usage:
    python scripts/capture_cross_venue.py --cycles 60 --interval 5
    python scripts/capture_cross_venue.py --cycles 12 --interval 5 --depth
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autofund.mvp.cross_venue_capture import (  # noqa: E402
    DEFAULT_ORDER_CAPACITY_MXN,
    BinancePublicTopOfBook,
    BitsoPublicTopOfBook,
    CrossVenueCollector,
    CrossVenueStore,
    default_pairs,
    measure_bitso_depth,
    reference_unavailable,
)

CAPTURE_ROOT = ROOT / "artifacts" / "mvp" / "cross-venue" / "capture"
REPORT_PATH = ROOT / "artifacts" / "mvp" / "cross-venue" / "capture-report.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="read-only cross-venue capture")
    parser.add_argument("--cycles", type=int, required=True,
                        help="number of capture cycles (required: capture is never unbounded)")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="seconds between cycles")
    parser.add_argument("--root", type=Path, default=CAPTURE_ROOT,
                        help="where to persist the JSONL capture")
    parser.add_argument("--depth", action="store_true",
                        help="also probe executable depth at the order cap on every cycle")
    parser.add_argument("--depth-every", type=int, default=6,
                        help="probe depth every N cycles when --depth is given")
    args = parser.parse_args()

    if args.cycles <= 0:
        raise SystemExit("--cycles must be positive")

    store = CrossVenueStore(args.root)
    collector = CrossVenueCollector(
        incumbent=BitsoPublicTopOfBook(),
        references={"Binance": BinancePublicTopOfBook()},
        store=store, pairs=default_pairs())

    depth_records: list[dict[str, object]] = []

    def progress(done: int, total: int) -> None:
        if args.depth and (done % args.depth_every == 0 or done == total):
            for pair in default_pairs():
                for side in ("BUY", "SELL"):
                    try:
                        probe = measure_bitso_depth(book=pair.incumbent_symbol, side=side,
                                                    requested_mxn=DEFAULT_ORDER_CAPACITY_MXN)
                        depth_records.append({"cycle": done, **probe.public()})
                    except Exception as exc:
                        depth_records.append({"cycle": done, "book": pair.incumbent_symbol,
                                              "side": side, "error": type(exc).__name__})
        if done % 6 == 0 or done == total:
            print(f"  {datetime.now(UTC).strftime('%H:%M:%S')} cycle {done}/{total}",
                  flush=True)

    print(f"capturing {args.cycles} cycles at {args.interval}s "
          f"(~{args.cycles * args.interval / 60:.1f} minutes)", flush=True)
    report = collector.run(cycles=args.cycles, cycle_interval_seconds=args.interval,
                           on_cycle=progress)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "captured_at": datetime.now(UTC).isoformat(),
        "report": report.public(),
        "pairs_with_reference": [pair.key for pair in default_pairs()],
        "pairs_without_usable_reference": [
            {"pair": pair.key, "would_be_reference": pair.reference_symbol,
             "reason": "the reference market was not trading during development"}
            for pair in reference_unavailable()],
        "depth_probes": depth_records,
        "order_capacity_mxn": str(DEFAULT_ORDER_CAPACITY_MXN),
        "authenticated_calls": 0,
        "orders_submitted": 0,
        "transfers_performed": 0,
    }
    REPORT_PATH.write_text(json.dumps(payload, indent=2, default=str) + "\n",
                           encoding="utf-8")

    print()
    print(json.dumps({
        "duration_seconds": report.public()["duration_seconds"],
        "cycles": report.cycles,
        "records": report.records,
        "valid_records": report.valid_records,
        "per_pair": report.per_pair,
        "failures": report.failures,
        "median_skew_seconds": report.skew_median_seconds,
        "bytes_on_disk": store.total_bytes(),
        "depth_probes": len(depth_records),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

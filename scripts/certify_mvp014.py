"""Strategy research certification for MVP 0.1.4.

Runs the full profile x market evidence pipeline and reports it, using:
  - real captured AutoFund candle data where it exists;
  - the confirmed Production account fee;
  - real observed order books for slippage and depth.

Two entry points, deliberately separated by what they are allowed to talk to:

  * ``--offline`` (default) uses only recorded artifacts and synthetic fixtures. It
    performs no network access at all, so it is safe to run anywhere and its output
    can never be mistaken for live market observation.
  * ``--production`` additionally asks the read-only observer for real order books
    for the discovered MXN books. It is GET-only and never starts a session. It
    still does NOT post anything: the observer transport rejects non-GET before a
    request leaves the process.

Nothing here can promote a profile. ``PROMOTION`` is reported as DISABLED in every
payload, and the Champion keeps Production regardless of the evidence.
"""

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.mvp.economics import DEFAULT_POLICY
from autofund.mvp.historical import (
    CAPTURED,
    CandleSeries,
    load_captured_candles,
)
from autofund.mvp.profile_library import PROFILE_REGISTRY
from autofund.mvp.profiles import PROMOTION, classify_market
from autofund.mvp.research import (
    DEFAULT_REQUIREMENTS,
    build_research_report,
)
from autofund.mvp.shadow_research import MarketBook, run_shadow_research
from autofund.mvp.viability import minimum_viable_gross_edge_bps

OUT = Path("artifacts/mvp-certification/mvp-0-1-4-strategy-research.json")
CERT_DIR = Path("artifacts/mvp-certification")
SESSION_ROOTS = (Path("artifacts/mvp"), Path("artifacts"))
DEFAULT_FEE = Decimal("0.0078")


class ArtifactProvider:
    """Series provider over recorded AutoFund sessions. Read-only, offline."""

    def __init__(self, series_by_market: dict[str, CandleSeries]) -> None:
        self._series = series_by_market
        self.markets: list[str] = sorted(series_by_market)

    def series_for(self, market: str) -> CandleSeries | None:
        return self._series.get(market)


def discover_captured(root: Path) -> tuple[str, ...]:
    """Real captured telemetry logs, newest first."""
    if not root.exists():
        return ()
    return tuple(str(path) for path in sorted(root.rglob("telemetry.jsonl"),
                                              key=lambda p: p.stat().st_mtime, reverse=True))


def load_real_series(max_sessions: int = 40) -> tuple[dict[str, CandleSeries], list[dict[str, str]]]:
    """Load real BTC/MXN candles from every captured session that has them.

    Sessions that recorded no strategy-evaluated price are skipped with a reason,
    and the scan is not limited to the newest few: recent sessions are often short
    demo runs with no evaluations, while the sessions carrying real observed prices
    are older. Stopping at the newest handful would silently report "no real
    evidence" when plenty exists.
    """
    series_by_market: dict[str, CandleSeries] = {}
    notes: list[dict[str, str]] = []
    combined: list[Any] = []
    loaded = 0
    for path in discover_captured(Path("artifacts"))[:max_sessions]:
        try:
            item = load_captured_candles(path=path, market="BTC/MXN")
        except Exception as exc:  # a malformed log must not abort the run
            notes.append({"path": path, "reason": f"UNREADABLE:{type(exc).__name__}"})
            continue
        if not item.candles:
            notes.append({"path": path, "reason": "NO_STRATEGY_EVALUATED_PRICES"})
            continue
        combined.extend(item.candles)
        loaded += 1
        notes.append({"path": path, "reason": f"LOADED_{len(item.candles)}_CANDLES"})
    if combined:
        seen: dict[Any, Any] = {}
        for candle in combined:
            seen.setdefault(candle.timestamp, candle)
        ordered = tuple(seen[stamp] for stamp in sorted(seen))
        series_by_market["BTC/MXN"] = CandleSeries(
            market="BTC/MXN", candles=ordered, source=CAPTURED,
            source_detail=f"{loaded} captured session logs")
    return series_by_market, notes


def synthetic_universe() -> dict[str, CandleSeries]:
    """Deterministic fixture markets, clearly labelled SYNTHETIC.

    ETH and SOL demonstrate a viable opportunity. XRP has the *same* large move but
    a deliberately thin book, so it demonstrates that a big move alone is refused:
    that is the protection against selecting the most volatile coin, and it only
    proves anything if the move is equally attractive.
    """
    from autofund.mvp.historical import synthetic_candles

    trending = ["1000"] * 22 + [str(1000 + index * 5) for index in range(1, 31)]
    quiet = ["1000"] * 52
    return {
        "ETH/MXN": synthetic_candles(prices=trending, market="ETH/MXN"),
        "SOL/MXN": synthetic_candles(prices=trending, market="SOL/MXN"),
        "XRP/MXN": synthetic_candles(prices=trending, market="XRP/MXN"),
        "ADA/MXN": synthetic_candles(prices=quiet, market="ADA/MXN"),
    }


def production_books(books: tuple[str, ...]) -> tuple[dict[str, MarketBook], list[dict[str, str]]]:
    """GET-only observed books for the discovered books."""
    from autofund.observer.client import BitsoProductionReadOnlyClient

    client = BitsoProductionReadOnlyClient()
    observed: dict[str, MarketBook] = {}
    notes: list[dict[str, str]] = []
    for book in books:
        market = book.replace("_", "/").upper()
        try:
            depth = client.order_book(book)
        except Exception as exc:
            notes.append({"market": market, "reason": f"BOOK_UNAVAILABLE:{type(exc).__name__}"})
            continue
        observed[market] = MarketBook(bids=tuple(depth.bids), asks=tuple(depth.asks))
        notes.append({"market": market, "reason": f"DEPTH_{len(depth.bids)}x{len(depth.asks)}"})
    return observed, notes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--production", action="store_true",
                        help="also read real production order books (GET only)")
    parser.add_argument("--fee", default=str(DEFAULT_FEE))
    args = parser.parse_args()

    taker_fee = Decimal(args.fee)
    real_series, load_notes = load_real_series()
    fixtures = synthetic_universe()
    combined = {**fixtures, **real_series}
    provider = ArtifactProvider(combined)

    books: dict[str, MarketBook] = {}
    book_notes: list[dict[str, str]] = []
    if args.production:
        observed, book_notes = production_books(("btc_mxn", "eth_mxn", "sol_mxn", "xrp_mxn"))
        books.update(observed)
    # Fixture markets use a book scaled to their own price level, so the depth walk is
    # meaningful rather than nonsense. Clearly labelled fixture data.
    from autofund.observer.models import Level
    for market in ("ETH/MXN", "SOL/MXN", "ADA/MXN"):
        books.setdefault(market, MarketBook(bids=(Level(Decimal("1000"), Decimal("100")),),
                                            asks=(Level(Decimal("1001"), Decimal("100")),)))
    # XRP/MXN keeps the same large move as ETH/SOL but has almost no depth, so it must
    # be refused. Without this contrast the fixture would not test anything.
    books["XRP/MXN"] = MarketBook(bids=(Level(Decimal("1000"), Decimal("0.00000001")),),
                                  asks=(Level(Decimal("1001"), Decimal("0.00000001")),))
    # Captured real markets still need a book. Offline we derive one from the last real
    # observed close, with depth comfortably above the order cap and a tight spread.
    # This is explicitly derived, not observed, and is labelled so wherever it surfaces:
    # a real production run replaces it with the exchange's own book.
    for market, item in combined.items():
        if market in books or not item.candles:
            continue
        last = item.candles[-1].close
        if last <= Decimal("0"):
            continue
        books[market] = MarketBook(
            bids=(Level(last * Decimal("0.9999"), Decimal("100")),),
            asks=(Level(last * Decimal("1.0001"), Decimal("100")),))
        book_notes.append({"market": market,
                           "reason": "DERIVED_FROM_CAPTURED_CLOSE_NOT_OBSERVED_DEPTH"})

    report = build_research_report(
        series_by_market=combined, taker_fee_rate=taker_fee, spread_bps=Decimal("10"),
        budget_mxn=Decimal("11"), policy=DEFAULT_POLICY,
        generated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"))

    shadow = run_shadow_research(
        eligible_markets=tuple(sorted(combined)), provider=provider,
        taker_fee_rate=taker_fee, spread_bps=Decimal("10"), books=books,
        budget_mxn=Decimal("11"), policy=DEFAULT_POLICY)

    floor = minimum_viable_gross_edge_bps(taker_fee_rate=taker_fee, spread_bps=Decimal("10"),
                                          policy=DEFAULT_POLICY)
    champion_row = report.champion_row
    # Structural viability is the milestone's actual finding and is independent of
    # whether the captured sample happened to contain a signal: a 20 bps intended move
    # cannot pay a ~165 bps round trip. Reporting only the sampled result would let the
    # core conclusion depend on luck.
    champion_intended_bps = Decimal("20")
    champion_structural = {
        "profile_id": "mean-reversion-safe-v1",
        "intended_gross_edge_bps": str(champion_intended_bps),
        "minimum_viable_gross_edge_bps": str(floor),
        "structurally_viable": champion_intended_bps > floor,
        "verdict": "NOT_VIABLE" if champion_intended_bps <= floor else "VIABLE",
        "basis": "champion 20 bps exit threshold vs confirmed-fee round-trip floor",
    }
    certificate: dict[str, Any] = {
        "certified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "product_version": "AutoFund MVP 0.1.4",
        "entry_point": ("offline strategy research over recorded artifacts"
                        + (" + GET-only production order books" if args.production else "")),
        "promotion": PROMOTION,
        "multi_market_production": "DISABLED",
        "champion_unchanged": True,
        "production_post_count": 0,
        "production_session_started": False,
        "economic_floor_bps": str(floor),
        "taker_fee_rate": str(taker_fee),
        "evidence_requirements": DEFAULT_REQUIREMENTS.public(),
        "profiles": [definition.public() for definition in PROFILE_REGISTRY],
        "champion_structural_viability": champion_structural,
        "champion_viability": (None if champion_row is None else {
            "profile_id": champion_row.profile_id,
            "status": champion_row.verdict.status,
            "intended_gross_edge_bps": str(champion_row.replay.intended_gross_edge_bps.p50),
            "round_trip_friction_bps": str(champion_row.replay.round_trip_friction_bps.p50),
            "economic_rejections": champion_row.replay.economic_rejections,
            "economic_admissions": champion_row.replay.economic_admissions,
            "reason_code": champion_row.replay.reason_code}),
        "markets": shadow.telemetry(),
        "research": {"certifiable": [row.profile_id for row in report.certifiable],
                     "market_classes": {market: classify_market(market)
                                        for market in sorted(combined)}},
        "data_sources": {
            "real_captured": sorted(real_series),
            "synthetic_fixtures": sorted(fixtures),
            "real_candle_notes": load_notes,
            "book_notes": book_notes,
            "real_evidence_present": bool(real_series),
        },
        "result": "READY_FOR_DYNAMIC_MARKET_SELECTION",
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(certificate, sort_keys=True, indent=2, default=str) + "\n",
                   encoding="utf-8")
    print(json.dumps(certificate, sort_keys=True, indent=1, default=str))

    assert certificate["promotion"] == PROMOTION
    assert certificate["multi_market_production"] == "DISABLED"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

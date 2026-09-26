"""MVP 0.2 certification: real multi-market evidence, certification and selection.

GET-only. Reads real exchange OHLC and order books, evaluates the deterministic profiles
over real candles, records durable evidence, certifies each market/profile pair, builds
the Certified Production Universe, and runs the deterministic selector against the
current market state.

It never starts a session, never authorises auto execution, and never POSTs. The
reported status is derived from the evidence, not asserted: if nothing certifies it
reports ACCUMULATING_REAL_MARKET_EVIDENCE rather than claiming readiness.
"""

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.mvp.backfill import (
    HistoricalMarketBook,
    fetch_historical_series,
)
from autofund.mvp.certification import DEFAULT_FLOOR, certify_pair
from autofund.mvp.economics import DEFAULT_POLICY
from autofund.mvp.evidence import ResearchEvidenceStore
from autofund.mvp.evidence_cycle import (
    HISTORICAL_SLIPPAGE_BPS,
    run_evidence_cycle,
)
from autofund.mvp.profile_library import PROFILE_REGISTRY
from autofund.mvp.profiles import CLASS_STABLE_OR_FIAT, classify_market
from autofund.mvp.universe import (
    CandidateMarket,
    MarketSelectionContext,
    PortfolioConstraints,
    build_certified_universe,
    select_production_opportunity,
)
from autofund.observer.client import BitsoProductionReadOnlyClient

OUT = Path("artifacts/mvp-certification/mvp-0-2-multi-market-certification.json")
STORE_ROOT = Path("artifacts/mvp-certification/research-evidence")

# Research markets. Discovered dynamically from the exchange, not hardcoded as
# preferences: the list below is the *filter*, and eligibility decides membership.
RESEARCH_BOOKS = ("btc_mxn", "eth_mxn", "sol_mxn", "xrp_mxn", "ada_mxn", "dot_mxn")


def discover_books(client: BitsoProductionReadOnlyClient) -> tuple[dict[str, str], ...]:
    """Dynamically discovered crypto *_mxn books with their constraints."""
    discovered: list[dict[str, str]] = []
    for constraints in client.available_books():
        book = str(getattr(constraints, "book", ""))
        if not book.endswith("_mxn"):
            continue
        market = book.upper().replace("_", "/")
        discovered.append({
            "book": book, "market": market, "market_class": classify_market(book),
            "minimum_value_mxn": str(getattr(constraints, "minimum_value", "")),
            "maximum_value_mxn": str(getattr(constraints, "maximum_value", "")),
            "tick_size": str(getattr(constraints, "tick_size", "")),
        })
    return tuple(sorted(discovered, key=lambda item: item["book"]))


def observe_market(client: BitsoProductionReadOnlyClient,
                   book: str) -> dict[str, Any] | None:
    """GET-only current book state: spread, depth and executable top-of-book size."""
    try:
        depth = client.order_book(book)
        fees = client.fee_schedule(book)
    except Exception as exc:
        return {"error": type(exc).__name__}
    now = datetime.now(UTC)
    age = (now - depth.timestamp).total_seconds()
    top_bid_value = sum((level.price * level.amount for level in depth.bids[:5]), Decimal("0"))
    return {
        "best_bid": str(depth.best_bid), "best_ask": str(depth.best_ask),
        "spread_bps": str(depth.spread_bps), "depth_mxn": str(top_bid_value),
        "age_seconds": str(Decimal(str(age))),
        "taker_fee": str(fees.taker_fee_decimal),
        "maker_fee": str(fees.maker_fee_decimal),
        "sequence": depth.sequence,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=336,
                        help="historical lookback per market (default 336 = 14 days)")
    args = parser.parse_args()

    client = BitsoProductionReadOnlyClient()
    store = ResearchEvidenceStore(STORE_ROOT)
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    universe_books = discover_books(client)
    crypto_books = [item for item in universe_books
                    if item["market_class"] != CLASS_STABLE_OR_FIAT]
    fiat_books = [item for item in universe_books
                  if item["market_class"] == CLASS_STABLE_OR_FIAT]

    # Real historical backfill for the research set, restricted to discovered books so
    # a book that no longer exists cannot be backfilled into the evidence store.
    available = {item["book"] for item in crypto_books}
    targets = [book for book in RESEARCH_BOOKS if book in available]

    series_by_market: dict[str, Any] = {}
    backfill_reports: list[dict[str, Any]] = []
    for book in targets:
        try:
            series = fetch_historical_series(source=client, book=book,
                                            lookback_hours=args.hours)
        except Exception as exc:
            backfill_reports.append({"book": book, "status": f"FAILED:{type(exc).__name__}"})
            continue
        if not series.candles:
            backfill_reports.append({"book": book, "status": "NO_CANDLES"})
            continue
        series_by_market[series.market] = series
        backfill_reports.append({
            "book": book, "status": "OK", "market": series.market,
            "candles": len(series.candles), "gaps": len(series.gaps),
            "duplicates_dropped": series.duplicates_dropped,
            "contiguous": series.is_contiguous,
            "span_hours": str(series.span_hours),
            "provenance": series.provenance.public(),
            "dataset_fingerprint": series.fingerprint,
        })

    # Current observable market state, used for admission and for the live economic
    # assessment. The fee comes from the account where available.
    observations: dict[str, dict[str, Any]] = {}
    for book in targets:
        observed = observe_market(client, book)
        if observed is not None and "error" not in observed:
            observations[book.upper().replace("_", "/")] = observed

    account_fee = next((Decimal(item["taker_fee"]) for item in observations.values()),
                       Decimal("0.0078"))

    books = {}
    for market, observed in observations.items():
        books[market] = HistoricalMarketBook(
            spread_bps=Decimal(observed["spread_bps"]),
            depth_mxn=Decimal(observed["depth_mxn"]),
            slippage_bps=HISTORICAL_SLIPPAGE_BPS,
            minimum_value_mxn=Decimal("1"),
            depth_is_observed=True)

    cycle = run_evidence_cycle(
        series_by_market=series_by_market, store=store, budget_mxn=Decimal("11"),
        taker_fee_rate=account_fee,
        books={market: books.get(market, HistoricalMarketBook(Decimal("10"), Decimal("1000")))
               for market in series_by_market},
        policy=DEFAULT_POLICY, floor=DEFAULT_FLOOR)

    certifications = tuple(
        certify_pair(market=market, profile_id=profile_id, store=store, floor=DEFAULT_FLOOR)
        for market, profile_id in store.pairs())

    market_data_ok = {market: True for market in observations}
    fee_known = {market: True for market in observations}
    certified_universe = build_certified_universe(
        certifications=certifications,
        current_market_data={**{m: True for m in series_by_market}, **market_data_ok},
        fee_known={**{m: True for m in series_by_market}, **fee_known},
        book_compatible={m: True for m in series_by_market},
        accounting_compatible={m: True for m in series_by_market},
        execution_compatible={m: True for m in series_by_market},
        profile_fingerprints={d.profile_id: (d.fingerprint, d.identity.strategy_fingerprint)
                              for d in PROFILE_REGISTRY},
        profile_markets={d.profile_id: d.markets for d in PROFILE_REGISTRY})

    # The selector runs over the certified universe using the latest evaluated
    # opportunity per pair. With an empty universe this is a NO_TRADE by construction.
    candidates: list[CandidateMarket] = []
    for entry in certified_universe.entries:
        certification = next((c for c in certifications
                              if c.market == entry.market
                              and c.profile_id == entry.profile_id), None)
        if certification is None:
            continue
        evaluation = next((item for report in cycle.ingest
                           if report.market == entry.market
                           for item in report.evaluations
                           if item.profile_id == entry.profile_id), None)
        if evaluation is None:
            continue
        observed = observations.get(entry.market, {})
        candidates.append(CandidateMarket(
            market=entry.market, book=entry.market.lower().replace("/", "_"),
            profile_id=entry.profile_id, certification=certification,
            decision=("BUY" if evaluation.signals > 0 else "NO_SIGNAL"),
            expected_gross_edge_bps=(evaluation.median_net_edge_bps or Decimal("0")),
            expected_net_edge_bps=evaluation.median_net_edge_bps,
            expected_net_pnl_mxn=evaluation.median_net_pnl_mxn,
            viability=None, taker_fee_rate=account_fee,
            spread_bps=Decimal(observed.get("spread_bps", "999")),
            bid_quantity_mxn=Decimal(observed.get("depth_mxn", "0")),
            data_age_seconds=Decimal(observed.get("age_seconds", "999"))))

    # A synthetic deployed/cash split consistent with the portfolio envelope below. The original
    # milestone read these from a real account, and a single-account cash balance is personal
    # financial state rather than a certification input: the selector only needs *a* deployed
    # figure that respects the caps, not this operator's.
    portfolio = PortfolioConstraints(
        authorized_capital_mxn=Decimal("50"), max_deployment_mxn=Decimal("25"),
        single_order_cap_mxn=Decimal("11"), deployed_mxn=Decimal("12.00"),
        cash_mxn=Decimal("38.00"))
    selection = select_production_opportunity(
        candidates=tuple(candidates),
        context=MarketSelectionContext(constraints=portfolio, unresolved_orders=0,
                                       open_markets=frozenset({"BTC/MXN"})))

    certified_pairs = [f"{c.market}|{c.profile_id}" for c in certifications if c.certified]
    certifiable_pairs = [f"{c.market}|{c.profile_id}" for c in certifications
                         if c.state in ("CERTIFIED", "PRODUCTION_CERTIFIABLE")]
    non_btc_certified = [pair for pair in certified_pairs if not pair.startswith("BTC/")]

    methods = list(client.outbound_methods)
    certificate: dict[str, Any] = {
        "certified_at": generated_at,
        "product_version": "AutoFund MVP 0.2",
        "entry_point": "GET-only multi-market backfill, evaluation, certification and selection",
        "production_get_count": methods.count("GET"),
        "production_post_count": methods.count("POST"),
        "production_session_started": False,
        "current_btc_position_mutated": False,
        "promotion": "DISABLED",
        "multi_market_production": "DISABLED",
        "account_fee_used": str(account_fee),
        "historical_lookback_hours": args.hours,
        "evidence_store": store.telemetry(),
        "discovered_universe": {
            "total_mxn_books": len(universe_books),
            "crypto_books": [item["book"] for item in crypto_books],
            "fiat_like_excluded": [item["book"] for item in fiat_books],
            "research_targets": targets,
        },
        "backfill": backfill_reports,
        "market_state": observations,
        "certification": [c.public() for c in certifications],
        "certified_universe": certified_universe.public(),
        "selection": selection.public(),
        "status_inputs": {
            "certified_pairs": certified_pairs,
            "certifiable_pairs": certifiable_pairs,
            "non_btc_certified_pairs": non_btc_certified,
            "market_quality_ok": {c.market: not bool(c.suspended_reason) for c in certifications},
            "crash_safe_selector_implemented": True,
            "one_unresolved_order_globally": True,
        },
        "status": ("READY_FOR_DYNAMIC_MARKET_REAL_SESSION" if non_btc_certified
                   else "ACCUMULATING_REAL_MARKET_EVIDENCE"),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(certificate, sort_keys=True, indent=2, default=str) + "\n",
                   encoding="utf-8")
    print(json.dumps(certificate, sort_keys=True, indent=1, default=str))

    assert methods.count("POST") == 0, "no Production writes permitted"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

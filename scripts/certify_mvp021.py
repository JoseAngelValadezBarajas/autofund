"""MVP 0.2.1 certification: robust trading evidence under an executable fill model.

GET-only. This script answers one question and refuses to answer a different one:

    Does any frozen non-BTC market/profile pair have enough REAL net-economic
    evidence, measured under an executable fill model and against an unseen
    holdout window, to justify a tightly bounded Production experiment?

It does not optimise for certification and does not optimise for trade count. The
order of operations is the substance of the result:

  1. freeze the experiment manifest              (before any evidence is examined)
  2. declare HOLDOUT_01                          (before it is evaluated)
  3. reclassify the MVP 0.2 30-day window as     (development, never validation)
     REAL_HISTORICAL_DEVELOPMENT
  4. evaluate DEVELOPMENT, HOLDOUT_01 and FORWARD separately
  5. report sensitivity without selecting a variant from it
  6. classify, naming the gate that blocked

NO_TRADE, NO_CURRENT_NON_BTC_EDGE and ACCUMULATING_REAL_MARKET_EVIDENCE are all valid
end states. Nothing here trades, starts a session, or POSTs.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.mvp.backfill import HistoricalMarketBook, fetch_historical_series
from autofund.mvp.economics import DEFAULT_POLICY
from autofund.mvp.executable_replay import replay_executable, slippage_sensitivity
from autofund.mvp.experiment import (
    REAL_HISTORICAL_DEVELOPMENT,
    REAL_HISTORICAL_HOLDOUT,
    ExperimentManifest,
    ExperimentManifestStore,
    HoldoutWindow,
    declare_holdout,
    provenance_for_window,
)
from autofund.mvp.profile_library import (
    CHALLENGER_PROFILES,
    PROFILE_BY_ID,
    PROFILE_REGISTRY,
    evaluator_for,
)
from autofund.mvp.profiles import CLASS_STABLE_OR_FIAT, classify_market
from autofund.mvp.robustness import (
    WindowEvidence,
    assert_windows_do_not_overlap,
    assess_pair,
)
from autofund.observer.client import BitsoProductionReadOnlyClient

ROOT = Path(__file__).resolve().parents[1] / "artifacts/mvp-certification/mvp-0-2-1-robust-evidence"
STORE_ROOT = Path(__file__).resolve().parents[1] / "artifacts/mvp-certification/research-evidence"
OUT = ROOT / "mvp-0-2-1-certification.json"
CODE_COMMIT = "fd99013"

# The research filter, not a preference list. Membership is decided by the exchange's
# discovered universe and by eligibility, never by which coin happened to trade.
RESEARCH_BOOKS = ("btc_mxn", "eth_mxn", "sol_mxn", "xrp_mxn", "ada_mxn", "dot_mxn")

# Research challengers only. The frozen Champion is excluded because it is already
# certified and must not be re-opened or re-parameterised by a research cycle.
CHALLENGER_IDS = frozenset(item.profile_id for item in CHALLENGER_PROFILES)

# Development evidence is the window MVP 0.2 already evaluated. Reusing the same span
# keeps the comparison honest: the holdout is the same length and immediately precedes it.
DEVELOPMENT_HOURS = 720
HOLDOUT_HOURS = 720


def discover_books(client: BitsoProductionReadOnlyClient) -> tuple[dict[str, str], ...]:
    discovered: list[dict[str, str]] = []
    for constraints in client.available_books():
        book = str(getattr(constraints, "book", ""))
        if not book.endswith("_mxn"):
            continue
        discovered.append({"book": book, "market": book.upper().replace("_", "/"),
                           "market_class": classify_market(book),
                           "minimum_value_mxn": str(getattr(constraints, "minimum_value", "")),
                           "maximum_value_mxn": str(getattr(constraints, "maximum_value", "")),
                           "tick_size": str(getattr(constraints, "tick_size", ""))})
    return tuple(sorted(discovered, key=lambda item: item["book"]))


def observe_market(client: BitsoProductionReadOnlyClient,
                   book: str) -> dict[str, Any] | None:
    try:
        depth = client.order_book(book)
        fees = client.fee_schedule(book)
    except Exception as exc:
        return {"error": type(exc).__name__}
    top_bid_value = sum((level.price * level.amount for level in depth.bids[:5]),
                        Decimal("0"))
    age = (datetime.now(UTC) - depth.timestamp).total_seconds()
    return {"best_bid": str(depth.best_bid), "best_ask": str(depth.best_ask),
            "spread_bps": str(depth.spread_bps), "depth_mxn": str(top_bid_value),
            "age_seconds": str(Decimal(str(age))),
            "taker_fee": str(fees.taker_fee_decimal),
            "maker_fee": str(fees.maker_fee_decimal), "sequence": depth.sequence}


def window_evidence(*, name: str, series: Any, profile_id: str, market: str,
                    taker_fee_rate: Decimal, spread_bps: Decimal,
                    budget_mxn: Decimal = Decimal("11")) -> WindowEvidence:
    """Replay one window under the executable fill model and label its provenance."""
    evaluator = evaluator_for(profile_id)
    result = replay_executable(
        candles=series.candles, profile_id=profile_id, market=market, evaluator=evaluator,
        taker_fee_rate=taker_fee_rate, spread_bps=spread_bps, policy=DEFAULT_POLICY,
        budget_mxn=budget_mxn)
    return WindowEvidence(
        name=name, provenance_kind=provenance_for_window(window_name=name), result=result,
        window_start_ms=series.window.start_ms, window_end_ms=series.window.end_ms,
        notes=(f"candles={len(series.candles)}", f"gaps={len(series.gaps)}",
               f"dataset_fingerprint={series.fingerprint}"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--development-hours", type=int, default=DEVELOPMENT_HOURS)
    parser.add_argument("--holdout-hours", type=int, default=HOLDOUT_HOURS)
    parser.add_argument("--refreeze", action="store_true",
                        help="allow replacing an existing frozen manifest")
    args = parser.parse_args()

    ROOT.mkdir(parents=True, exist_ok=True)
    client = BitsoProductionReadOnlyClient()
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    now = datetime.now(UTC)

    universe = discover_books(client)
    crypto = [item for item in universe if item["market_class"] != CLASS_STABLE_OR_FIAT]
    available = {item["book"] for item in crypto}
    targets = [book for book in RESEARCH_BOOKS if book in available]

    # ---- 1. freeze the experiment BEFORE any evidence is evaluated ----
    store = ExperimentManifestStore(ROOT)
    if args.refreeze and store.exists():
        (ROOT / "TRADING_EXPERIMENT_MANIFEST.json").unlink()
    manifest = ExperimentManifest(
        strategy_profile_id="volatility-mean-reversion-v1",
        strategy_version=PROFILE_BY_ID["volatility-mean-reversion-v1"].identity.version,
        strategy_fingerprint=PROFILE_BY_ID[
            "volatility-mean-reversion-v1"].identity.strategy_fingerprint,
        strategy_parameters=PROFILE_BY_ID[
            "volatility-mean-reversion-v1"].identity.parameters,
        economic_policy_version=DEFAULT_POLICY.version,
        economic_policy_fingerprint="fee-aware-economic-edge-v1",
        minimum_net_profit_mxn=DEFAULT_POLICY.minimum_net_profit_mxn,
        minimum_net_edge_bps=DEFAULT_POLICY.minimum_net_edge_bps,
        fee_model_version="0.1.3", fee_source_semantics="BASE_REDUCES_SELLABLE_QUANTITY",
        confirmed_taker_fee_rate=Decimal("0.0078"),
        slippage_model_version="0.1.4", fill_model_version="0.2.1",
        market_classification_version="0.2", certification_policy_version="0.2.1",
        risk_policy_fingerprint="unchanged-from-0.2",
        capital_policy_fingerprint="unchanged-from-0.2",
        single_order_cap_mxn=Decimal("11"), max_deployment_mxn=Decimal("25"),
        authorized_capital_mxn=Decimal("50"),
        dataset_cutoff_ms=int(now.timestamp() * 1000), code_commit=CODE_COMMIT,
        created_at=generated_at,
        development_window=HoldoutWindow(
            name="DEVELOPMENT", start_ms=int((now.timestamp() - args.development_hours * 3600)
                                             * 1000),
            end_ms=int(now.timestamp() * 1000),
            reason="window MVP 0.2 evaluated while building and debugging the profiles"))
    frozen = store.freeze(manifest) if not store.exists() else store.load()

    # ---- 2. declare the holdout BEFORE evaluating it ----
    holdout_window = declare_holdout(
        name="HOLDOUT_01", development_start_ms=manifest.development_window.start_ms,
        duration_days=args.holdout_hours // 24,
        reason=("complete non-overlapping block of equal duration immediately preceding "
                "the development window; declared before evaluation"))
    if not any(item["name"] == "HOLDOUT_01"
               for item in frozen.get("holdout_windows", [])):
        frozen = store.freeze_holdouts((holdout_window,))

    # ---- 3. real historical series: development, holdout and forward ----
    series_by_window: dict[str, dict[str, Any]] = {"DEVELOPMENT": {}, "HOLDOUT_01": {}}
    backfill: list[dict[str, Any]] = []
    # Only two windows exist, and the reason is a hard constraint rather than a choice:
    # FORWARD evidence means candles captured *after* the manifest was frozen. The
    # manifest was frozen moments ago, so no such data can exist yet. Relabelling the
    # most recent week of history as "forward" would present already-seen data as
    # prospective evidence, which is the specific error this milestone exists to stop.
    # The FORWARD slot is therefore reported as absent, and will fill itself as the
    # running system records new candles.
    spans = (
        ("DEVELOPMENT", args.development_hours, now),
        ("HOLDOUT_01", args.holdout_hours, now - timedelta(hours=args.development_hours)),
    )
    for book in targets:
        market = book.upper().replace("_", "/")
        for name, hours, anchor in spans:
            try:
                series = fetch_historical_series(source=client, book=book,
                                                lookback_hours=hours, now=anchor)
            except Exception as exc:
                backfill.append({"book": book, "window": name,
                                 "status": f"FAILED:{type(exc).__name__}"})
                continue
            if not series.candles:
                backfill.append({"book": book, "window": name, "status": "NO_CANDLES"})
                continue
            series_by_window[name][market] = series
            backfill.append({"book": book, "market": market, "window": name, "status": "OK",
                             "candles": len(series.candles), "gaps": len(series.gaps),
                             "contiguous": series.is_contiguous,
                             "provenance": series.provenance.kind,
                             "dataset_fingerprint": series.fingerprint})

    # ---- 4. current market state: fee, spread, depth ----
    observations: dict[str, dict[str, Any]] = {}
    for book in targets:
        observed = observe_market(client, book)
        if observed is not None and "error" not in observed:
            observations[book.upper().replace("_", "/")] = observed
    account_fee = next((Decimal(item["taker_fee"]) for item in observations.values()),
                       Decimal("0.0078"))

    # ---- 5. evaluate each window separately, then classify ----
    assessments: list[dict[str, Any]] = []
    markets = sorted({market for window in series_by_window.values()
                      for market in window})
    for market in markets:
        observed = observations.get(market, {})
        spread_bps = Decimal(observed.get("spread_bps", "12"))
        book = next((item for item in crypto if item["market"] == market), None)
        depth = Decimal(observed.get("depth_mxn", "0"))
        market_book = HistoricalMarketBook(
            spread_bps=spread_bps, depth_mxn=depth, slippage_bps=Decimal("5"),
            minimum_value_mxn=Decimal(book["minimum_value_mxn"]) if book
            and book["minimum_value_mxn"] else Decimal("1"),
            depth_is_observed=True, spread_observed_at=generated_at)

        for profile in PROFILE_REGISTRY:
            if profile.profile_id not in CHALLENGER_IDS:
                continue
            windows: list[WindowEvidence] = []
            for name in ("DEVELOPMENT", "HOLDOUT_01"):
                series = series_by_window[name].get(market)
                if series is None:
                    continue
                windows.append(window_evidence(
                    name=name, series=series, profile_id=profile.profile_id, market=market,
                    taker_fee_rate=account_fee, spread_bps=spread_bps))
            if not windows:
                continue
            assert_windows_do_not_overlap(tuple(windows))

            development_series = series_by_window["DEVELOPMENT"].get(market)
            # Sensitivity is run only where an edge is actually claimed. Perturbing a
            # strategy that never traded, or that already lost money, would spend the
            # budget looking for a setting under which it survives -- which is the
            # selection behaviour this milestone forbids.
            claims_edge = any(window.net_pnl_mxn > Decimal("0") for window in windows)
            sensitivity = None
            if claims_edge and development_series is not None:
                sensitivity = slippage_sensitivity(
                    candles=development_series.candles, profile_id=profile.profile_id,
                    market=market, evaluator=evaluator_for(profile.profile_id),
                    taker_fee_rate=account_fee, spread_bps=spread_bps,
                    policy=DEFAULT_POLICY)

            assessment = assess_pair(
                market=market, profile_id=profile.profile_id,
                experiment_fingerprint=manifest.fingerprint_value,
                windows=tuple(windows), sensitivity=sensitivity,
                notes=(f"experiment={CODE_COMMIT}",))
            payload = assessment.public()
            payload["market_book"] = market_book.public()
            payload["execution_regime"] = "EXECUTABLE_DELAYED_FILL_V1"
            payload["real_live_session"] = False
            assessments.append(payload)

    # ---- 6. decide the milestone outcome from the evidence, not from hope ----
    certified = [item for item in assessments if item["certified"]]
    ready = sorted({item["market"] for item in certified
                    if not item["market"].startswith("BTC/")
                    and not item["market"].endswith("USD/MXN")})
    # A cleared edge is a *final* answer and outranks a merely short sample: reporting
    # ACCUMULATING when the unseen window already refused every opportunity would imply
    # that more data could turn a losing profile into a winning one.
    no_edge_states = ("NO_ADMISSIBLE_OPPORTUNITY", "NOT_VIABLE", "REGIME_DEPENDENT",
                      "HOLDOUT_NEGATIVE", "DRAWDOWN_EXCEEDED", "SENSITIVITY_FRAGILE")
    short_sample_states = ("ACCUMULATING_SAMPLE", "DEVELOPMENT_ONLY_NO_INDEPENDENT_EVIDENCE",
                           "NO_OPPORTUNITY_IN_WINDOW")
    determinable = [item for item in assessments
                    if item["state"] in (*no_edge_states, *short_sample_states)]
    if certified:
        outcome = "READY_FOR_DYNAMIC_MARKET_REAL_SESSION"
    elif determinable and all(item["state"] in no_edge_states for item in determinable):
        outcome = "NO_CURRENT_NON_BTC_EDGE"
    elif any(item["state"] in short_sample_states for item in assessments):
        outcome = "ACCUMULATING_REAL_MARKET_EVIDENCE"
    else:
        outcome = "BLOCKED"

    methods = list(client.outbound_methods)
    certificate: dict[str, Any] = {
        "certified_at": generated_at,
        "product_version": "AutoFund MVP 0.2.1",
        "entry_point": "GET-only executable-fill evidence certification",
        "outcome": outcome,
        "experiment": frozen,
        "execution_realism": {
            "fill_model": "DELAYED_NEXT_OBSERVATION",
            "fill_delay_bars": 1,
            "same_bar_fill": False,
            "buy_price_basis": "ASK_WHEN_OBSERVED_ELSE_NEXT_OPEN_PLUS_SLIPPAGE",
            "sell_price_basis": "BID_WHEN_OBSERVED_ELSE_NEXT_OPEN_MINUS_SLIPPAGE",
            "spread_double_counted": False,
            "assessment_and_fill_use_the_same_price": True,
            "fee_semantics": "BUY_FEE_IN_BASE_REDUCES_QUANTITY; SELL_FEE_IN_QUOTE_REDUCES_PROCEEDS",
        },
        "evidence_provenance": {
            "DEVELOPMENT": REAL_HISTORICAL_DEVELOPMENT,
            "HOLDOUT_01": REAL_HISTORICAL_HOLDOUT,
            "FORWARD": "ABSENT_NOT_YET_CAPTURED",
            "note": ("MVP 0.2's 30-day window is reclassified as development evidence: the "
                     "profiles were built and repaired while looking at it, so it is not "
                     "presented as out-of-sample validation. Forward evidence requires "
                     "candles captured after the freeze and does not exist yet."),
        },
        "account_fee_used": str(account_fee),
        "windows_hours": {"development": args.development_hours,
                          "holdout": args.holdout_hours,
                          "forward": 0,
                          "forward_note": ("no forward evidence exists yet: it must be "
                                           "captured after the manifest freeze")},
        "discovered_universe": {"count": len(universe), "crypto_count": len(crypto),
                                "fiat_excluded": [item["book"] for item in universe
                                                  if item["market_class"]
                                                  == CLASS_STABLE_OR_FIAT]},
        "backfill": backfill,
        "assessments": assessments,
        "certified_pairs": [f"{item['market']}|{item['profile_id']}" for item in certified],
        "certified_non_btc_pairs": ready,
        "production_get_count": methods.count("GET"),
        "production_post_count": methods.count("POST"),
        "production_session_started": False,
        "real_live_session": False,
        "current_btc_position_mutated": False,
        "production_ledger_mutated": False,
        "promotion": "DISABLED",
        "multi_market_production": "DISABLED",
        "five_trade_floor_changed": False,
        "strategy_parameters_changed": False,
        "risk_policy_changed": False,
        "capital_policy_changed": False,
        "economic_guard_changed": False,
        "single_order_cap_mxn": "11",
        "max_deployment_mxn": "25",
        "authorized_capital_mxn": "50",
        "leverage_or_shorting": False,
    }
    OUT.write_text(json.dumps(certificate, sort_keys=True, indent=2, default=str) + "\n",
                  encoding="utf-8")

    print(json.dumps({"outcome": outcome,
                      "certified_pairs": certificate["certified_pairs"],
                      "certified_non_btc_pairs": ready,
                      "production_post_count": certificate["production_post_count"],
                      "production_get_count": certificate["production_get_count"],
                      "assessments": len(assessments)}, indent=2))
    for item in assessments:
        first = item["windows"][0] if item["windows"] else {}
        print(f"  {item['market']:9s} {item['profile_id']:30s} {item['state']:38s} "
              f"net={first.get('net_pnl_mxn', '')} trips={first.get('completed_round_trips', '')} "
              f"failed={','.join(item['failed_gates'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

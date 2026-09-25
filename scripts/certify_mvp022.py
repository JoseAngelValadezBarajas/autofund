"""MVP 0.2.2 certification: risk-adjusted strategy challengers on real market data.

GET-only. This script answers one question:

    Does either new versioned challenger have a risk path compatible with AutoFund's
    transaction costs and micro-capital risk policy, on unseen evidence?

The sequence, which is the substance of the result:

  1. fetch DEVELOPMENT and HOLDOUT windows per market
  2. explore the PREDECLARED configuration set on DEVELOPMENT only
  3. select per (market, profile) under the predeclared risk-first rule
  4. evaluate the selected configuration on HOLDOUT **unchanged**
  5. classify with the existing 0.2.1 gates, unchanged
  6. report each market separately

Nothing here trades, starts a session, or POSTs. `NO_CURRENT_EDGE` is a successful
research outcome, and is the outcome this milestone expects.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.mvp.backfill import fetch_historical_series
from autofund.mvp.challenger_research import (
    CONFIG_SETS,
    RESEARCH_VERSION,
    SELECTION_RULE,
    build_evaluator,
    record_from_result,
    select_configuration,
)
from autofund.mvp.economics import DEFAULT_POLICY
from autofund.mvp.executable_replay import replay_executable
from autofund.mvp.experiment import (
    ExperimentManifest,
    ExperimentManifestStore,
    HoldoutWindow,
    declare_holdout,
    provenance_for_window,
)
from autofund.mvp.profile_library import FROZEN_PROFILES, RISK_ADJUSTED_CHALLENGERS
from autofund.mvp.profiles import (
    CLASS_STABLE_OR_FIAT,
    classify_market,
)
from autofund.mvp.robustness import (
    WindowEvidence,
    assert_windows_do_not_overlap,
    assess_pair,
)
from autofund.observer.client import BitsoProductionReadOnlyClient

ROOT = Path(__file__).resolve().parents[1] / "artifacts/mvp-certification/mvp-0-2-2-challengers"
OUT = ROOT / "mvp-0-2-2-certification.json"
CODE_COMMIT = "ebb5cd9"

RESEARCH_BOOKS = ("btc_mxn", "eth_mxn", "sol_mxn", "xrp_mxn", "ada_mxn", "dot_mxn")
DEVELOPMENT_HOURS = 720
HOLDOUT_HOURS = 720
BUDGET_MXN = Decimal("11")
TAKER_FEE_RATE = Decimal("0.0078")


def discover_crypto_books(client: BitsoProductionReadOnlyClient) -> tuple[str, ...]:
    """Dynamically discovered crypto *_mxn books. Fiat-like books are excluded by name."""
    books: list[str] = []
    for constraints in client.available_books():
        book = str(getattr(constraints, "book", ""))
        if not book.endswith("_mxn"):
            continue
        if classify_market(book) == CLASS_STABLE_OR_FIAT:
            continue
        books.append(book)
    return tuple(sorted(books))


def observe_spread(client: BitsoProductionReadOnlyClient, book: str) -> Decimal:
    """Live observed spread for the friction model. GET-only, with a stated fallback."""
    try:
        return client.order_book(book).spread_bps
    except Exception:
        return Decimal("12")


def build_windows(*, dev_hours: int, holdout_hours: int) -> tuple[HoldoutWindow, HoldoutWindow]:
    now = datetime.now(UTC)
    development = HoldoutWindow(
        name="DEVELOPMENT", start_ms=int((now.timestamp() - dev_hours * 3600) * 1000),
        end_ms=int(now.timestamp() * 1000),
        reason="window in which the predeclared configuration set is explored")
    holdout = declare_holdout(
        name="HOLDOUT_01",
        development_start_ms=development.start_ms,
        duration_days=max(1, holdout_hours // 24),
        reason=("complete non-overlapping block of equal duration immediately preceding the "
                "development window; declared before the search and evaluated after it"))
    return development, holdout


def window_evidence(*, name: str, series: Any, profile_id: str, market: str,
                    evaluator: Any, spread_bps: Decimal) -> WindowEvidence:
    result = replay_executable(
        candles=series.candles, profile_id=profile_id, market=market, evaluator=evaluator,
        taker_fee_rate=TAKER_FEE_RATE, spread_bps=spread_bps, policy=DEFAULT_POLICY,
        budget_mxn=BUDGET_MXN)
    return WindowEvidence(
        name=name, provenance_kind=provenance_for_window(window_name=name), result=result,
        window_start_ms=series.window.start_ms, window_end_ms=series.window.end_ms,
        notes=(f"candles={len(series.candles)}", f"gaps={len(series.gaps)}",
               f"dataset_fingerprint={series.fingerprint}"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--development-hours", type=int, default=DEVELOPMENT_HOURS)
    parser.add_argument("--holdout-hours", type=int, default=HOLDOUT_HOURS)
    parser.add_argument("--refreeze", action="store_true")
    args = parser.parse_args()

    ROOT.mkdir(parents=True, exist_ok=True)
    client = BitsoProductionReadOnlyClient()
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    # ---- the research generation is frozen before any evidence is fetched ----
    store = ExperimentManifestStore(ROOT)
    if args.refreeze and store.exists():
        for stale in ROOT.iterdir():
            if stale.is_file():
                stale.unlink()
    development_window, holdout_window = build_windows(
        dev_hours=args.development_hours, holdout_hours=args.holdout_hours)
    challengers = tuple(definition.profile_id for definition in RISK_ADJUSTED_CHALLENGERS)
    manifest = ExperimentManifest(
        strategy_profile_id="risk-adjusted-challenger-set",
        strategy_version="0.2.2",
        strategy_fingerprint="|".join(
            sorted(definition.identity.fingerprint
                   for definition in RISK_ADJUSTED_CHALLENGERS)),
        strategy_parameters=tuple(
            (definition.profile_id, definition.identity.fingerprint)
            for definition in RISK_ADJUSTED_CHALLENGERS),
        economic_policy_version=DEFAULT_POLICY.version,
        economic_policy_fingerprint="fee-aware-economic-edge-v1",
        minimum_net_profit_mxn=DEFAULT_POLICY.minimum_net_profit_mxn,
        minimum_net_edge_bps=DEFAULT_POLICY.minimum_net_edge_bps,
        fee_model_version="0.1.3", fee_source_semantics="BASE_REDUCES_SELLABLE_QUANTITY",
        confirmed_taker_fee_rate=TAKER_FEE_RATE,
        slippage_model_version="0.1.4", fill_model_version="0.2.2",
        market_classification_version="0.2", certification_policy_version="0.2.1",
        risk_policy_fingerprint="unchanged-from-0.2.1",
        capital_policy_fingerprint="unchanged-from-0.2.1",
        single_order_cap_mxn=Decimal("11"), max_deployment_mxn=Decimal("25"),
        authorized_capital_mxn=Decimal("50"),
        dataset_cutoff_ms=int(datetime.now(UTC).timestamp() * 1000), code_commit=CODE_COMMIT,
        created_at=generated_at, development_window=development_window)
    frozen = store.freeze(manifest) if not store.exists() else store.load()
    if not any(item["name"] == "HOLDOUT_01" for item in frozen.get("holdout_windows", [])):
        frozen = store.freeze_holdouts((holdout_window,))

    # ---- real evidence: development and holdout, per market ----
    books = [book for book in RESEARCH_BOOKS if book in set(discover_crypto_books(client))]
    series: dict[str, dict[str, Any]] = {"DEVELOPMENT": {}, "HOLDOUT_01": {}}
    spreads: dict[str, Decimal] = {}
    backfill: list[dict[str, Any]] = []
    for book in books:
        market = book.upper().replace("_", "/")
        spreads[market] = observe_spread(client, book)
        for name, hours, anchor in (
                ("DEVELOPMENT", args.development_hours, datetime.now(UTC)),
                ("HOLDOUT_01", args.holdout_hours,
                 datetime.now(UTC) - timedelta(hours=args.development_hours))):
            try:
                fetched = fetch_historical_series(source=client, book=book,
                                                 lookback_hours=hours, now=anchor)
            except Exception as exc:
                backfill.append({"book": book, "window": name,
                                 "status": f"FAILED:{type(exc).__name__}"})
                continue
            if not fetched.candles:
                backfill.append({"book": book, "window": name, "status": "NO_CANDLES"})
                continue
            series[name][market] = fetched
            backfill.append({"book": book, "market": market, "window": name, "status": "OK",
                             "candles": len(fetched.candles), "gaps": len(fetched.gaps),
                             "dataset_fingerprint": fetched.fingerprint})

    # ---- 1. parameter exploration: DEVELOPMENT only ----
    searches: list[dict[str, Any]] = []
    selections: dict[tuple[str, str], Any] = {}
    for market, development in sorted(series["DEVELOPMENT"].items()):
        for profile_id in challengers:
            records = []
            for config in CONFIG_SETS[profile_id]:
                evaluator = build_evaluator(profile_id, config)
                # The identity of the configuration actually evaluated, so a variant can
                # never be reported under the base profile's fingerprint.
                result = replay_executable(
                    candles=development.candles, profile_id=profile_id, market=market,
                    evaluator=evaluator, taker_fee_rate=TAKER_FEE_RATE,
                    spread_bps=spreads[market], policy=DEFAULT_POLICY,
                    budget_mxn=BUDGET_MXN)
                records.append(record_from_result(profile_id=profile_id, config=config,
                                                  result=result))
            chosen = select_configuration(tuple(records))
            selections[(market, profile_id)] = chosen
            searches.append({
                "market": market, "profile_id": profile_id,
                "configurations_tested": len(records),
                "selection_rule": list(SELECTION_RULE),
                "selected_token": None if chosen is None else chosen.token,
                "records": [record.telemetry() for record in records],
            })

    # ---- 2. selected configuration evaluated on HOLDOUT, unchanged ----
    assessments: list[dict[str, Any]] = []
    for (market, profile_id), chosen in sorted(selections.items()):
        if chosen is None:
            continue
        evaluator = build_evaluator(profile_id, chosen.config)
        windows = []
        for name in ("DEVELOPMENT", "HOLDOUT_01"):
            fetched = series[name].get(market)
            if fetched is None:
                continue
            windows.append(window_evidence(
                name=name, series=fetched, profile_id=profile_id, market=market,
                evaluator=evaluator, spread_bps=spreads[market]))
        if not windows:
            continue
        assert_windows_do_not_overlap(tuple(windows))
        assessment = assess_pair(
            market=market, profile_id=profile_id,
            experiment_fingerprint=chosen.profile_fingerprint, windows=tuple(windows),
            notes=(f"config={chosen.token}", f"selected_from={len(CONFIG_SETS[profile_id])}",
                   f"selection_rule={'|'.join(SELECTION_RULE)}",
                   "holdout_evaluated_after_selection=True"))
        payload = assessment.public()
        payload["selected_config"] = dict(chosen.config)
        payload["selected_token"] = chosen.token
        payload["development_selection"] = chosen.telemetry()
        payload["risk_adjusted_summary"] = (
            windows[-1].result.risk_adjusted_summary.telemetry())
        assessments.append(payload)

    # ---- 3. outcome, derived from the evidence ----
    eligible = [item for item in assessments if item["certified"]]
    if eligible:
        outcome = "FORWARD_SHADOW_CANDIDATE_FOUND"
    elif not assessments:
        outcome = "INSUFFICIENT_EVIDENCE"
    else:
        # Distinguish "no edge" from "not enough evidence" honestly: a configuration that
        # lost money or breached its risk boundary on unseen data has no edge, whatever
        # more data would add.
        terminal = {"NOT_VIABLE", "HOLDOUT_NEGATIVE", "DRAWDOWN_EXCEEDED",
                    "REGIME_DEPENDENT", "SENSITIVITY_FRAGILE", "NO_ADMISSIBLE_OPPORTUNITY"}
        outcome = ("NO_CURRENT_EDGE"
                   if all(item["state"] in terminal for item in assessments)
                   else "INSUFFICIENT_EVIDENCE")

    methods = list(client.outbound_methods)
    certificate: dict[str, Any] = {
        "certified_at": generated_at,
        "product_version": "AutoFund MVP 0.2.2",
        "entry_point": "GET-only risk-adjusted challenger research",
        "outcome": outcome,
        "research_version": RESEARCH_VERSION,
        "experiment": frozen,
        "challengers": {definition.profile_id: definition.public()
                        for definition in RISK_ADJUSTED_CHALLENGERS},
        "frozen_previous_profiles": {definition.profile_id: definition.public()
                                     for definition in FROZEN_PROFILES},
        "selection_rule": list(SELECTION_RULE),
        "config_sets": {key: list(value) for key, value in CONFIG_SETS.items()},
        "backfill": backfill,
        "spreads_bps": {market: str(value) for market, value in sorted(spreads.items())},
        "searches": searches,
        "assessments": assessments,
        "certified_pairs": [f"{item['market']}|{item['profile_id']}" for item in eligible],
        "forward_shadow_eligible": [f"{item['market']}|{item['profile_id']}"
                                    for item in eligible],
        "production_get_count": methods.count("GET"),
        "production_post_count": methods.count("POST"),
        "production_session_started": False,
        "real_live_session": False,
        "current_btc_position_mutated": False,
        "production_ledger_mutated": False,
        "promotion": "DISABLED",
        "multi_market_production": "DISABLED",
        "drawdown_policy_changed": False,
        "capital_limits_changed": False,
        "economic_guard_changed": False,
        "mae_mfe_implemented": True,
        "previous_profiles_modified": False,
        "single_order_cap_mxn": "11",
        "max_deployment_mxn": "25",
        "authorized_capital_mxn": "50",
        "leverage_or_shorting": False,
        "markets_pooled": False,
    }
    OUT.write_text(json.dumps(certificate, sort_keys=True, indent=2, default=str) + "\n",
                  encoding="utf-8")

    print(json.dumps({"outcome": outcome,
                      "certified_pairs": certificate["certified_pairs"],
                      "forward_shadow_eligible": certificate["forward_shadow_eligible"],
                      "production_post_count": certificate["production_post_count"],
                      "production_get_count": certificate["production_get_count"],
                      "assessments": len(assessments),
                      "configurations_tested": sum(item["configurations_tested"]
                                                   for item in searches)}, indent=2))
    for item in assessments:
        holdout = next((w for w in item["windows"] if w["name"] == "HOLDOUT_01"), None)
        if holdout is None:
            continue
        print(f"  {item['market']:9s} {item['profile_id']:30s} {item['state']:26s} "
              f"trips={holdout['completed_round_trips']:3d} "
              f"net={str(holdout['net_pnl_mxn'])[:10]:>10} "
              f"medMAE={holdout['median_mae_mxn']!s:>10} "
              f"risk_rej={holdout['risk_adjusted_rejects']:4d} "
              f"failed={','.join(item['failed_gates'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

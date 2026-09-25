"""MVP 0.2.3 certification: passive execution economics feasibility.

GET-only. Answers one question and refuses to answer a different one:

    Can a different spot execution style change the economics enough to make an
    otherwise-rejected opportunity feasible?

It does not invent strategies. Every number here comes from replaying the **frozen
profile decisions unchanged** — `mean-reversion-safe-v1`, `trend-continuation-v1`,
`volatility-mean-reversion-v1`, `volatility-mean-reversion-v2`, `range-expansion-v1` —
while varying only the fees each leg would pay. That isolates execution cost from alpha.

The decisive analysis is a **bound**, not a projection. For each pair it also computes
`MAKER_FEE_WITH_FREE_FILL_BOUND`: the same unchanged decisions paying the maker rate on
both legs but filling unconditionally, with no queue and no adverse selection. That
combination is unachievable. It is computed precisely because it is unachievable — it is
the ceiling. If a pair fails there, no passive execution model can save it, and the
binding constraint is not execution cost.

Nothing here places an order, cancels an order, or mutates anything. Cancellation is
simulated locally; there is no cancel capability in the Production allowlist.
"""

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.mvp.backfill import fetch_historical_series
from autofund.mvp.economics import DEFAULT_POLICY
from autofund.mvp.execution_comparison import (
    compare_execution_modes,
    counterfactual_maker_fee_upper_bound,
)
from autofund.mvp.execution_gap import gap_summary
from autofund.mvp.passive_execution import (
    ALL_MODES,
    CANDLE_ONLY_UNCERTAIN,
    CONFIRMING_MAKER_EVIDENCE,
    MAKER_MAKER,
    TAKER_TAKER,
    execution_modes,
    fee_floor_reduction_bps,
    fee_floors,
)
from autofund.mvp.profile_library import PROFILE_REGISTRY, evaluator_for
from autofund.mvp.profiles import CLASS_STABLE_OR_FIAT, classify_market
from autofund.mvp.robustness import MAX_DRAWDOWN_MXN
from autofund.observer.client import (
    BitsoProductionReadOnlyClient,
    ProductionCredentials,
)
from autofund.observer.errors import AuthenticationUnavailable, StrictReadOnlyLimitation

ROOT = Path(__file__).resolve().parents[1] / "artifacts/mvp-certification/mvp-0-2-3-passive"
OUT = ROOT / "mvp-0-2-3-certification.json"
CODE_COMMIT = "f2c28a0"

RESEARCH_BOOKS = ("btc_mxn", "eth_mxn", "sol_mxn", "xrp_mxn", "ada_mxn", "dot_mxn")
HISTORY_HOURS = 720
BUDGET_MXN = Decimal("11")


def production_client() -> BitsoProductionReadOnlyClient:
    """Authenticated GET-only Production client, if the operator has confirmed it."""
    from os import environ

    key = environ.get("AUTOFUND_BITSO_PROD_API_KEY", "")
    secret = environ.get("AUTOFUND_BITSO_PROD_API_SECRET", "")
    confirmed = environ.get("AUTOFUND_BITSO_PROD_READONLY_CONFIRMED") == "true"
    if not (key and secret and confirmed):
        raise AuthenticationUnavailable("Production read-only credentials not confirmed")
    return BitsoProductionReadOnlyClient(
        ProductionCredentials(api_key=key, api_secret=secret, readonly_confirmed=True))


def discover_crypto_books(client: BitsoProductionReadOnlyClient) -> tuple[str, ...]:
    books: list[str] = []
    for constraints in client.available_books():
        book = str(getattr(constraints, "book", ""))
        if book.endswith("_mxn") and classify_market(book) != CLASS_STABLE_OR_FIAT:
            books.append(book)
    return tuple(sorted(books))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=HISTORY_HOURS)
    parser.add_argument("--refreeze", action="store_true")
    args = parser.parse_args()

    ROOT.mkdir(parents=True, exist_ok=True)
    if args.refreeze and OUT.exists():
        OUT.unlink()
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    client = production_client()

    # ---- 1. account-confirmed maker and taker fees, per book ----
    discovered = discover_crypto_books(client)
    targets = [book for book in RESEARCH_BOOKS if book in set(discovered)]
    schedules: dict[str, Any] = {}
    fee_report: list[dict[str, Any]] = []
    for book in targets:
        try:
            schedule = client.account_fee_schedule(book)
        except StrictReadOnlyLimitation as exc:
            fee_report.append({"book": book, "status": f"UNAVAILABLE:{exc}"})
            continue
        schedules[book] = schedule
        fee_report.append({"book": book, "status": "OK", **schedule.public()})

    if not schedules:
        # No confirmed fees means the fee question is unanswerable and so is the milestone.
        payload = {"certified_at": generated_at, "product_version": "AutoFund MVP 0.2.3",
                   "status": "BLOCKED",
                   "reason": "ACCOUNT_FEE_SCHEDULE_UNAVAILABLE",
                   "fees": fee_report,
                   "production_post_count": 0,
                   "production_session_started": False,
                   "current_btc_position_mutated": False}
        OUT.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps({"status": "BLOCKED", "reason": payload["reason"]}, indent=2))
        return 0

    # ---- 2. structural fee floors from those rates ----
    floors_per_book: dict[str, Any] = {}
    for book, schedule in sorted(schedules.items()):
        floors = fee_floors(maker_rate=schedule.maker_rate, taker_rate=schedule.taker_rate)
        floors_per_book[book] = {
            "maker_fee_decimal": str(schedule.maker_rate),
            "taker_fee_decimal": str(schedule.taker_rate),
            "floors": {mode: float(floor.fee_only_round_trip_bps)
                       for mode, floor in floors.items()},
            "reduction_bps_vs_taker_taker": {
                mode: str(value)
                for mode, value in fee_floor_reduction_bps(floors=floors).items()},
        }

    # ---- 3. historical evidence per market ----
    now = datetime.now(UTC)
    series: dict[str, Any] = {}
    backfill: list[dict[str, Any]] = []
    for book in targets:
        try:
            fetched = fetch_historical_series(source=client, book=book,
                                             lookback_hours=args.hours, now=now)
        except Exception as exc:
            backfill.append({"book": book, "status": f"FAILED:{type(exc).__name__}"})
            continue
        series[book.upper().replace("_", "/")] = fetched
        backfill.append({"book": book, "market": fetched.market, "status": "OK",
                         "candles": len(fetched.candles), "gaps": len(fetched.gaps),
                         "dataset_fingerprint": fetched.fingerprint})

    # ---- 4. re-price unchanged decisions under every mode, plus the bound ----
    comparisons: list[dict[str, Any]] = []
    for market, fetched in sorted(series.items()):
        book = market.lower().replace("/", "_")
        schedule = schedules.get(book)
        if schedule is None:
            continue
        spread = Decimal("12")
        for definition in PROFILE_REGISTRY:
            profile_id = definition.profile_id
            results = compare_execution_modes(
                market=market, profile_id=profile_id, candles=fetched.candles,
                evaluator=evaluator_for(profile_id), maker_rate=schedule.maker_rate,
                taker_rate=schedule.taker_rate, spread_bps=spread,
                policy=DEFAULT_POLICY, budget_mxn=BUDGET_MXN, modes=ALL_MODES,
                # Candle-only history cannot support a maker fill, so passive modes are
                # reported as unevaluable rather than optimistically filled.
                maker_fill_supported=False)
            bound = counterfactual_maker_fee_upper_bound(
                market=market, profile_id=profile_id, candles=fetched.candles,
                evaluator=evaluator_for(profile_id), maker_rate=schedule.maker_rate,
                spread_bps=spread, policy=DEFAULT_POLICY, budget_mxn=BUDGET_MXN)
            entry = {
                "market": market, "profile_id": profile_id,
                "profile_fingerprint": definition.identity.fingerprint,
                "maker_fee_decimal": str(schedule.maker_rate),
                "taker_fee_decimal": str(schedule.taker_rate),
                "modes": {mode: result.telemetry()
                          for mode, result in sorted(results.items())},
                "upper_bound": bound.telemetry(),
            }
            comparisons.append(entry)

    # ---- 5. the structural conclusion ----
    # Per-pair verdict against the unachievable ceiling. A pair that loses even when maker
    # fees are combined with unconditional fills cannot be rescued by any execution model,
    # so its obstacle is alpha. That is a much stronger statement than "it lost money",
    # because it does not depend on how well or badly a passive order would have filled.
    verdicts: list[str] = []
    for item in comparisons:
        bound = Decimal(item["upper_bound"]["net_pnl_mxn"])
        trips = int(item["upper_bound"]["round_trips"])
        if trips == 0:
            verdict = "NO_OPPORTUNITY_ANY_MODEL"
        elif bound > 0:
            verdict = "UPPER_BOUND_POSITIVE"
        else:
            verdict = "UPPER_BOUND_NEGATIVE_ALPHA_LIMITED"
        item["upper_bound_verdict"] = verdict
        if verdict != "NO_OPPORTUNITY_ANY_MODEL":
            verdicts.append(verdict)

    positive = verdicts.count("UPPER_BOUND_POSITIVE")
    negative = verdicts.count("UPPER_BOUND_NEGATIVE_ALPHA_LIMITED")
    trading_pairs = positive + negative
    profiles_positive = sorted({item["profile_id"] for item in comparisons
                                if item["upper_bound_verdict"] == "UPPER_BOUND_POSITIVE"})

    if not comparisons:
        status, cause = "BLOCKED", "NO_EVIDENCE"
    elif negative > positive:
        # Most pairs lose even at the ceiling: alpha, not execution cost.
        status = ("INSUFFICIENT_PASSIVE_FILL_EVIDENCE" if positive
                  else "PASSIVE_EXECUTION_NOT_VIABLE")
        cause = "A"
    else:
        # The ceiling is positive for most trading pairs, so friction was load-bearing and
        # the open question is whether a realistic fill preserves it.
        status = "INSUFFICIENT_PASSIVE_FILL_EVIDENCE"
        cause = "B"

    cause_detail = {
        "primary": cause,
        "upper_bound_positive_pairs": positive,
        "upper_bound_negative_pairs": negative,
        "trading_pairs": trading_pairs,
        "profiles_rescued_by_ceiling": profiles_positive,
        "cause_a_note": (f"{negative} of {trading_pairs} pairs lose money even with maker "
                         "fees AND unconditional fills, so no execution model can save them"),
        "cause_b_note": (f"{positive} pair(s) become positive under the ceiling, all of them "
                         f"in {profiles_positive or ['none']}; for these, lower friction "
                         "admits opportunities the taker model refused entirely"),
        "cause_c_undetermined": ("adverse selection and queue position cannot be measured "
                                 "from candle-only history, so whether a realistic fill "
                                 "preserves the surviving edge is not determinable here"),
    }

    methods = list(client.outbound_methods)
    certificate: dict[str, Any] = {
        "certified_at": generated_at,
        "product_version": "AutoFund MVP 0.2.3",
        "entry_point": "GET-only passive execution economics feasibility",
        "status": status,
        "structural_cause": cause,
        "structural_cause_detail": cause_detail,
        "code_commit": CODE_COMMIT,
        "fee_source": "ACCOUNT_CONFIRMED",
        "fees": fee_report,
        "fee_floors": floors_per_book,
        "execution_modes": {mode: definition.public()
                            for mode, definition in execution_modes().items()},
        "passive_fill_evidence": {
            "historical_evidence": CANDLE_ONLY_UNCERTAIN,
            "confirming_evidence_kinds": sorted(CONFIRMING_MAKER_EVIDENCE),
            "maker_fills_determined_from_history": False,
            "reason": ("candle OHLC records a range and not a sequence, so it cannot show "
                       "that a resting order was ahead of the trades that printed at its "
                       "price; a maker fill is therefore not claimable from it"),
        },
        "backfill": backfill,
        "comparisons": comparisons,
        "production_architecture_gap": gap_summary(),
        "upper_bound_positive_pairs": positive,
        "upper_bound_negative_pairs": negative,
        "upper_bound_pairs": trading_pairs,
        "production_get_count": methods.count("GET"),
        "production_post_count": methods.count("POST"),
        "production_cancel_requests": 0,
        "production_replace_requests": 0,
        "production_session_started": False,
        "real_live_session": False,
        "current_btc_position_mutated": False,
        "production_ledger_mutated": False,
        "promotion": "DISABLED",
        "multi_market_production": "DISABLED",
        "drawdown_policy_changed": False,
        "capital_limits_changed": False,
        "economic_guard_changed": False,
        "strategy_decisions_changed": False,
        "risk_adjusted_entry_changed": False,
        "new_production_mutation_capability_added": False,
        "single_order_cap_mxn": "11",
        "max_deployment_mxn": "25",
        "authorized_capital_mxn": "50",
        "max_drawdown_policy_mxn": str(MAX_DRAWDOWN_MXN),
        "markets_pooled": False,
    }
    OUT.write_text(json.dumps(certificate, sort_keys=True, indent=2, default=str) + "\n",
                  encoding="utf-8")

    print(json.dumps({
        "status": status, "structural_cause": cause,
        "pairs": len(comparisons),
        "upper_bound_positive_pairs": positive,
        "upper_bound_negative_pairs": negative,
        "production_post_count": certificate["production_post_count"],
        "production_get_count": certificate["production_get_count"],
    }, indent=2))
    for book, data in sorted(floors_per_book.items()):
        print(f"  {book:9s} maker={data['maker_fee_decimal']} "
              f"taker={data['taker_fee_decimal']} "
              f"floor_tt={data['floors'][TAKER_TAKER]:.2f} "
              f"floor_mm={data['floors'][MAKER_MAKER]:.2f} "
              f"saves={data['reduction_bps_vs_taker_taker'][MAKER_MAKER]} bps")
    for item in comparisons:
        print(f"  {item['market']:9s} {item['profile_id']:30s} "
              f"tt_trips={item['modes'][TAKER_TAKER]['round_trips']:3d} "
              f"tt_net={item['modes'][TAKER_TAKER]['net_pnl_mxn'][:10]:>10} "
              f"bound_trips={item['upper_bound']['round_trips']:3d} "
              f"bound_net={item['upper_bound']['net_pnl_mxn'][:10]:>10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

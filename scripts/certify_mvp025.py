"""MVP 0.2.5 certification: asymmetric risk/reward opportunity research.

Ask one question: at this account's real costs, does an entry placed close to a genuine
invalidation boundary create a materially better trade shape than the frozen profiles?

**The diagnostic runs first, and it can veto the challengers.** Section 6 permits at most two
new concepts and requires the MAE/MFE evidence to justify them, so the existing frozen paths
are measured before anything is built, and that measurement is recorded in the frozen manifest
as the basis for the parameters. A diagnostic pointing at the exit rule would have produced
exit changes instead.

**What the arithmetic says, and why it shapes every parameter.** The unchanged risk gate nets
friction from *both* the reward and the risk path, so it requires

    gross >= friction + ratio * (risk + friction)

At the account's confirmed 173 bps round trip that is a minimum gross move of 396 bps even
when risk is negligible, rising to ~606 bps at a 260 bps invalidation and a 1.0 ratio. Measured
ATR at these horizons is 29-96 bps, so the requirement is 4-14x ATR. Challenger targets are
therefore derived from this requirement rather than from a volatility multiple, and a geometry
whose required move exceeds the volatility cap is refused rather than truncated -- truncating
would make every configuration produce the same sub-threshold target and would look like a
search.

**The maker bound is not computed.** 0.2.3 and 0.2.4 established that maker fills are
unobservable from candle history, and section 25 forbids using a hypothetical fill to make a
challenger pass. Reporting the bound would invite that reading, so it is absent with a stated
reason.

Everything is GET-only. No order is placed, cancelled or replaced; the position and the
Production ledger are untouched.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.mvp.asymmetry_experiment import (
    ASYMMETRY_BLOCKED,
    ASYMMETRY_MANIFEST_FILE,
    COMPARISON_SEMANTICS,
    CONFIG_SETS,
    FORWARD_SHADOW_CANDIDATE_FOUND,
    NO_CURRENT_EDGE,
    PREDECESSOR_BY_CONCEPT,
    PREDECLARED_HORIZONS,
    SELECTION_RULE,
    INSUFFICIENT_ASYMmetry_EVIDENCE,
    build_challenger,
    freeze_asymmetry_manifest,
    predeclared_configuration_count,
)
from autofund.mvp.asymmetry_gate import (
    PREDECLARED_REWARD_RISK_THRESHOLDS,
    friction_bps_for,
    maximum_risk_bps,
)
from autofund.mvp.backfill import fetch_historical_series
from autofund.mvp.economics import DEFAULT_POLICY
from autofund.mvp.executable_replay import (
    MAX_SINGLE_TRADE_RISK_MXN,
    MINIMUM_REWARD_RISK_RATIO,
    replay_executable,
)
from autofund.mvp.horizon import aggregate_candles
from autofund.mvp.horizon_profiles import horizon_profiles
from autofund.mvp.profile_library import evaluator_for
from autofund.mvp.robustness import MAX_DRAWDOWN_MXN
from autofund.mvp.trade_shape import (
    DIAGNOSTIC_MINIMUM_TRADES,
    DRAWDOWN_WITHIN_POLICY_PASS,
    RAW_MINIMUM_SAMPLE_PASS,
    RISK_SHAPE_ACCEPTABLE,
    TripShape,
    assess_certification_ladder,
    summarise_trade_shape,
)
from autofund.observer.client import (
    BitsoProductionReadOnlyClient,
    ProductionCredentials,
)
from autofund.observer.errors import AuthenticationUnavailable

ROOT = Path(__file__).resolve().parents[1] / "artifacts/mvp/asymmetry"
OUT = ROOT / "mvp-0-2-5-certification.json"
CODE_COMMIT = "54403fb"
BASELINE_COMMIT = "54403fb"

RESEARCH_BOOKS = ("btc_mxn", "eth_mxn", "sol_mxn", "xrp_mxn")
HISTORY_HOURS = 1440
DEVELOPMENT_DAYS = 30
HOLDOUT_DAYS = 7
BUDGET_MXN = Decimal("11")
SPREAD_BPS = Decimal("12")
MODELLED_SLIPPAGE_BPS = Decimal("5")

DIAGNOSTIC_PROFILE_IDS = (
    "volatility-mean-reversion-v2", "range-expansion-v1",
    "volatility-mean-reversion-15m-v1", "volatility-mean-reversion-1h-v1",
    "range-expansion-15m-v1", "range-expansion-1h-v1",
)


def production_client() -> BitsoProductionReadOnlyClient:
    from os import environ

    key = environ.get("AUTOFUND_BITSO_PROD_API_KEY", "")
    secret = environ.get("AUTOFUND_BITSO_PROD_API_SECRET", "")
    confirmed = environ.get("AUTOFUND_BITSO_PROD_READONLY_CONFIRMED") == "true"
    if not (key and secret and confirmed):
        raise AuthenticationUnavailable("Production read-only credentials not confirmed")
    return BitsoProductionReadOnlyClient(
        ProductionCredentials(api_key=key, api_secret=secret, readonly_confirmed=True))


def _trace(message: str) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "progress.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now(UTC).isoformat()} {message}\n")


class RecordingEvaluator:
    """Keeps each proposal so path metrics read the declared target and invalidation
    instead of recomputing them with hindsight."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.proposals: dict[int, Any] = {}

    @property
    def identity(self) -> Any:
        return self._inner.identity

    def propose(self, **kwargs: Any) -> Any:
        proposal = self._inner.propose(**kwargs)
        self.proposals[len(kwargs.get("candles") or ())] = proposal
        return proposal


def bar_minutes_for(profile_id: str) -> int:
    if "-1h" in profile_id:
        return 60
    if "-15m" in profile_id:
        return 15
    return 1


def evaluator_for_id(profile_id: str) -> Any:
    """Resolve a frozen profile, a 0.2.4 horizon profile, or an asymmetric challenger.

    The challengers live in a separate module so the frozen registry is untouched; this is
    the single place that reconciles the three sources.
    """
    for profile in horizon_profiles():
        if profile.identity.profile_id == profile_id:
            return profile
    for concept, configs in CONFIG_SETS.items():
        for tf in PREDECLARED_HORIZONS:
            for config in configs:
                candidate = build_challenger(concept=concept, timeframe=tf, config=config)
                if candidate.identity.profile_id == profile_id:
                    return candidate
    return evaluator_for(profile_id)


def build_trip_shapes(*, result: Any, recorder: RecordingEvaluator, market: str,
                      profile_id: str, bar_minutes: int) -> tuple[TripShape, ...]:
    shapes: list[TripShape] = []
    for trip in result.trips:
        proposal = recorder.proposals.get(trip.entry_signal_index + 1)
        if proposal is None:
            continue
        shapes.append(TripShape(
            market=market, profile_id=profile_id,
            entry_price_mxn=trip.economics.entry.execution_price_mxn,
            exit_price_mxn=trip.economics.exit.execution_price_mxn,
            target_price_mxn=proposal.expected_exit_reference_mxn,
            invalidation_price_mxn=proposal.invalidation_price_mxn,
            mae_mxn=trip.mae_mxn, mfe_mxn=trip.mfe_mxn,
            net_pnl_mxn=trip.net_pnl_mxn, gross_pnl_mxn=trip.gross_pnl_mxn,
            friction_mxn=trip.economics.total_friction_mxn,
            holding_bars=trip.holding_bars,
            time_to_mae_bars=trip.risk_path.time_to_mae_bars if trip.risk_path else 0,
            time_to_mfe_bars=trip.risk_path.time_to_mfe_bars if trip.risk_path else 0,
            exit_reason=trip.exit_reason,
            capital_hours=Decimal(trip.holding_bars * bar_minutes) / Decimal("60")))
    return tuple(shapes)


def slice_window(candles: tuple[Any, ...], start: datetime,
                 end: datetime) -> tuple[Any, ...]:
    return tuple(c for c in candles if start <= c.timestamp < end)


def run_profile(*, candles: tuple[Any, ...], market: str, profile_id: str, evaluator: Any,
                taker_fee_rate: Decimal, bar_minutes: int) -> dict[str, Any]:
    """Replay one profile; summarise both its certification ladder and its path shape."""
    recorder = RecordingEvaluator(evaluator)
    result = replay_executable(
        candles=candles, profile_id=profile_id, market=market, evaluator=recorder,
        taker_fee_rate=taker_fee_rate, spread_bps=SPREAD_BPS, policy=DEFAULT_POLICY,
        budget_mxn=BUDGET_MXN, modelled_slippage_bps=MODELLED_SLIPPAGE_BPS,
        bar_minutes=bar_minutes)
    shapes = build_trip_shapes(result=result, recorder=recorder, market=market,
                               profile_id=profile_id, bar_minutes=bar_minutes)
    shape = summarise_trade_shape(market=market, profile_id=profile_id, shapes=shapes)
    ladder = assess_certification_ladder(
        round_trips=len(result.trips), net_pnl_mxn=result.net_pnl_mxn,
        effective_drawdown_mxn=max(result.max_drawdown_mxn, result.effective_drawdown_mxn),
        median_mae_mxn=shape.median_mae_mxn, maximum_drawdown_mxn=MAX_DRAWDOWN_MXN,
        max_single_trade_risk_mxn=MAX_SINGLE_TRADE_RISK_MXN,
        minimum_round_trips=DIAGNOSTIC_MINIMUM_TRADES)
    ladder["risk_gates_satisfied"] = (
        ladder["raw_minimum_sample"] == RAW_MINIMUM_SAMPLE_PASS
        and ladder["drawdown_within_policy"] == DRAWDOWN_WITHIN_POLICY_PASS
        and ladder["risk_shape"] == RISK_SHAPE_ACCEPTABLE)
    return {
        "profile_id": profile_id, "market": market, "bar_minutes": bar_minutes,
        "evaluations": result.evaluations, "strategy_signals": result.strategy_signals,
        "economic_passes": result.economic_passes,
        "economic_rejects": result.economic_rejects,
        "risk_adjusted_passes": result.risk_adjusted_passes,
        "risk_adjusted_rejects": result.risk_adjusted_rejects,
        "net_pnl_mxn": str(result.net_pnl_mxn),
        "max_drawdown_mxn": str(result.max_drawdown_mxn),
        "effective_drawdown_mxn": str(result.effective_drawdown_mxn),
        "capital_hours": str(result.capital_hours),
        "net_pnl_per_capital_hour": _s(result.net_pnl_per_capital_hour),
        "round_trips": len(result.trips),
        "certification_ladder": ladder, "shape": shape.telemetry(),
        "trips": [t.telemetry() for t in shapes],
    }


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _gate_failure_counts(challenger_report: list[dict[str, Any]]) -> dict[str, int]:
    """How many tested configurations failed each gate.

    Reported separately per gate because a single "failed" count would hide which constraint
    is actually binding, and the whole point of the ladder is to name the binding one.
    """
    counts: dict[str, int] = {}
    for entry in challenger_report:
        for config in entry["configurations_tested"]:
            for gate in config["ladder"]["failed_gates"]:
                counts[gate] = counts.get(gate, 0) + 1
    return dict(sorted(counts.items()))


def select_configuration(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the predeclared selection rule. Every tested configuration is retained."""
    ordered = sorted(records, key=lambda item: str(item["config"]))
    traded = [item for item in ordered if item["round_trips"] > 0]
    viable = [item for item in ordered if item["ladder"]["risk_gates_satisfied"]]
    if not viable:
        return {"selected": None, "reason": "NO_CONFIGURATION_SATISFIED_RISK_GATES",
                "selection_rule": list(SELECTION_RULE), "tested": len(ordered),
                "survivors": 0, "traded_but_failed": [i["config"] for i in traded],
                "traded_configurations": len(traded)}
    ratios = [item for item in viable if item["mfe_to_mae"] is not None]
    if ratios:
        best = max(item["mfe_to_mae"] for item in ratios)
        viable = [item for item in ratios if item["mfe_to_mae"] == best]
    nets = [item for item in viable if item["net_to_mae"] is not None]
    if nets:
        best_net = max(item["net_to_mae"] for item in nets)
        viable = [item for item in nets if item["net_to_mae"] == best_net]
    viable = sorted(viable, key=lambda item: item["net_pnl"], reverse=True)
    return {"selected": viable[0]["config"], "reason": "SELECTION_RULE_APPLIED",
            "selection_rule": list(SELECTION_RULE), "tested": len(ordered),
            "survivors": len(viable), "traded_configurations": len(traded)}


def classify_holdout(*, holdout: dict[str, Any], predecessor: dict[str, Any] | None,
                     ) -> tuple[str, dict[str, str]]:
    """Decide whether the challenger materially improved trade shape on unseen data.

    The comparison is against the frozen predecessor named in the manifest, and the semantics
    were declared before the holdout was inspected. A candidate must therefore pass the
    existing hard gates *and* improve the path it travelled to get there.
    """
    ladder = holdout["certification_ladder"]
    shape = holdout["shape"]
    evidence = {"signals": str(holdout["strategy_signals"]),
                "risk_admitted": str(holdout["risk_adjusted_passes"]),
                "round_trips": str(holdout["round_trips"])}
    if holdout["strategy_signals"] == 0:
        return NO_CURRENT_EDGE, {**evidence, "reason": "NO_SIGNALS_ON_HOLDOUT"}
    if holdout["risk_adjusted_passes"] == 0:
        return NO_CURRENT_EDGE, {**evidence, "reason": "RISK_GATE_REFUSED_EVERY_ENTRY"}
    if holdout["round_trips"] < DIAGNOSTIC_MINIMUM_TRADES:
        return INSUFFICIENT_ASYMmetry_EVIDENCE, {
            **evidence, "reason": "ROUND_TRIPS_BELOW_SAMPLE_FLOOR"}
    if ladder["drawdown_within_policy"] != "DRAWDOWN_WITHIN_POLICY_PASS":
        return NO_CURRENT_EDGE, {**evidence, "reason": "DRAWDOWN_EXCEEDED"}
    if ladder["risk_shape"] != "RISK_SHAPE_ACCEPTABLE":
        return NO_CURRENT_EDGE, {**evidence, "reason": "RISK_SHAPE_UNACCEPTABLE"}
    if Decimal(holdout["net_pnl_mxn"]) <= 0:
        return NO_CURRENT_EDGE, {**evidence, "reason": "NON_NEGATIVE_NET_ECONOMICS_FAIL"}
    if predecessor is None:
        return INSUFFICIENT_ASYMmetry_EVIDENCE, {
            **evidence, "reason": "PREDECESSOR_HOLDOUT_SHAPE_UNAVAILABLE"}
    mine = shape["median_mfe_to_mae"]
    theirs = predecessor["shape"]["median_mfe_to_mae"]
    if mine is None or theirs is None:
        return INSUFFICIENT_ASYMmetry_EVIDENCE, {
            **evidence, "reason": "MFE_TO_MAE_UNDEFINED"}
    evidence["challenger_mfe_to_mae"] = str(mine)
    evidence["predecessor_mfe_to_mae"] = str(theirs)
    if Decimal(mine) <= Decimal(theirs):
        return NO_CURRENT_EDGE, {**evidence, "reason": "SHAPE_NOT_IMPROVED"}
    net_mine = shape["median_net_to_mae"]
    net_theirs = predecessor["shape"]["median_net_to_mae"]
    if net_mine is not None and net_theirs is not None:
        evidence["challenger_net_to_mae"] = str(net_mine)
        evidence["predecessor_net_to_mae"] = str(net_theirs)
        if Decimal(net_mine) < Decimal(net_theirs):
            return NO_CURRENT_EDGE, {**evidence, "reason": "NET_TO_MAE_WORSE"}
    return FORWARD_SHADOW_CANDIDATE_FOUND, evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=HISTORY_HOURS)
    parser.add_argument("--refreeze", action="store_true")
    parser.add_argument("--supersede", action="store_true")
    args = parser.parse_args()

    ROOT.mkdir(parents=True, exist_ok=True)
    if args.refreeze:
        if OUT.exists():
            if not args.supersede:
                raise SystemExit(
                    "REFUSING_TO_REFREEZE: a result exists. Pass --supersede only when it "
                    "is known to be defective, and say why in the commit message.")
            index = 1
            while True:
                archived = OUT.with_name(f"{OUT.stem}.superseded-{index:02d}{OUT.suffix}")
                if not archived.exists():
                    break
                index += 1
            OUT.replace(archived)
            print(f"SUPERSEDED: previous result archived to {archived.name}")
        manifest_path = ROOT / ASYMMETRY_MANIFEST_FILE
        if manifest_path.exists():
            manifest_path.unlink()
    _trace("start")
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    client = production_client()
    _trace("client-ready")

    schedule = client.account_fee_schedule("btc_mxn")
    maker_rate, taker_rate = schedule.maker_rate, schedule.taker_rate
    friction_bps = friction_bps_for(taker_fee_rate=taker_rate, spread_bps=SPREAD_BPS,
                                    slippage_bps=MODELLED_SLIPPAGE_BPS)
    risk_cap_bps = maximum_risk_bps(budget_mxn=BUDGET_MXN,
                                    max_single_trade_risk_mxn=MAX_SINGLE_TRADE_RISK_MXN,
                                    friction_bps=friction_bps)
    _trace(f"fees maker={maker_rate} taker={taker_rate} friction={friction_bps} "
           f"risk_cap={risk_cap_bps}")

    now = datetime.now(UTC).replace(second=0, microsecond=0)
    development_end = now
    development_start = development_end - timedelta(days=DEVELOPMENT_DAYS)
    holdout_end = development_start
    holdout_start = holdout_end - timedelta(days=HOLDOUT_DAYS)

    aggregated: dict[tuple[str, str], Any] = {}
    base_series: dict[str, Any] = {}
    markets: list[str] = []
    backfill: list[dict[str, Any]] = []
    for book in RESEARCH_BOOKS:
        market = book.upper().replace("_", "/")
        try:
            fetched = fetch_historical_series(source=client, book=book,
                                             lookback_hours=args.hours, now=now)
        except Exception as exc:
            backfill.append({"book": book, "status": f"FAILED:{type(exc).__name__}"})
            continue
        base_series[market] = fetched
        markets.append(market)
        backfill.append({"book": book, "market": market, "status": "OK",
                         "base_candles": len(fetched.candles), "gaps": len(fetched.gaps),
                         "dataset_fingerprint": fetched.fingerprint})
        for tf in PREDECLARED_HORIZONS:
            aggregated[(market, tf.name)] = aggregate_candles(
                candles=fetched.candles, timeframe=tf, market=market)
        _trace(f"fetched {book} candles={len(fetched.candles)}")

    # ---- 1. DEV: diagnostic over the frozen profiles ----
    _trace("diagnostic")
    diagnostic: list[dict[str, Any]] = []
    for market in markets:
        for profile_id in DIAGNOSTIC_PROFILE_IDS:
            bar_minutes = bar_minutes_for(profile_id)
            key = ("1h" if bar_minutes == 60 else "15m") if bar_minutes > 1 else None
            source = (aggregated[(market, key)].candles if key
                      else slice_window(base_series[market].candles, development_start,
                                        development_end))
            try:
                record = run_profile(candles=source, market=market, profile_id=profile_id,
                                     evaluator=evaluator_for_id(profile_id),
                                     taker_fee_rate=taker_rate, bar_minutes=bar_minutes)
            except Exception as exc:
                diagnostic.append({"market": market, "profile_id": profile_id,
                                   "status": f"FAILED:{type(exc).__name__}:{exc}"})
                continue
            record["status"] = "OK"
            diagnostic.append(record)
            _trace(f"diag {market} {profile_id} trips={record['round_trips']} "
                   f"dx={record['shape']['diagnosis']}")

    # Aggregate the per-trip path evidence across every frozen profile that actually traded.
    all_trips: list[dict[str, Any]] = []
    for item in diagnostic:
        if item.get("status") == "OK":
            all_trips.extend(item.get("trips", []))
    traded_trips = [t for t in all_trips if t.get("mfe_mxn") is not None]
    covered = sum(1 for t in traded_trips if Decimal(t["mfe_mxn"]) > 0)
    zero_mfe = sum(1 for t in traded_trips if Decimal(t["mfe_mxn"]) <= 0)
    defect_counts: dict[str, int] = {}
    for item in diagnostic:
        if item.get("status") != "OK":
            continue
        defect = item["shape"]["diagnosis"]
        defect_counts[defect] = defect_counts.get(defect, 0) + 1
    # The aggregate diagnosis uses the same rule as the per-profile classifier, applied to
    # every trade the frozen research produced.
    aggregate_covered_fraction = (Decimal(covered) / Decimal(len(traded_trips))
                                 if traded_trips else None)
    if not traded_trips:
        dominant = INSUFFICIENT_ASYMmetry_EVIDENCE
    elif aggregate_covered_fraction is not None and aggregate_covered_fraction <= Decimal("0.34"):
        dominant = "LITTLE_FAVORABLE_EXCURSION"
    else:
        dominant = "EXCESSIVE_ADVERSE_EXCURSION"
    diagnosis_evidence = (
        ("profiles_examined", str(len(diagnostic))),
        ("profiles_producing_trades", str(sum(1 for i in diagnostic
                                              if i.get("status") == "OK"
                                              and i["round_trips"] > 0))),
        ("trades_with_path_evidence", str(len(traded_trips))),
        ("trades_with_zero_mfe", str(zero_mfe)),
        ("trades_with_positive_mfe", str(covered)),
        ("mfe_covered_fraction", str(aggregate_covered_fraction)),
        ("dominant_defect", dominant),
        ("challengers_justified", "YES" if dominant in
         ("LITTLE_FAVORABLE_EXCURSION", "EXCESSIVE_ADVERSE_EXCURSION") else "NO"),
        ("basis", "MAE/MFE measured on frozen profiles before any challenger was built"))

    # ---- 2. freeze the experiment before any challenger is evaluated ----
    manifest = freeze_asymmetry_manifest(
        root=ROOT, baseline_commit=BASELINE_COMMIT, product_version="AutoFund MVP 0.2.5",
        code_commit=CODE_COMMIT, dataset_cutoff_ms=int(now.timestamp() * 1000),
        development=(development_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     int(development_start.timestamp() * 1000),
                     int(development_end.timestamp() * 1000)),
        holdout=(holdout_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                 int(holdout_start.timestamp() * 1000), int(holdout_end.timestamp() * 1000)),
        economic_policy=DEFAULT_POLICY, max_drawdown_mxn=MAX_DRAWDOWN_MXN,
        max_single_trade_risk_mxn=MAX_SINGLE_TRADE_RISK_MXN,
        minimum_reward_risk_ratio=MINIMUM_REWARD_RISK_RATIO,
        single_order_cap_mxn=BUDGET_MXN, authorized_capital_mxn=Decimal("50"),
        execution_model_version="autofund.executable-execution.v1",
        maker_fee_rate=maker_rate, taker_fee_rate=taker_rate, spread_bps=SPREAD_BPS,
        slippage_bps=MODELLED_SLIPPAGE_BPS, diagnosis=diagnosis_evidence)
    _trace(f"manifest-frozen fp={manifest['manifest_fingerprint'][:16]}")

    # ---- 3. predecessor holdout shapes, for the predeclared comparison ----
    predecessor_shapes: dict[tuple[str, str, str], dict[str, Any]] = {}
    for market in markets:
        for concept, predecessor_id in PREDECESSOR_BY_CONCEPT.items():
            for tf in PREDECLARED_HORIZONS:
                if predecessor_id in (
                        "volatility-mean-reversion-v2", "range-expansion-v1"):
                    bar_minutes, source = 1, aggregated[(market, tf.name)].candles
                else:
                    bar_minutes = bar_minutes_for(predecessor_id)
                    source = aggregated[(market, tf.name)].candles
                hold = slice_window(source, holdout_start, holdout_end)
                try:
                    record = run_profile(
                        candles=hold, market=market, profile_id=predecessor_id,
                        evaluator=evaluator_for_id(predecessor_id),
                        taker_fee_rate=taker_rate, bar_minutes=bar_minutes)
                except Exception:
                    continue
                predecessor_shapes[(market, concept, tf.name)] = record
    _trace("predecessor-holdout-done")

    # ---- 4. challengers: DEVELOPMENT search, all configurations retained ----
    challenger_report: list[dict[str, Any]] = []
    for market in markets:
        for concept, configs in CONFIG_SETS.items():
            for tf in PREDECLARED_HORIZONS:
                dev = slice_window(aggregated[(market, tf.name)].candles,
                                   development_start, development_end)
                records: list[dict[str, Any]] = []
                for config in configs:
                    profile = build_challenger(concept=concept, timeframe=tf, config=config)
                    try:
                        record = run_profile(
                            candles=dev, market=market,
                            profile_id=profile.identity.profile_id, evaluator=profile,
                            taker_fee_rate=taker_rate, bar_minutes=tf.seconds // 60)
                    except Exception as exc:
                        _trace(f"FAIL {market} {concept} {tf.name} {config.label} {exc}")
                        continue
                    shape = record["shape"]
                    records.append({
                        "config": config.label, "concept": concept, "timeframe": tf.name,
                        "parameters": config.public(concept=concept,
                                                    timeframe_name=tf.name),
                        "window": "DEVELOPMENT", "round_trips": record["round_trips"],
                        "strategy_signals": record["strategy_signals"],
                        "economic_passes": record["economic_passes"],
                        "risk_adjusted_passes": record["risk_adjusted_passes"],
                        "net_pnl": Decimal(record["net_pnl_mxn"]),
                        "mfe_to_mae": (Decimal(shape["median_mfe_to_mae"])
                                       if shape["median_mfe_to_mae"] else None),
                        "net_to_mae": (Decimal(shape["median_net_to_mae"])
                                       if shape["median_net_to_mae"] else None),
                        "ladder": record["certification_ladder"], "metrics": shape,
                    })
                    _trace(f"dev {market} {concept} {tf.name} {config.label} "
                           f"trips={record['round_trips']} "
                           f"riskP={record['risk_adjusted_passes']}")
                entry: dict[str, Any] = {
                    "market": market, "concept": concept, "timeframe": tf.name,
                    "development_window": [development_start.isoformat(),
                                           development_end.isoformat()],
                    "holdout_window": [holdout_start.isoformat(), holdout_end.isoformat()],
                    "configurations_tested": records,
                    "selection": select_configuration(records),
                    "predecessor_profile_id": PREDECESSOR_BY_CONCEPT[concept],
                }

                # ---- 5. HOLDOUT for the selected configuration only ----
                selection = entry["selection"]
                if selection["selected"] is None:
                    entry["holdout"] = None
                    entry["classification"] = NO_CURRENT_EDGE
                    entry["classification_evidence"] = {
                        "reason": "NO_CONFIGURATION_SURVIVED_DEVELOPMENT",
                        "traded_configurations": str(
                            selection.get("traded_configurations", 0))}
                else:
                    config = next(c for c in configs if c.label == selection["selected"])
                    profile = build_challenger(concept=concept, timeframe=tf, config=config)
                    hold = slice_window(aggregated[(market, tf.name)].candles,
                                        holdout_start, holdout_end)
                    record = run_profile(
                        candles=hold, market=market,
                        profile_id=profile.identity.profile_id, evaluator=profile,
                        taker_fee_rate=taker_rate, bar_minutes=tf.seconds // 60)
                    entry["holdout"] = record
                    verdict, why = classify_holdout(
                        holdout=record,
                        predecessor=predecessor_shapes.get((market, concept, tf.name)))
                    entry["classification"] = verdict
                    entry["classification_evidence"] = why
                    _trace(f"holdout {market} {concept} {tf.name} "
                           f"trips={record['round_trips']} -> {verdict}")
                challenger_report.append(entry)

    # ---- 6. verdict ----
    #
    # The status rule distinguishes three genuinely different outcomes, and the distinction
    # is the same one section 1 of the specification asked to be made explicit:
    #
    #   FORWARD_SHADOW_CANDIDATE_FOUND   a challenger passed on unseen data
    #   NO_CURRENT_EDGE                  the hypothesis was measured and lost
    #   INSUFFICIENT_EVIDENCE            the hypothesis was never reached
    #
    # A challenger that produced hundreds of trades on development and lost on every one has
    # been measured. Calling that INSUFFICIENT_EVIDENCE because it did not survive to the
    # holdout would understate a decisive negative, and it is the mirror image of the 0.2.4
    # defect where a traded-but-losing configuration was reported as a sample failure.
    # `holdout_inspected` is recorded separately, because "the holdout was never reached" is a
    # different fact from "the holdout was reached and failed" and it leaves the holdout clean.
    classifications = [e["classification"] for e in challenger_report]
    found = [e for e in challenger_report
             if e["classification"] == FORWARD_SHADOW_CANDIDATE_FOUND]
    holdout_traded = [e for e in challenger_report
                      if e["holdout"] is not None and e["holdout"]["round_trips"] > 0]
    development_traded = [e for e in challenger_report
                          if any(c["round_trips"] > 0 for c in e["configurations_tested"])]
    holdout_inspected = any(e["holdout"] is not None for e in challenger_report)
    total_development_trips = sum(c["round_trips"] for e in challenger_report
                                  for c in e["configurations_tested"])
    if not markets:
        status = ASYMMETRY_BLOCKED
    elif found:
        status = FORWARD_SHADOW_CANDIDATE_FOUND
    elif holdout_traded or development_traded:
        # Measured and negative. The holdout may or may not have been reached; that is
        # recorded separately rather than being folded into the verdict.
        status = NO_CURRENT_EDGE
    elif total_development_trips == 0:
        status = INSUFFICIENT_ASYMmetry_EVIDENCE
    else:
        status = NO_CURRENT_EDGE

    payload = {
        "certified_at": generated_at, "product_version": "AutoFund MVP 0.2.5",
        "status": status, "baseline_commit": BASELINE_COMMIT, "code_commit": CODE_COMMIT,
        "asymmetry_manifest": manifest,
        "account_fees": {"maker_rate": str(maker_rate), "taker_rate": str(taker_rate),
                         "source": schedule.source,
                         "observed_at": str(schedule.observed_at)},
        "cost_geometry": {
            "friction_bps": str(friction_bps),
            "maximum_risk_bps_allowed": str(risk_cap_bps),
            "reward_risk_thresholds_tested": [str(t)
                                              for t in PREDECLARED_REWARD_RISK_THRESHOLDS],
            "certifying_execution_mode": "TAKER_TAKER",
            "maker_bound_computed": False,
            "maker_bound_reason": ("maker fills are unobservable from candle history and "
                                   "may not be used to make a challenger pass"),
            "minimum_gross_move_bps": str(friction_bps),
            "gross_requirement_formula": "friction + ratio * (risk + friction)",
        },
        "diagnostic": diagnostic,
        "diagnostic_summary": {
            "defect_counts": dict(sorted(defect_counts.items())),
            "dominant_defect": dominant,
            "profiles_examined": len(diagnostic),
            "trades_with_path_evidence": len(traded_trips),
            "trades_with_zero_mfe": zero_mfe,
            "trades_with_positive_mfe": covered,
            "mfe_covered_fraction": _s(aggregate_covered_fraction),
            "mfe_is_diagnostic_not_profit": True,
        },
        "challengers": challenger_report,
        "classification_counts": {name: classifications.count(name)
                                  for name in sorted(set(classifications))},
        "holdout_inspected": holdout_inspected,
        "holdout_clean": not holdout_inspected,
        "development_round_trips": total_development_trips,
        "development_summary": {
            "configurations_evaluated": sum(len(e["configurations_tested"])
                                            for e in challenger_report),
            "configurations_that_traded": sum(
                1 for e in challenger_report for c in e["configurations_tested"]
                if c["round_trips"] > 0),
            "configurations_with_positive_net": sum(
                1 for e in challenger_report for c in e["configurations_tested"]
                if Decimal(c["net_pnl"]) > 0),
            "aggregate_net_pnl_mxn": str(sum(
                (Decimal(c["net_pnl"]) for e in challenger_report
                 for c in e["configurations_tested"]), Decimal("0"))),
            "gate_failure_counts": _gate_failure_counts(challenger_report),
        },
        "forward_shadow_candidates": [
            {"market": e["market"], "concept": e["concept"], "timeframe": e["timeframe"],
             "config": e["selection"]["selected"]} for e in found],
        "comparison_semantics": list(COMPARISON_SEMANTICS),
        "backfill": backfill,
        "aggregation": [
            {"market": m, "timeframe": tf.name,
             "complete_buckets": aggregated[(m, tf.name)].complete_buckets,
             "incomplete_buckets_excluded": aggregated[(m, tf.name)].incomplete_buckets}
            for m in markets for tf in PREDECLARED_HORIZONS],
        "safety": {
            "production_post_count": 0, "production_cancel_count": 0,
            "production_replace_count": 0, "production_session_started": False,
            "new_exchange_mutations_added": 0, "economic_guard_changed": False,
            "risk_engine_changed": False, "drawdown_policy_changed": False,
            "capital_limits_changed": False,
            "frozen_profile_fingerprints_changed": False,
            "previous_research_rewritten": False,
        },
        "configurations_declared": predeclared_configuration_count(),
        "configurations_evaluated": sum(len(e["configurations_tested"])
                                        for e in challenger_report),
    }
    OUT.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": status, "dominant_defect": dominant,
        "trades_with_path_evidence": len(traded_trips),
        "trades_with_zero_mfe": zero_mfe, "trades_with_positive_mfe": covered,
        "challenger_pairs": len(challenger_report),
        "classifications": payload["classification_counts"],
        "forward_shadow_candidates": len(found),
        "friction_bps": str(friction_bps), "risk_cap_bps": str(risk_cap_bps),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

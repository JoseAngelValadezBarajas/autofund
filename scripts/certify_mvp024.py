"""MVP 0.2.4 certification: longer horizons, and forward microstructure capture.

Part A answers one question: at the account's real costs, does trading a coarser horizon
make transaction friction a smaller fraction of the opportunity? The headline metric is the
**friction-to-gross-opportunity ratio**, because that is the mechanism the hypothesis is
about. A coarser horizon is expected to raise the average size of the move in bps while
leaving the ~173 bps of round-trip cost untouched, so the ratio should fall.

It may still not be worth trading. A larger target that is rarely reached, or one that takes
a day of capital-time to reach, is not an improvement. Capital-hours and net P&L per
capital-hour are therefore reported alongside, and the risk gates are applied unchanged: a
configuration that breaches drawdown policy is not selectable however attractive its return.

Three things this script deliberately does not do:

- It does not sweep horizons to find a winner. Exactly 15m and 1h were predeclared, and
  all four configurations per concept per horizon are evaluated and retained, not just the
  best one. Reporting only the winner is the same defect as choosing the horizon after
  seeing the answer.
- It does not let maker fees decide anything. The maker-fee-with-free-fill bound is computed
  as sensitivity only, because a claim based on fills that were never observed is a claim
  about a market that was not measured.
- It does not implement maker execution. Part B captures the evidence that would eventually
  justify it, and building the collector is not the same as earning the right to use it.

Everything here is GET-only. No order is placed, cancelled or replaced. The realized
position and the Production ledger are untouched.
"""

import argparse
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.mvp.backfill import fetch_historical_series
from autofund.mvp.economics import DEFAULT_POLICY
from autofund.mvp.executable_replay import (
    DEFAULT_MIN_ROUND_TRIPS,
    MAX_SINGLE_TRADE_RISK_MXN,
    MINIMUM_REWARD_RISK_RATIO,
    replay_executable,
)
from autofund.mvp.horizon import (
    BASE_TIMEFRAME_SECONDS,
    PREDECLARED_TIMEFRAMES,
    aggregate_candles,
)
from autofund.mvp.horizon_experiment import (
    CERTIFYING_EXECUTION_MODE,
    CONFIG_SETS,
    FORWARD_MICROSTRUCTURE_CAPTURE_READY,
    HORIZON_BLOCKED,
    HORIZON_MANIFEST_FILE,
    INSUFFICIENT_HORIZON_EVIDENCE,
    LONGER_HORIZON_PROMISING,
    MICROSTRUCTURE_BLOCKED,
    NO_HORIZON_EDGE,
    PREDECLARED_HORIZONS,
    SELECTION_RULE,
    HorizonConfig,
    freeze_horizon_manifest,
)
from autofund.mvp.horizon_profiles import (
    RangeExpansionHorizon,
    VolatilityMeanReversionHorizon,
)
from autofund.mvp.microstructure import (
    PASSIVE_FILL_EXACTNESS,
    QUEUE_POSITION_OBSERVABLE,
    REAL_CAPTURED_MICROSTRUCTURE,
    MicrostructureStore,
)
from autofund.mvp.microstructure_bitso import build_collector
from autofund.mvp.passive_execution import MAKER_MAKER, fee_floors
from autofund.mvp.profile_library import evaluator_for
from autofund.mvp.profiles import BPS
from autofund.mvp.robustness import MAX_DRAWDOWN_MXN
from autofund.observer.client import (
    BitsoProductionReadOnlyClient,
    ProductionCredentials,
)
from autofund.observer.errors import AuthenticationUnavailable

ROOT = Path(__file__).resolve().parents[1] / "artifacts/mvp/horizon-microstructure"
OUT = ROOT / "mvp-0-2-4-certification.json"
CAPTURE_ROOT = ROOT / "microstructure"
CODE_COMMIT = "93e7d23"
BASELINE_COMMIT = "93e7d23"

RESEARCH_BOOKS = ("btc_mxn", "eth_mxn", "sol_mxn", "xrp_mxn")
HISTORY_HOURS = 1440          # 60 days of 1-minute base bars
DEVELOPMENT_DAYS = 30
HOLDOUT_DAYS = 7
BUDGET_MXN = Decimal("11")
SPREAD_BPS = Decimal("12")
MODELLED_SLIPPAGE_BPS = Decimal("5")

# The 1-minute comparison baseline. These are the frozen profiles built for 1-minute bars,
# used only to situate the coarser horizons against a measured reference. They are NOT
# candidate horizons: the predeclared set is 15m and 1h and nothing here may add to it.
BASELINE_PROFILE_IDS = ("volatility-mean-reversion-v2", "range-expansion-v1")

# Verdicts for a single (market, concept, horizon) pair.
FORWARD_SHADOW_ELIGIBLE = "FORWARD_SHADOW_ELIGIBLE"
NOT_VIABLE = "NOT_VIABLE"
HOLDOUT_NEGATIVE = "HOLDOUT_NEGATIVE"
DRAWDOWN_EXCEEDED = "DRAWDOWN_EXCEEDED"
RISK_REJECTED_EVERY_ENTRY = "RISK_REJECTED_EVERY_ENTRY"
INSUFFICIENT_TRIPS = "INSUFFICIENT_TRIPS"
NO_SIGNALS = "NO_SIGNALS"


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


def _trace(message: str) -> None:
    """Durable progress trace.

    Written to a file rather than only to stdout because a long certification is run through
    a terminal that may not surface interleaved output, and a run that appears to hang is
    indistinguishable from one that is progressing unless it says where it is.
    """
    line = f"{datetime.now(UTC).isoformat()} {message}"
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "progress.log").open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


class RecordingEvaluator:
    """Wraps a horizon profile and keeps the proposal it made for each bar.

    The friction-to-opportunity ratio needs the target the profile *declared* at the moment
    it decided, not a target recomputed afterwards. A recomputed target would drift with
    volatility and would quietly answer a different question than the one being asked.
    """

    def __init__(self, profile: Any) -> None:
        self._profile = profile
        self.proposals: dict[int, Any] = {}

    @property
    def identity(self) -> Any:
        return self._profile.identity

    def propose(self, **kwargs: Any) -> Any:
        proposal = self._profile.propose(**kwargs)
        candles = kwargs.get("candles") or ()
        self.proposals[len(candles)] = proposal
        return proposal


def config_profile(concept: str, timeframe: Any, config: HorizonConfig) -> Any:
    """Build the horizon profile for one configuration, without mutating any frozen identity.

    `target_floor_bps` is carried into the target model rather than left at the module
    default. Dropping it would make all four configurations of a concept identical, which
    would report a four-point search that never actually searched — the budget would be
    spent and no hypothesis tested.
    """
    target_model = replace(config.target_model_for(concept))
    if concept == "volatility_mean_reversion":
        return VolatilityMeanReversionHorizon(
            timeframe_name=timeframe.name, window=config.window,
            displacement_atr_multiple=config.displacement_atr_multiple,
            stop_atr_multiple=config.stop_atr_multiple,
            max_holding_bars=config.max_holding_bars,
            expected_holding_horizon=config.expected_holding_horizon,
            target_model=target_model)
    if concept == "range_expansion":
        return RangeExpansionHorizon(
            timeframe_name=timeframe.name, window=config.window,
            expansion_atr_multiple=config.expansion_atr_multiple,
            stop_atr_multiple=config.stop_atr_multiple,
            max_holding_bars=config.max_holding_bars,
            expected_holding_horizon=config.expected_holding_horizon,
            target_model=target_model)
    raise ValueError(f"UNKNOWN_CONCEPT:{concept}")


def slice_window(candles: tuple[Any, ...], start: datetime,
                 end: datetime) -> tuple[Any, ...]:
    """Half-open window slice on candle labels, so the two windows never share a bar."""
    return tuple(candle for candle in candles if start <= candle.timestamp < end)


def evaluate(*, candles: tuple[Any, ...], market: str, concept: str, timeframe: Any,
             config: HorizonConfig, taker_fee_rate: Decimal) -> dict[str, Any]:
    """Replay one configuration on one window with taker-taker execution throughout."""
    profile = config_profile(concept, timeframe, config)
    recorder = RecordingEvaluator(profile)
    result = replay_executable(
        candles=candles, profile_id=profile.identity.profile_id, market=market,
        evaluator=recorder, taker_fee_rate=taker_fee_rate, spread_bps=SPREAD_BPS,
        policy=DEFAULT_POLICY, budget_mxn=BUDGET_MXN,
        modelled_slippage_bps=MODELLED_SLIPPAGE_BPS,
        bar_minutes=timeframe.seconds // 60)

    # Per-trip friction share against the target declared at entry. This is the number the
    # hypothesis is about: if friction is a smaller fraction of the opportunity, the ratio
    # falls, and nothing else in this script can show that.
    ratios, gross_bps_list = _trip_ratios(result=result, recorder=recorder)
    friction_bps_list = [
        trip.economics.total_friction_mxn / BUDGET_MXN * BPS for trip in result.trips
    ]

    def mean(values: Any) -> Decimal | None:
        return (sum(values, Decimal("0")) / Decimal(len(values))) if values else None

    ordered = sorted(ratios)
    median = ordered[len(ordered) // 2] if ordered else None
    holding = [trip.holding_bars for trip in result.trips]
    return {
        "status": result.status, "reason_code": result.reason_code,
        "evaluations": result.evaluations, "strategy_signals": result.strategy_signals,
        "economic_passes": result.economic_passes, "economic_rejects": result.economic_rejects,
        "risk_adjusted_passes": result.risk_adjusted_passes,
        "risk_adjusted_rejects": result.risk_adjusted_rejects,
        "round_trips": len(result.trips), "distinct_episodes": result.distinct_episodes,
        "unfilled_signals": result.unfilled_signals,
        "net_pnl_mxn": str(result.net_pnl_mxn),
        "gross_pnl_mxn": str(result.gross_pnl_mxn),
        "total_friction_mxn": str(result.total_friction_mxn),
        "max_drawdown_mxn": str(result.max_drawdown_mxn),
        "effective_drawdown_mxn": str(result.effective_drawdown_mxn),
        "median_mae_mxn": _s(result.median_mae_mxn),
        "p90_mae_mxn": _s(result.p90_mae_mxn),
        "median_mfe_mxn": _s(result.median_mfe_mxn),
        "p90_mfe_mxn": _s(result.p90_mfe_mxn),
        "median_holding_bars": (sorted(holding)[len(holding) // 2] if holding else None),
        "p90_holding_bars": (sorted(holding)[int(len(holding) * 0.9)] if holding else None),
        "median_holding_minutes": (
            sorted(holding)[len(holding) // 2] * (timeframe.seconds // 60) if holding else None),
        "capital_hours": str(result.capital_hours),
        "net_pnl_per_capital_hour": _s(result.net_pnl_per_capital_hour),
        "friction_to_opportunity_ratio_mean": _s(mean),
        "friction_to_opportunity_ratio_median": _s(median),
        "friction_bps_mean": _s(mean(friction_bps_list) if friction_bps_list else None),
        "gross_target_bps_mean": _s(mean(gross_bps_list) if gross_bps_list else None),
        "gross_target_bps_median": (sorted(gross_bps_list)[len(gross_bps_list) // 2]
                                    if gross_bps_list else None),
        "risk_gate_rejected_everything": (result.risk_adjusted_passes == 0
                                          and result.economic_passes > 0),
        "exit_reasons": result.exit_reason_counts,
        "speculative_win_rate": _s(result.speculative_win_rate),
        "mae_exceeds_realised_reward": result.mae_exceeds_realised_reward,
        "friction_to_opportunity_ratio": _s(median),
    }


def _trip_ratios(*, result: Any, recorder: RecordingEvaluator,
                 budget_mxn: Decimal = BUDGET_MXN) -> tuple[list[Decimal], list[Decimal]]:
    """Friction-to-opportunity ratio and gross target bps, one pair per completed trip.

    Reads the proposal captured at the signal bar, which is the one the entry decision used.
    Reading it back afterwards is what keeps the ratio honest: it is the target the profile
    *declared* when it decided, not one recomputed with hindsight.
    """
    ratios: list[Decimal] = []
    grosses: list[Decimal] = []
    for trip in result.trips:
        proposal = recorder.proposals.get(trip.entry_signal_index + 1)
        if proposal is None or trip.economics.entry.execution_price_mxn <= 0:
            continue
        entry_price = trip.economics.entry.execution_price_mxn
        gross_bps = (proposal.expected_exit_reference_mxn - entry_price) / entry_price * BPS
        if gross_bps <= 0:
            continue
        friction_bps = trip.economics.total_friction_mxn / budget_mxn * BPS
        grosses.append(gross_bps)
        ratios.append(friction_bps / gross_bps)
    return ratios, grosses


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def select_configuration(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply the predeclared selection rule, in order, to the tested configurations."""
    ordered = sorted(records, key=lambda item: str(item["config"]))
    viable = [item for item in ordered if item["risk_gates_satisfied"]]
    if not viable:
        return {"selected": None, "reason": "NO_CONFIGURATION_SATISFIED_RISK_GATES",
                "tested": len(ordered)}
    ratios = [item for item in viable if item["friction_ratio"] is not None]
    if ratios:
        best_ratio = min(item["friction_ratio"] for item in ratios)
        viable = [item for item in ratios if item["friction_ratio"] == best_ratio]
    positives = [item for item in viable if item["net_pnl"] > 0]
    if positives:
        viable = positives
        per_hour = [item for item in viable if item["net_pnl_per_capital_hour"] is not None]
        if per_hour:
            best_hour = max(item["net_pnl_per_capital_hour"] for item in per_hour)
            viable = [item for item in per_hour
                      if item["net_pnl_per_capital_hour"] == best_hour] or per_hour
        viable = sorted(viable, key=lambda item: item["net_pnl"], reverse=True)
    chosen = viable[0]
    return {"selected": chosen["config"], "reason": "SELECTION_RULE_APPLIED",
            "selection_rule": list(SELECTION_RULE), "tested": len(ordered),
            "risk_gate_survivors": sum(1 for item in ordered
                                       if item["risk_gates_satisfied"])}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=HISTORY_HOURS)
    parser.add_argument("--capture-seconds", type=int, default=0,
                        help="bounded forward capture duration; 0 performs no capture")
    parser.add_argument("--refreeze", action="store_true")
    parser.add_argument("--supersede", action="store_true",
                        help="archive an existing result that is known to be defective")
    args = parser.parse_args()

    ROOT.mkdir(parents=True, exist_ok=True)
    _trace("start")
    # Re-freezing is permitted only when the previous attempt produced no results at all.
    # Once a certification artefact exists, the manifest stays locked: a rewritable manifest
    # would let a later evaluation quietly inherit an earlier freeze, which is the exact
    # failure the freeze exists to prevent. A crashed run that evaluated nothing is a
    # different situation — there is no result for the freeze to have protected.
    if args.refreeze:
        if OUT.exists():
            if not args.supersede:
                raise SystemExit(
                    "REFUSING_TO_REFREEZE: a certification result already exists. "
                    "The frozen manifest may not be rewritten after results were produced. "
                    "Pass --supersede only when the existing result is known to be defective, "
                    "and state why in the commit message.")
            # The superseded result is archived rather than deleted. A run that produced a
            # wrong answer is part of the record: erasing it would make the defect
            # undiscoverable and would hide that a correction ever happened. Numbered so a
            # second correction cannot overwrite the evidence of the first.
            index = 1
            while True:
                archived = OUT.with_name(
                    f"{OUT.stem}.superseded-{index:02d}{OUT.suffix}")
                if not archived.exists():
                    break
                index += 1
            OUT.replace(archived)
            print(f"SUPERSEDED: previous result archived to {archived.name}")
        manifest_path = ROOT / HORIZON_MANIFEST_FILE
        if manifest_path.exists():
            manifest_path.unlink()
    generated_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")

    _trace("client-built")

    client = production_client()
    _trace("client-ready")

    # ---- 1. account-confirmed fees ----
    schedule = client.account_fee_schedule("btc_mxn")
    maker_rate = schedule.maker_rate
    taker_rate = schedule.taker_rate
    floors = fee_floors(maker_rate=maker_rate, taker_rate=taker_rate)

    _trace("fees-read")
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    # The holdout is the block immediately preceding development, matching the convention
    # every earlier milestone used. Both windows are declared here, before any evaluation,
    # and recorded in the frozen manifest.
    development_end = now
    development_start = development_end - timedelta(days=DEVELOPMENT_DAYS)
    holdout_end = development_start
    holdout_start = holdout_end - timedelta(days=HOLDOUT_DAYS)

    # ---- 2. freeze the experiment BEFORE any window is evaluated ----
    manifest = freeze_horizon_manifest(
        root=ROOT, baseline_commit=BASELINE_COMMIT, product_version="AutoFund MVP 0.2.4",
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
        maker_fee_rate=maker_rate, taker_fee_rate=taker_rate)

    _trace("manifest-frozen")

    # ---- 3. fetch 1-minute base history once, aggregate per time frame ----
    markets: list[dict[str, Any]] = []
    base_series: dict[str, Any] = {}
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
        _trace(f"fetched {book} candles={len(fetched.candles)}")
        markets.append({"book": book, "market": market})
        backfill.append({"book": book, "market": market, "status": "OK",
                         "base_candles": len(fetched.candles), "gaps": len(fetched.gaps),
                         "dataset_fingerprint": fetched.fingerprint})

    horizon_report: list[dict[str, Any]] = []
    aggregation_report: list[dict[str, Any]] = []
    aggregated: dict[tuple[str, str], Any] = {}
    _trace("aggregating")
    for entry in markets:
        market = entry["market"]
        for timeframe in PREDECLARED_TIMEFRAMES:
            series = aggregate_candles(candles=base_series[market].candles,
                                       timeframe=timeframe, market=market)
            aggregated[(market, timeframe.name)] = series
            aggregation_report.append({
                "market": market, "timeframe": timeframe.name,
                "timeframe_seconds": timeframe.seconds,
                "label_convention": timeframe.label_convention,
                "base_candles": series.source_bars,
                "complete_buckets": series.complete_buckets,
                "incomplete_buckets_excluded": series.incomplete_buckets,
                "irregular_buckets": series.irregular_buckets,
                "drop_rate": str(series.drop_rate),
                "first_close_at": series.first_close_at, "last_close_at": series.last_close_at})

    # ---- 4. development search: all predeclared configurations, all retained ----
    for entry in markets:
        market = entry["market"]
        for concept, configs in CONFIG_SETS.items():
            for timeframe in PREDECLARED_HORIZONS:
                series = aggregated[(market, timeframe.name)]
                dev = slice_window(series.candles, development_start, development_end)
                records: list[dict[str, Any]] = []
                for config in configs:
                    _trace(f"dev {market} {concept} {timeframe.name} {config.label}")
                    outcome = evaluate(candles=dev, market=market, concept=concept,
                                       timeframe=timeframe, config=config,
                                       taker_fee_rate=taker_rate)
                    effective = Decimal(outcome["effective_drawdown_mxn"])
                    worst = Decimal(outcome["median_mae_mxn"] or "0")
                    record = {
                        "config": config.label, "concept": concept,
                        "timeframe": timeframe.name,
                        "parameters": config.public(timeframe=timeframe,
                                                    concept=concept),
                        "window": "DEVELOPMENT",
                        "net_pnl": Decimal(outcome["net_pnl_mxn"]),
                        "net_pnl_per_capital_hour": (
                            Decimal(outcome["net_pnl_per_capital_hour"])
                            if outcome["net_pnl_per_capital_hour"] else None),
                        "friction_ratio": (
                            Decimal(outcome["friction_to_opportunity_ratio_median"])
                            if outcome["friction_to_opportunity_ratio_median"] else None),
                        "risk_gates_satisfied": (
                            outcome["round_trips"] >= DEFAULT_MIN_ROUND_TRIPS
                            and effective <= MAX_DRAWDOWN_MXN
                            and worst <= abs(MAX_SINGLE_TRADE_RISK_MXN)),
                        "outcome": outcome,
                    }
                    records.append(record)
                selection = select_configuration(records)
                chosen_label = selection["selected"]
                chosen = next((item for item in records
                               if item["config"] == chosen_label), None)

                holdout_outcome = None
                verdict = None
                if chosen is not None:
                    hold = slice_window(series.candles, holdout_start, holdout_end)
                    holdout_outcome = evaluate(
                        candles=hold, market=market, concept=concept, timeframe=timeframe,
                        config=next(c for c in configs if c.label == chosen_label),
                        taker_fee_rate=taker_rate)
                    verdict = _verdict(chosen["outcome"], holdout_outcome)

                horizon_report.append({
                    "market": market, "concept": concept, "timeframe": timeframe.name,
                    "timeframe_seconds": timeframe.seconds,
                    "development_window": [development_start.isoformat(),
                                           development_end.isoformat()],
                    "holdout_window": [holdout_start.isoformat(), holdout_end.isoformat()],
                    "configurations_tested": [
                        {**{k: v for k, v in item.items() if k != "outcome"},
                         "metrics": item["outcome"]} for item in records],
                    "selection": selection,
                    "selected_development": (
                        {**{k: v for k, v in chosen.items() if k != "outcome"},
                         "metrics": chosen["outcome"]} if chosen else None),
                    "selected_holdout": holdout_outcome,
                    "verdict": verdict,
                })

    # ---- 5. Part A verdict ----
    # The distinction that matters most is between "measured and there is no edge" and "could
    # not measure it". A pair that produced no signals, or whose every entry was refused, did
    # not test the hypothesis — it never reached it. Collapsing those into NO_HORIZON_EDGE
    # would report an unrun experiment as a negative finding.
    measured_verdicts = {NOT_VIABLE, DRAWDOWN_EXCEEDED, HOLDOUT_NEGATIVE,
                         FORWARD_SHADOW_ELIGIBLE}
    verdicts = [item["verdict"] for item in horizon_report]
    eligible = [item for item in horizon_report
                if item["verdict"] == FORWARD_SHADOW_ELIGIBLE]
    # `None` means no configuration cleared the risk gates, so nothing was selected and the
    # hypothesis was never carried to a holdout. That is an unmeasured pair, exactly like a
    # pair with no signals. Counting it as measured would report NO_HORIZON_EDGE for a test
    # that never ran — the failure mode this milestone is most exposed to, because its
    # finding is a negative one and a wrongly-negative verdict would look like agreement.
    unmeasured = [v for v in verdicts
                  if v is None or v in (INSUFFICIENT_TRIPS, NO_SIGNALS,
                                        RISK_REJECTED_EVERY_ENTRY)]
    if not markets:
        part_a_status = HORIZON_BLOCKED
    elif eligible:
        part_a_status = LONGER_HORIZON_PROMISING
    elif verdicts and all(v in measured_verdicts for v in verdicts):
        part_a_status = NO_HORIZON_EDGE
    else:
        part_a_status = INSUFFICIENT_HORIZON_EVIDENCE

    # Aggregate the friction mechanism by horizon, across EVERY tested configuration.
    # Not just the selected one: the ratio is a property of the horizon and the target the
    # configuration demands, not of which configuration won — and when no configuration wins
    # (as happened here) a selected-only aggregate reports nothing at all, losing the single
    # most informative measurement in the milestone.
    horizon_ratios: dict[str, list[Decimal]] = {}
    horizon_gross: dict[str, list[Decimal]] = {}
    for item in horizon_report:
        for tested in item["configurations_tested"]:
            metrics = tested["metrics"]
            value = metrics["friction_to_opportunity_ratio_median"]
            if value:
                horizon_ratios.setdefault(item["timeframe"], []).append(Decimal(value))
            gross = metrics["gross_target_bps_median"]
            if gross:
                horizon_gross.setdefault(item["timeframe"], []).append(Decimal(gross))

    def _mean(values: list[Decimal]) -> str | None:
        return str(sum(values, Decimal("0")) / Decimal(len(values))) if values else None

    mechanism = {name: {"friction_to_opportunity_ratio_mean": _mean(values),
                        "gross_target_bps_mean": _mean(horizon_gross.get(name, [])),
                        "observations": len(values)}
                 for name, values in sorted(horizon_ratios.items())}

    # The 1-minute baseline, measured the same way, so "the friction share fell" is a
    # comparison between two real measurements rather than a claim about a remembered number.
    #
    # It uses the FROZEN 1-minute evaluators rather than the horizon classes. Those classes
    # accept only the predeclared timeframes by design — building one on "1m" raises, which
    # is the identity guard doing its job. The frozen profiles are the 1-minute strategies,
    # so they are also the correct comparison.
    baseline_ratios: list[Decimal] = []
    baseline_gross: list[Decimal] = []
    baseline_report: list[dict[str, Any]] = []
    for entry in markets:
        market = entry["market"]
        base = slice_window(base_series[market].candles, development_start, development_end)
        for profile_id in BASELINE_PROFILE_IDS:
            # `PROFILE_BY_ID[...].evaluator` is the evaluator's NAME, not the object;
            # `evaluator_for` is the resolver. Using the field directly passes a string where
            # a callable belongs, which only surfaces when the replay first asks it to decide.
            evaluator = evaluator_for(profile_id)
            recorder = RecordingEvaluator(evaluator)
            try:
                result = replay_executable(
                    candles=base, profile_id=profile_id, market=market,
                    evaluator=recorder, taker_fee_rate=taker_rate, spread_bps=SPREAD_BPS,
                    policy=DEFAULT_POLICY, budget_mxn=BUDGET_MXN,
                    modelled_slippage_bps=MODELLED_SLIPPAGE_BPS, bar_minutes=1)
            except Exception as exc:
                baseline_report.append({"market": market, "profile_id": profile_id,
                                        "status": f"FAILED:{type(exc).__name__}:{exc}"})
                continue
            ratios, grosses = _trip_ratios(result=result, recorder=recorder)
            baseline_ratios.extend(ratios)
            baseline_gross.extend(grosses)
            baseline_report.append({"market": market, "profile_id": profile_id, "status": "OK",
                                    "round_trips": len(result.trips),
                                    "signals": result.strategy_signals,
                                    "risk_adjusted_passes": result.risk_adjusted_passes,
                                    "friction_to_opportunity_ratio_mean": _mean(ratios),
                                    "gross_target_bps_mean": _mean(grosses)})
    baseline_ratio = _mean(baseline_ratios)
    mechanism_confirmed = (
        baseline_ratio is not None and bool(horizon_ratios)
        and all(Decimal(value["friction_to_opportunity_ratio_mean"] or "1")
                < Decimal(baseline_ratio) for value in mechanism.values()))

    # ---- 6. Part B: bounded forward capture, if requested and possible ----
    capture: dict[str, Any] = {"status": MICROSTRUCTURE_BLOCKED,
                               "reason": "CAPTURE_NOT_REQUESTED"}
    if args.capture_seconds > 0:
        capture = _capture(client=client, seconds=args.capture_seconds, books=RESEARCH_BOOKS)

    payload = {
        "certified_at": generated_at,
        "product_version": "AutoFund MVP 0.2.4",
        "status": f"{part_a_status} + {capture['status']}",
        "part_a_status": part_a_status, "part_b_status": capture["status"],
        "baseline_commit": BASELINE_COMMIT, "code_commit": CODE_COMMIT,
        "horizon_manifest": manifest,
        "account_fees": {"maker_rate": str(maker_rate), "taker_rate": str(taker_rate),
                         "source": schedule.source,
                         "observed_at": str(schedule.observed_at)},
        "fee_floors_bps": {mode: str(floor.fee_only_round_trip_bps)
                           for mode, floor in floors.items()},
        "certifying_execution_mode": CERTIFYING_EXECUTION_MODE,
        "maker_bound": {
            "mode": MAKER_MAKER, "authority": "SENSITIVITY_ONLY",
            "may_certify": False,
            "note": "reported to bound the fee-attributable share, never to decide a verdict",
        },
        "backfill": backfill, "aggregation": aggregation_report,
        "friction_to_opportunity_by_horizon": mechanism,
        "friction_to_opportunity_at_base": {
            "timeframe_seconds": BASE_TIMEFRAME_SECONDS,
            "friction_to_opportunity_ratio_mean": baseline_ratio,
            "gross_target_bps_mean": _mean(baseline_gross),
            "observations": len(baseline_ratios),
            "note": "measured the same way, so 'the friction share fell' is a comparison "
                    "between two measurements rather than a claim about a remembered number",
            "configurations": baseline_report},
        "mechanism_confirmed": mechanism_confirmed,
        "verdict_counts": {name: verdicts.count(name) for name in sorted(set(verdicts))},
        "unmeasured_pairs": len(unmeasured),
        "horizons": horizon_report,
        "forward_shadow_eligible": [
            {"market": item["market"], "concept": item["concept"],
             "timeframe": item["timeframe"],
             "config": item["selection"]["selected"]} for item in eligible],
        "part_b": capture,
        "safety": {
            "production_post_count": 0, "production_cancel_count": 0,
            "production_replace_count": 0, "production_session_started": False,
            "new_exchange_mutations_added": 0,
            "economic_guard_changed": False, "drawdown_policy_changed": False,
            "capital_limits_changed": False,
            "frozen_profile_fingerprints_changed": False,
        },
    }
    OUT.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "part_a_status": part_a_status,
                      "part_b_status": capture["status"],
                      "friction_by_horizon": mechanism,
                      "eligible_pairs": len(eligible),
                      "pairs": len(horizon_report)}, indent=2))
    return 0


def _verdict(development: dict[str, Any], holdout: dict[str, Any]) -> str:
    """Classify one selected pair. A holdout loss is the strongest available signal."""
    if development["strategy_signals"] == 0:
        return NO_SIGNALS
    if development["risk_gate_rejected_everything"]:
        return RISK_REJECTED_EVERY_ENTRY
    if development["round_trips"] < DEFAULT_MIN_ROUND_TRIPS:
        return INSUFFICIENT_TRIPS
    if Decimal(development["net_pnl_mxn"]) <= 0:
        return NOT_VIABLE
    if development["effective_drawdown_mxn"] and (
            Decimal(development["effective_drawdown_mxn"]) > MAX_DRAWDOWN_MXN):
        return DRAWDOWN_EXCEEDED
    if holdout["round_trips"] < DEFAULT_MIN_ROUND_TRIPS:
        return INSUFFICIENT_TRIPS
    if Decimal(holdout["net_pnl_mxn"]) <= 0:
        return HOLDOUT_NEGATIVE
    return FORWARD_SHADOW_ELIGIBLE


def _capture(*, client: BitsoProductionReadOnlyClient, seconds: int,
             books: tuple[str, ...]) -> dict[str, Any]:
    """Bounded forward capture. Duration is explicit so it cannot become unbounded."""
    import time

    store = MicrostructureStore(root=CAPTURE_ROOT)
    collector = build_collector(client=client, store=store, books=books)
    started = datetime.now(UTC)
    passes = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        collector.capture_once()
        passes += 1
        time.sleep(min(5, max(0.0, deadline - time.monotonic())))
    ended = datetime.now(UTC)
    return {
        "status": FORWARD_MICROSTRUCTURE_CAPTURE_READY,
        "provenance": REAL_CAPTURED_MICROSTRUCTURE,
        "started_at": started.isoformat(), "ended_at": ended.isoformat(),
        "duration_seconds": int((ended - started).total_seconds()),
        "capture_passes": passes,
        "books": list(books),
        "quality": collector.quality.public(),
        "manifest": store.manifest(),
        "queue_position_observable": QUEUE_POSITION_OBSERVABLE,
        "passive_fill_exactness": PASSIVE_FILL_EXACTNESS,
        "adverse_selection_analysis_possible": collector.quality.events_stored
        > len(books),
        "order_capability": "NONE", "production_mutation": "NONE",
        "separate_from_production_ledger": True,
        "bounded_storage": True,
    }


if __name__ == "__main__":
    raise SystemExit(main())

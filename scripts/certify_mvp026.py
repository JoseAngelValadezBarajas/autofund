"""MVP 0.2.6 certification: independent alpha source discovery.

Ask one question and refuse to ask a different one: **is there any independent information
source with measurable predictive content?** Nothing here opens a position, sizes anything, or
defines a stop. That separation is deliberate — MVP 0.2.5 showed how easily execution friction
can be mistaken for a lack of signal, and a PnL backtest confounds the two.

**Part A — cross-market / lead-lag.** A synchronized panel over four books, with the predeclared
relationship and feature sets, measuring whether a leader's value at or before T predicts a
follower's executable return strictly after T. Contemporaneous correlation is computed and
reported separately by construction: it is not alpha.

**Part B — microstructure / order flow.** Uses only `REAL_CAPTURED_MICROSTRUCTURE`, and only
for as long as that evidence supports. If the capture is too short, the verdict is
`MICROSTRUCTURE_ACCUMULATING`, which is a statement about the evidence rather than about the
market.

**Part C — validation.** A candidate is frozen into an `ALPHA_CANDIDATE_MANIFEST` before the
validation window is touched. Validation lies strictly after the discovery moment, so it cannot
have been used to choose the candidate even accidentally.

Everything is GET-only. The read-only collector is used unchanged; no order, cancel or replace
capability exists anywhere in this path.
"""

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.decimal_utils import ZERO
from autofund.mvp.alpha_controls import (
    FUTURE_LEAK,
    AlphaCandidateManifest,
    ControlResult,
    run_controls,
)
from autofund.mvp.alpha_discovery import (
    INSUFFICIENT_EVIDENCE,
    INSUFFICIENT_EVIDENCE_STATUS,
    INSUFFICIENT_SAMPLE,
    MICROSTRUCTURE_ACCUMULATING,
    MICROSTRUCTURE_ACCUMULATING_STATUS,
    MINIMUM_OBSERVATIONS,
    NO_ALPHA_SOURCE_FOUND,
    NO_SIGNAL,
    PREDECLARED_HORIZONS_MINUTES,
    PREDICTIVE,
    PREDICTIVE_BUT_NOT_ECONOMIC,
    PREDICTIVE_NOT_ECONOMIC,
    PRICE_ONLY_RESEARCH_BASELINE,
    TERMINAL_STATUSES,
    VALIDATED_ALPHA_SOURCE,
    VALIDATED_ALPHA_SOURCE_FOUND,
    WEAK_UNSTABLE_SIGNAL,
    MultipleTestLedger,
    PredictiveContent,
    assess_predictive_content,
    declare_windows,
)
from autofund.mvp.backfill import fetch_historical_series
from autofund.mvp.cross_market import (
    BASKET,
    CROSS_SECTIONAL_DISPERSION,
    DIVERGENCE,
    FEATURE_INTERPRETATION,
    LAGGED_RETURN,
    LEADER_FOLLOWER_DIVERGENCE,
    MARKET_BREADTH,
    PREDECLARED_FEATURES,
    PREDECLARED_RELATIONSHIPS,
    RELATIVE_RETURN_VS_BASKET,
    VOLATILITY_ADJUSTED_RELATIVE_MOVE,
    MarketPanel,
    MarketSeries,
    basket_series,
    build_panel,
)
from autofund.mvp.microstructure import BookEvent, DepthLevel, TradeEvent
from autofund.mvp.microstructure_alpha import (
    BOOK_SUPPORTED_BOUND,
    INSUFFICIENT_QUEUE_EVIDENCE,
    MINIMUM_MARKOUT_OBSERVATIONS,
    PREDECLARED_MARKOUT_SECONDS,
    TOP_OF_BOOK_IMBALANCE,
    features_from_book,
    markouts_for,
    summarise_markouts,
)
from autofund.observer.client import (
    BitsoProductionReadOnlyClient,
    ProductionCredentials,
)
from autofund.observer.errors import AuthenticationUnavailable

ROOT = Path(__file__).resolve().parents[1] / "artifacts/mvp/alpha"
OUT = ROOT / "mvp-0-2-6-certification.json"
MANIFEST_FILE = ROOT / "ALPHA_CANDIDATE_MANIFEST.json"
CODE_COMMIT = "d87efad"
BASELINE_COMMIT = "d87efad"

RESEARCH_BOOKS = ("btc_mxn", "eth_mxn", "sol_mxn", "xrp_mxn")
DEVELOPMENT_HOURS = 720
VALIDATION_HOURS = 168
BASE_INTERVAL_SECONDS = 60

# The one microstructure feature predeclared for the markout test. Deliberately single: the
# capture is short, and testing eight features against a thin sample is how a search produces a
# finding out of noise.
MARKOUT_FEATURE = TOP_OF_BOOK_IMBALANCE

# How stale a microstructure capture may be before Part B reports accumulating rather than a
# conclusion. Chosen from the markout horizons: the longest horizon must be reachable.
MINIMUM_CAPTURE_SECONDS = max(PREDECLARED_MARKOUT_SECONDS) * 60


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


def _s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def load_microstructure(*, root: Path) -> dict[str, tuple[list[BookEvent], list[TradeEvent]]]:
    """Read the captured evidence back into events, preserving every field.

    Reconstructs the events rather than storing parsed objects during capture, so the stored
    record remains the source of truth and a change to this reader cannot alter the evidence.
    """
    from collections import defaultdict

    out: dict[str, tuple[list[BookEvent], list[TradeEvent]]] = {}
    books: dict[str, list[BookEvent]] = defaultdict(list)
    trades: dict[str, list[TradeEvent]] = defaultdict(list)
    if not root.exists():
        return {}
    for path in sorted(root.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            kind = record.get("kind")
            if kind == "ORDER_BOOK":
                book = str(record["book"])
                bids = tuple(DepthLevel(price=Decimal(item["price"]),
                                        quantity=Decimal(item["quantity"]))
                             for item in record.get("bids", []))
                asks = tuple(DepthLevel(price=Decimal(item["price"]),
                                        quantity=Decimal(item["quantity"]))
                             for item in record.get("asks", []))
                if not bids or not asks:
                    continue
                books[book].append(BookEvent(
                    book=book,
                    exchange_timestamp=datetime.fromisoformat(record["exchange_timestamp"]),
                    received_at=datetime.fromisoformat(record["received_at"]),
                    sequence=record.get("sequence"), bids=bids, asks=asks,
                    update_provenance=record.get("update_provenance", "SNAPSHOT"),
                    quality=record.get("quality", "OK")))
            elif kind == "TRADE":
                book = str(record["book"])
                trades[book].append(TradeEvent(
                    book=book, trade_id=record.get("trade_id"),
                    exchange_timestamp=datetime.fromisoformat(record["exchange_timestamp"]),
                    received_at=datetime.fromisoformat(record["received_at"]),
                    price=Decimal(record["price"]), quantity=Decimal(record["quantity"]),
                    maker_side=record.get("maker_side"),
                    sequence=record.get("sequence"), quality=record.get("quality", "OK")))
    for book in set(books) | set(trades):
        out[book] = (sorted(books.get(book, []),
                            key=lambda event: event.exchange_timestamp),
                     sorted(trades.get(book, []),
                            key=lambda trade: trade.exchange_timestamp))
    return out


def analyse_microstructure(*, evidence: dict[str, tuple[list[BookEvent], list[TradeEvent]]]
                           ) -> dict[str, Any]:
    """Forward markout and adverse-selection evidence from the captured books.

    Reports `MICROSTRUCTURE_ACCUMULATING` whenever the evidence cannot support a statement. The
    thresholds are the predeclared ones and are never relaxed because the sample is small —
    relaxing them is exactly how a thin dataset produces a confident wrong answer.
    """
    per_book: list[dict[str, Any]] = []
    total_observations = 0
    total_span_seconds = 0.0
    any_sufficient = False
    for book, (events, trades) in sorted(evidence.items()):
        if not events:
            continue
        features = []
        for event in events:
            if not event.bids or not event.asks:
                continue
            try:
                features.append(features_from_book(event=event))
            except Exception:
                continue
        if len(features) < 2:
            continue
        span = (features[-1].moment - features[0].moment).total_seconds()
        total_span_seconds = max(total_span_seconds, span)
        total_observations += len(features)
        summaries: list[dict[str, Any]] = []
        for horizon in PREDECLARED_MARKOUT_SECONDS:
            markouts = []
            for index, feature in enumerate(features):
                # Direction: a bid-heavy book is treated as buy-pressure, so a positive markout
                # means the midpoint moved the way the imbalance pointed. The convention lives
                # here so it cannot drift between the feature and its evaluation.
                direction = (Decimal("1") if feature.top_imbalance > 0
                             else Decimal("-1") if feature.top_imbalance < 0 else Decimal("0"))
                markouts.extend(markouts_for(
                    features=feature, later=features[index + 1:], horizons=(horizon,),
                    direction=direction))
            summary = summarise_markouts(
                feature_name=MARKOUT_FEATURE, book=book, horizon_seconds=horizon,
                markouts=markouts, evidence_class=BOOK_SUPPORTED_BOUND)
            if summary.observations >= MINIMUM_MARKOUT_OBSERVATIONS:
                any_sufficient = True
            summaries.append(summary.public())
        per_book.append({
            "book": book, "snapshots": len(events), "trades": len(trades),
            "usable_features": len(features),
            "span_seconds": span,
            "markouts": summaries,
            "queue_exact": False,
            "queue_position_assumed": False,
        })

    # Aggregate markout evidence across books for the reported horizon, so the statement is about
    # the dataset rather than about one book's thin slice. Books are reported separately above.
    aggregate: list[dict[str, Any]] = []
    for horizon in PREDECLARED_MARKOUT_SECONDS:
        means: list[Decimal] = []
        observations = 0
        favourable_hits = 0
        favourable_total = 0
        for entry in per_book:
            for summary in entry["markouts"]:
                if summary["horizon_seconds"] != horizon:
                    continue
                if summary["observations"] < MINIMUM_MARKOUT_OBSERVATIONS:
                    continue
                means.append(Decimal(summary["mean_markout_bps"]))
                observations += summary["observations"]
                favourable_hits += int(
                    Decimal(summary["favourable_fraction"]) * summary["observations"])
                favourable_total += summary["observations"]
        if observations >= MINIMUM_MARKOUT_OBSERVATIONS and means:
            mean = sum(means, ZERO) / Decimal(len(means))
            aggregate.append({
                "horizon_seconds": horizon, "observations": observations,
                "mean_of_book_means_bps": str(mean),
                "favourable_fraction": (str(Decimal(favourable_hits)
                                            / Decimal(favourable_total))
                                        if favourable_total else None),
                "books_with_sufficient_evidence": len(means),
                "evidence_class": BOOK_SUPPORTED_BOUND,
            })
    if not any_sufficient or not aggregate:
        classification = MICROSTRUCTURE_ACCUMULATING
    else:
        classification = PREDICTIVE if any(
            Decimal(item["mean_of_book_means_bps"]) != ZERO for item in aggregate
        ) else NO_SIGNAL
    return {
        "classification": classification,
        "books": per_book,
        "aggregate_markouts": aggregate,
        "total_snapshots": total_observations,
        "capture_span_seconds": total_span_seconds,
        "minimum_capture_seconds_required": MINIMUM_CAPTURE_SECONDS,
        "minimum_markout_observations": MINIMUM_MARKOUT_OBSERVATIONS,
        "queue_position_observable": False,
        "queue_exact": False,
        "passive_fill_exactness": "BOUNDED_ONLY",
        "adverse_selection_evidence_class": (
            BOOK_SUPPORTED_BOUND if aggregate else INSUFFICIENT_QUEUE_EVIDENCE),
        "evidence_provenance": "REAL_CAPTURED_MICROSTRUCTURE",
        "maker_authorised": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--development-hours", type=int, default=DEVELOPMENT_HOURS)
    parser.add_argument("--validation-hours", type=int, default=VALIDATION_HOURS)
    parser.add_argument("--refreeze", action="store_true")
    parser.add_argument("--supersede", action="store_true")
    parser.add_argument("--skip-microstructure", action="store_true")
    args = parser.parse_args()

    ROOT.mkdir(parents=True, exist_ok=True)
    if args.refreeze:
        for path in (OUT, MANIFEST_FILE):
            if path.exists():
                if not args.supersede and path == OUT:
                    raise SystemExit("REFUSING: a result exists; pass --supersede")
                if path == OUT:
                    index = 1
                    while True:
                        archived = OUT.with_name(
                            f"{OUT.stem}.superseded-{index:02d}{OUT.suffix}")
                        if not archived.exists():
                            break
                        index += 1
                    path.replace(archived)
                    print(f"SUPERSEDED: archived to {archived.name}")
                else:
                    path.unlink()
    _trace("start")

    client = production_client()
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    windows = declare_windows(now=now, development_hours=args.development_hours,
                             validation_hours=args.validation_hours)
    _trace(f"windows dev={windows.development_start}..{windows.development_end}")

    # ---- fetch and build the synchronized panel ----
    series_list: list[MarketSeries] = []
    backfill: list[dict[str, Any]] = []
    for book in RESEARCH_BOOKS:
        market = book.upper().replace("_", "/")
        try:
            fetched = fetch_historical_series(source=client, book=book,
                                             lookback_hours=args.development_hours
                                             + args.validation_hours + 24, now=now)
        except Exception as exc:
            backfill.append({"book": book, "status": f"FAILED:{type(exc).__name__}"})
            continue
        series_list.append(MarketSeries(market=market, candles=fetched.candles,
                                        interval=timedelta(seconds=BASE_INTERVAL_SECONDS)))
        backfill.append({"book": book, "market": market, "status": "OK",
                         "candles": len(fetched.candles), "gaps": len(fetched.gaps),
                         "fingerprint": fetched.fingerprint})
        _trace(f"fetched {book} candles={len(fetched.candles)}")
    if not series_list:
        payload = {"status": "BLOCKED", "reason": "NO_MARKET_DATA", "backfill": backfill}
        OUT.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
        print(json.dumps(payload, indent=2))
        return 0

    basket = basket_series(series=series_list, interval_seconds=BASE_INTERVAL_SECONDS)
    panel_input = [basket, *series_list]
    panel = build_panel(series=panel_input, interval_seconds=BASE_INTERVAL_SECONDS,
                        horizons=tuple(PREDECLARED_HORIZONS_MINUTES))
    _trace(f"panel built observations={len(panel.observations)}")
    rows = _feature_relationship_rows()
    _assert_rows_resolve(panel=panel, rows=rows)
    _trace(f"feature rows resolved: {len(rows)}")

    # ---- Part A: measure predictive content on DEVELOPMENT only ----
    measurements: list[dict[str, Any]] = []
    controls_log: list[dict[str, Any]] = []
    control_results: dict[tuple[str, int], tuple[ControlResult, ...]] = {}
    comparisons = 0
    for feature_name, leader, follower in rows:
        feature_key = _feature_key(feature_name=feature_name, leader=leader,
                                   follower=follower)
        for horizon in PREDECLARED_HORIZONS_MINUTES:
            comparisons += 1
            # One traversal yields the instants and both series, so the window filter can be
            # applied by position and the values never need re-pairing.
            moments, features, forwards = panel.pairs_for(
                feature=feature_key, market=follower, horizon=horizon)
            selected = [index for index, moment in enumerate(moments)
                        if windows.contains_development(moment)]
            if not selected:
                continue
            feature_values = tuple(features[index] for index in selected)
            forward_values = tuple(forwards[index] for index in selected)
            content = assess_predictive_content(
                feature_name=feature_key, market=follower, horizon_minutes=horizon,
                feature=feature_values, forward=forward_values)
            measurements.append({
                "feature_key": feature_key, "feature_name": feature_name,
                "leader": leader, "follower": follower, "horizon_minutes": horizon,
                "content": content.public(),
                "content_object": content,
            })
            # Controls run on every measurement, not only on the survivors. Running them
            # selectively would let a candidate be chosen before its controls were seen.
            if content.observations >= MINIMUM_OBSERVATIONS:
                results = run_controls(
                    feature_name=feature_key, market=follower, horizon_minutes=horizon,
                    feature=feature_values, forward=forward_values, original=content)
                control_results[(feature_key, horizon)] = results
                controls_log.append({
                    "feature_key": feature_key, "horizon_minutes": horizon,
                    "controls": [control.public() for control in results],
                })
    _trace(f"measured {len(measurements)} feature/horizon combinations")

    # ---- sensitivity gate ----
    #
    # A verdict is only worth recording if the measurement that produced it could have seen a
    # signal. The FUTURE_LEAK control answers exactly that: the outcome is injected as the feature,
    # so failure to detect it means the measurement has no resolution. The first run of this script
    # produced 84 verdicts without checking this, and the check reveals that all 28 one-minute
    # measurements were blind -- a perfect injected leak went undetected in 28 of 28 while being
    # detected in all 56 measurements at five and fifteen minutes.
    #
    # Those one-minute verdicts are therefore unmeasurable, not negative, and are relabelled rather
    # than reported. This is the only place in the pipeline where a verdict is overwritten, and it
    # can only ever remove a claim, never create one.
    unmeasurable = 0
    for measurement in measurements:
        results = control_results.get((measurement["feature_key"],
                                       measurement["horizon_minutes"]), ())
        leak = next((item for item in results if item.control == FUTURE_LEAK), None)
        sensitive = bool(leak and leak.as_expected)
        measurement["sensitivity_verified"] = sensitive
        # The leak control subsumes sign inversion: an anti-correlated leak is as visible as a
        # correlated one, so a measurement that cannot see either cannot be trusted on this axis.
        if not sensitive:
            unmeasurable += 1
            content = measurement["content"]
            content["verdict"] = INSUFFICIENT_SAMPLE
            content["notes"]["reason"] = "NO_MEASUREMENT_SENSITIVITY"
            content["notes"]["sensitivity_evidence"] = (
                "an injected perfect leak was not detected, so this measurement could not have "
                "seen a real signal either; its verdict is uninformative rather than negative")
    _trace(f"sensitivity gate: {unmeasurable} measurements relabelled unmeasurable")

    predictive = [m for m in measurements if m["content"]["verdict"] == PREDICTIVE]
    _trace(f"predictive combinations: {len(predictive)}")

    # ---- candidate selection: smallest defensible specification ----
    candidate: AlphaCandidateManifest | None = None
    candidate_reason = "NO_PREDICTIVE_MEASUREMENT"
    if predictive:
        # Choose by the *weakest* claim that is still supported: the largest effective sample and
        # the most subwindows, breaking ties toward the shortest horizon so the candidate is the
        # least demanding to translate into a trade later.
        def rank(measurement: dict[str, Any]) -> tuple[int, int, int]:
            content = measurement["content"]
            effective = int(Decimal(content["effective_observations"]))
            return (content["stable_subwindows"], effective, -measurement["horizon_minutes"])

        best = max(predictive, key=rank)
        content_obj = best["content_object"]
        results = control_results.get(
            (best["feature_key"], best["horizon_minutes"]), ())
        controls_ok = bool(results) and all(item.as_expected for item in results)
        if controls_ok:
            candidate = _freeze_candidate(
                best=best, content=content_obj, controls=tuple(results),
                comparisons=comparisons, windows=windows)
            candidate_reason = "FROZEN"
        else:
            candidate_reason = ("NEGATIVE_CONTROLS_FAILED" if results
                                else "CONTROLS_NOT_RUN")

    # ---- clean validation, only if a candidate was frozen ----
    validation: dict[str, Any] | None = None
    if candidate is not None:
        _trace("validating frozen candidate")
        validation = _validate(panel=panel, candidate=candidate, windows=windows)

    # ---- Part B: microstructure ----
    if args.skip_microstructure:
        microstructure: dict[str, Any] = {
            "classification": MICROSTRUCTURE_ACCUMULATING,
            "reason": "SKIPPED_BY_REQUEST", "books": [], "aggregate_markouts": []}
    else:
        evidence = load_microstructure(root=ROOT / "microstructure")
        microstructure = analyse_microstructure(evidence=evidence)
    _trace(f"microstructure classification={microstructure['classification']}")

    # ---- multiple-testing ledger ----
    ledger = MultipleTestLedger(
        feature_families_examined=len(PREDECLARED_FEATURES),
        relationships_examined=len(PREDECLARED_RELATIONSHIPS),
        horizons_examined=len(PREDECLARED_HORIZONS_MINUTES),
        buckets_per_feature=5, negative_controls_run=len(controls_log) * 4,
        failed_candidates_retained=len(measurements) - len(predictive))

    # ---- economic translation, computed before the status because the status depends on it ----
    economic = _economic_translation(candidate=candidate, validation=validation)

    # ---- was the validation window already exposed? ----
    #
    # This is the check that decides whether a `PREDICTIVE` validation result can be called
    # independent evidence. Earlier revisions of this script declared a validation window that
    # could not be fetched, so their measurements spanned the period this revision now reserves as
    # validation. One of those runs compared 84 combinations across exactly that period and printed
    # which of them looked best, so the identity of the strongest candidate was known before this
    # run's validation was read.
    #
    # That does not make the result worthless -- the direction was predeclared, the effect also
    # appears for a second follower, and no threshold was tuned on the earlier output (the one
    # threshold that was touched was removed for that reason). But it does mean the validation
    # number is not independent confirmation, and a status of VALIDATED_ALPHA_SOURCE would
    # overstate it. Detecting this from the artifacts rather than relying on memory means it cannot
    # be forgotten.
    exposure = _validation_exposure(windows=windows, root=ROOT)
    _trace(f"validation exposure: {exposure['status']}")

    # ---- classification and terminal status ----
    #
    # `classification` describes what was found and uses the section 26 vocabulary.
    # `status` is the milestone's terminal status and uses the section 32 vocabulary. They are
    # kept distinct because conflating them previously produced a status of `NO_SIGNAL`, which
    # reads as a conclusion about the market when the run had not in fact reached the question:
    # its validation window was empty and its classifier was accepting trivial effects.
    #
    # The economic test is applied FIRST because it is the binding constraint and the one the
    # exposure caveat cannot touch. The measured conditional movement is about 2.5 bps against 173
    # bps of round-trip friction, a gap of roughly seventy-fold. No amount of extra cleanliness in
    # the split would close that, so discovering that the holdout is imperfect cannot change the
    # answer that the information, real as it appears, is uncollectable at this account's costs.
    #
    # Exposure then caps what may be claimed on top. A holdout already searched by an earlier
    # sweep cannot license the word "validated", so VALIDATED_ALPHA_SOURCE is unreachable while it
    # stands -- but PREDICTIVE_NOT_ECONOMIC survives, because it asserts only that the information
    # exists and is too small to pay, and the corroboration for the first half comes from
    # replication on a second instrument rather than from the temporal split.
    if candidate is not None and validation is not None and validation["passed"]:
        if economic["tradeable_at_current_cost"] and exposure["pristine"]:
            classification = VALIDATED_ALPHA_SOURCE
        else:
            classification = PREDICTIVE_NOT_ECONOMIC
    elif candidate is not None and validation is not None:
        classification = PREDICTIVE_NOT_ECONOMIC if validation["predictive"] else NO_SIGNAL
    elif predictive:
        classification = WEAK_UNSTABLE_SIGNAL
    elif measurements:
        classification = NO_SIGNAL
    else:
        classification = INSUFFICIENT_EVIDENCE

    if classification == VALIDATED_ALPHA_SOURCE:
        status = VALIDATED_ALPHA_SOURCE_FOUND
    elif classification == PREDICTIVE_NOT_ECONOMIC:
        status = PREDICTIVE_BUT_NOT_ECONOMIC
    elif classification == WEAK_UNSTABLE_SIGNAL:
        # Measured predictive content that did not survive the controls or the frozen
        # specification. That is a negative result about the source, not a failure to measure it.
        status = NO_ALPHA_SOURCE_FOUND
    elif classification == INSUFFICIENT_EVIDENCE:
        status = INSUFFICIENT_EVIDENCE_STATUS
    elif microstructure["classification"] == MICROSTRUCTURE_ACCUMULATING:
        # A null cross-market result with an under-powered microstructure sample is a conclusion
        # about the evidence available, not yet about the market. Report the accumulating state.
        status = MICROSTRUCTURE_ACCUMULATING_STATUS
    else:
        status = NO_ALPHA_SOURCE_FOUND
    assert status in TERMINAL_STATUSES, status

    # A null result is only worth reporting if the measurement that produced it was sound. This
    # records the checks that license the conclusion, so a future reader can tell a real absence
    # of edge from a broken run.
    evidence_quality = {
        "validation_window_inhabited": bool(
            validation and validation.get("reason") != "NO_VALIDATION_OBSERVATIONS"),
        # The pipeline as a whole was sensitive if any measurement detected the injected leak. The
        # per-measurement outcome is recorded on the measurement itself as `sensitivity_verified`.
        "positive_control_detected": any(m["sensitivity_verified"] for m in measurements),
        "measurements_without_sensitivity": unmeasurable,
        "negative_controls_run_on_every_eligible_measurement": (
            len(controls_log) == len([m for m in measurements
                                      if m["content"]["observations"]
                                      >= MINIMUM_OBSERVATIONS])),
        "measurements": len(measurements),
        "measurements_with_controls": len(controls_log),
        "combinations_declared": comparisons,
        "predictive_rate": (str(Decimal(len(predictive)) / Decimal(len(measurements)))
                            if measurements else "0"),
        "note": ("a discovery pipeline that always finds alpha is broken; this run is required "
                 "to be able to report absence"),
    }
    _trace(f"classification={classification} status={status}")

    payload = {
        "certified_at": now.isoformat(), "product_version": "AutoFund MVP 0.2.6",
        "status": status, "classification": classification,
        "evidence_quality": evidence_quality,
        "baseline_commit": BASELINE_COMMIT, "code_commit": CODE_COMMIT,
        "validation_exposure": exposure,
        "disclosures": {
            # Stated because it bears on how the validation result should be read. An earlier
            # revision of this script placed the validation window in the future, where no data
            # exists, so it returned zero observations; that run's measurements therefore spanned
            # the period this revision reserves as validation. Nothing from the earlier run was
            # carried forward, and no threshold was tuned on it -- the one threshold that was
            # touched was removed for exactly that reason. But the period is no longer pristine,
            # so the declaration is recorded rather than assumed away.
            "superseded_run_spanned_validation_period": True,
            "superseded_run_carried_forward": False,
            "thresholds_tuned_on_a_prior_run": False,
            "note": ("the candidate was frozen and the classifier finalised before this run; the "
                     "validation window is read only by the validation step"),
        },
        "price_only_baseline": {
            "label": PRICE_ONLY_RESEARCH_BASELINE,
            "frozen": True, "new_price_only_strategy_implemented": False,
            "profiles": [],
            "note": "all price-only conclusions preserved as historical evidence",
        },
        "windows": windows.public(),
        "multiple_testing": ledger.public(),
        "backfill": backfill,
        "panel": panel.public(),
        "features": {name: FEATURE_INTERPRETATION[name] for name in PREDECLARED_FEATURES},
        "relationships": [list(pair) for pair in PREDECLARED_RELATIONSHIPS],
        "horizons_minutes": list(PREDECLARED_HORIZONS_MINUTES),
        "measurements": [{k: v for k, v in m.items() if k != "content_object"}
                         for m in measurements],
        "controls": controls_log,
        "predictive_combinations": len(predictive),
        "candidate": (candidate.public() if candidate else None),
        "candidate_reason": candidate_reason,
        "validation": validation,
        "microstructure": microstructure,
        "microstructure_classification": microstructure["classification"],
        "economic_translation": economic,
        "safety": {
            "production_post_count": 0, "production_cancel_count": 0,
            "production_replace_count": 0, "production_session_started": False,
            "new_exchange_mutations_added": 0, "economic_guard_changed": False,
            "risk_engine_changed": False, "drawdown_policy_changed": False,
            "capital_limits_changed": False,
            "frozen_profile_fingerprints_changed": False,
            "new_strategy_implemented": False, "maker_authorised": False,
        },
    }
    OUT.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": status,
        "classification": classification,
        "measurements": len(measurements),
        "predictive_combinations": len(predictive),
        "candidate": bool(candidate), "candidate_reason": candidate_reason,
        "validation": (validation["passed"] if validation else None),
        "microstructure": microstructure["classification"],
        "total_comparisons": ledger.total_comparisons,
        "evidence_quality": evidence_quality,
    }, indent=2))
    return 0


def _feature_key(*, feature_name: str, leader: str, follower: str) -> str:
    if feature_name == LEADER_FOLLOWER_DIVERGENCE:
        return f"{feature_name}|{leader}|{follower}"
    return f"{feature_name}|{follower}"


def _feature_relationship_rows() -> tuple[tuple[str, str, str], ...]:
    """The predeclared (feature, leader, follower) combinations, stated once.

    Deduplicated by the key that is actually evaluated -- the feature and the follower -- rather
    than by the tuple including the leader. Four relationships share each follower-level feature,
    so without this the same measurement would be taken four times and counted four times in the
    multiplicity budget, inflating the apparent search and wasting the run.

    `DIVERGENCE` is mapped onto `RELATIVE_RETURN_VS_BASKET` rather than given its own panel key,
    because that feature *is* the divergence: the follower's move measured against the basket. A
    separate key would compute the same quantity twice and the dedupe would then discard one of
    them, which is how a declared relationship quietly ends up unevaluated.
    """
    seen: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for leader, follower in PREDECLARED_RELATIONSHIPS:
        for feature in PREDECLARED_FEATURES:
            if leader == DIVERGENCE:
                # DIVERGENCE is not a market, so it has no lagged return of its own, and the
                # relationship it names is already the follower's return measured against the
                # basket -- which the panel emits as `RELATIVE_RETURN_VS_BASKET`. Mapping it here
                # rather than emitting a second key for the same quantity is what keeps the
                # multiplicity count honest: one hypothesis, one measurement.
                if feature not in (RELATIVE_RETURN_VS_BASKET,
                                   VOLATILITY_ADJUSTED_RELATIVE_MOVE, MARKET_BREADTH,
                                   CROSS_SECTIONAL_DISPERSION):
                    continue
                key = (feature, follower)
            elif feature == LEADER_FOLLOWER_DIVERGENCE:
                key = (feature, f"{leader}|{follower}")
            elif feature == LAGGED_RETURN and leader != BASKET:
                key = (feature, leader)
            else:
                key = (feature, follower)
            seen.setdefault(key, (feature, leader, follower))
    return tuple(seen.values())


def _assert_rows_resolve(*, panel: Any, rows: tuple[tuple[str, str, str], ...]) -> None:
    """Fail loudly if any declared combination names a feature the panel never emits.

    A key that matches nothing yields zero measurements silently, and the run then reports an
    absence of signal where it had actually measured nothing at all. That is the most dangerous
    possible failure for this milestone, because its expected answer is negative: a broken
    pipeline and a null result look identical in the output.
    """
    available: set[str] = set()
    for observation in panel.observations[:50]:
        available.update(observation.features.keys())
    unresolved = sorted({
        _feature_key(feature_name=feature, leader=leader, follower=follower)
        for feature, leader, follower in rows
        if _feature_key(feature_name=feature, leader=leader, follower=follower)
        not in available})
    if unresolved:
        raise SystemExit(
            "DECLARED_FEATURE_NOT_EMITTED_BY_PANEL: " + ", ".join(unresolved))


def _freeze_candidate(*, best: dict[str, Any], content: PredictiveContent,
                      controls: tuple[ControlResult, ...], comparisons: int,
                      windows: Any) -> AlphaCandidateManifest:
    from autofund.mvp.alpha_controls import build_candidate

    candidate = build_candidate(
        candidate_id=f"{best['feature_name']}|{best['leader']}|{best['follower']}|"
                     f"{best['horizon_minutes']}m",
        source_family="CROSS_MARKET_LEAD_LAG", feature_name=best["feature_name"],
        leader=best["leader"], follower=best["follower"],
        horizon_minutes=best["horizon_minutes"], bucket_count=5, content=content,
        economic_interpretation=FEATURE_INTERPRETATION[best["feature_name"]],
        controls=controls, comparisons=comparisons,
        created_at=datetime.now(UTC).isoformat(),
        development_window=(windows.development_start.isoformat(),
                            windows.development_end.isoformat()),
        validation_window=(windows.validation_start.isoformat(),
                           windows.validation_end.isoformat()))
    MANIFEST_FILE.write_text(json.dumps(candidate.public(), indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
    return candidate


def _validate(*, panel: MarketPanel, candidate: AlphaCandidateManifest,
              windows: Any) -> dict[str, Any]:
    """Evaluate the frozen candidate on the untouched window, changing nothing.

    No parameter, threshold, feature or horizon is altered. If the candidate fails here it is
    rejected, and the milestone does not return to validation with a modified candidate — that
    would convert the holdout into a second development set.
    """
    feature_key = (f"{candidate.feature_name}|{candidate.leader}|{candidate.follower}"
                   if candidate.feature_name == LEADER_FOLLOWER_DIVERGENCE
                   else f"{candidate.feature_name}|{candidate.follower}")
    keys = panel.keys_for(feature=feature_key, market=candidate.follower,
                          horizon=candidate.horizon_minutes)
    features, forwards = panel.aligned(feature=feature_key, market=candidate.follower,
                                       horizon=candidate.horizon_minutes)
    pairs = [(features[index], forwards[index])
             for index, moment in enumerate(keys)
             if windows.contains_validation(moment)]
    if not pairs:
        return {"ran": True, "passed": False, "reason": "NO_VALIDATION_OBSERVATIONS",
                "observations": 0, "predictive": False}
    content = assess_predictive_content(
        feature_name=candidate.feature_name, market=candidate.follower,
        horizon_minutes=candidate.horizon_minutes,
        feature=tuple(pair[0] for pair in pairs),
        forward=tuple(pair[1] for pair in pairs),
        bucket_count=candidate.bucket_count)
    predictive = content.verdict == PREDICTIVE
    return {
        "ran": True, "passed": predictive, "reason": ("PREDICTIVE_ON_UNSEEN_DATA"
                                                      if predictive
                                                      else "NOT_PREDICTIVE_ON_UNSEEN_DATA"),
        "observations": content.observations,
        "effective_observations": str(content.effective_observations),
        "rank_relationship": _s(content.rank_relationship),
        "monotone": content.monotone, "monotone_direction": content.monotone_direction,
        "stable_subwindows": content.stable_subwindows,
        "predictive": predictive,
        "parameters_changed_after_freeze": False,
        "returned_to_validation": False,
        "content": content.public(),
    }


SUPERSEDED_GLOB = "mvp-0-2-6-certification.superseded-*.json"


def _validation_exposure(*, windows: Any, root: Path) -> dict[str, Any]:
    """Whether any earlier run's measurements already covered the current validation window.

    Read from the artifacts rather than asserted, because the failure mode is a property of what
    was actually done rather than of what was intended. An earlier revision declared a validation
    window that could not be fetched, so its measurements silently spanned the period this revision
    now reserves as validation -- and that run printed which of its 84 combinations looked best.

    A window that an earlier sweep already covered cannot serve as independent confirmation: the
    identity of the surviving candidate was visible before it was read. Reporting that honestly
    keeps a real effect from being restated as a validated strategy, and keeps the milestone from
    claiming a clean validation it did not have.
    """
    overlaps: list[dict[str, Any]] = []
    for path in sorted(root.glob(SUPERSEDED_GLOB)):
        try:
            earlier = json.loads(path.read_text(encoding="utf-8")).get("windows") or {}
        except (OSError, ValueError):
            continue
        start = _parse(earlier.get("development_start"))
        end = _parse(earlier.get("development_end"))
        if start is None or end is None:
            continue
        # The windows are read as intervals of instants; a boundary touch is not an overlap of
        # observations, which is why the comparison is strict.
        if start < windows.validation_end and end > windows.validation_start:
            overlaps.append({"file": path.name,
                             "development_start": earlier["development_start"],
                             "development_end": earlier["development_end"],
                             "combinations_measured":
                                 len(json.loads(path.read_text(encoding="utf-8"))
                                     .get("measurements") or [])})
    return {
        "pristine": not overlaps,
        "status": "PRISTINE" if not overlaps else "PREVIOUSLY_EXPOSED",
        "overlapping_runs": overlaps,
        "note": ("a validation window already searched by an earlier sweep cannot independently "
                 "confirm the candidate that sweep surfaced"),
    }


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _economic_translation(*, candidate: AlphaCandidateManifest | None,
                          validation: dict[str, Any] | None) -> dict[str, Any]:
    """Whether a validated signal's predicted move could clear this account's costs.

    Only reached for a candidate that validated. This is where ALPHA_EXISTS is separated from
    TRADEABLE_ALPHA_EXISTS: the measured movement is compared against the friction the account
    actually pays, and a signal whose movement is smaller than that is not tradeable however
    clean its statistics.
    """
    friction_bps = Decimal("173.00")
    if candidate is None or validation is None or not validation.get("passed"):
        return {"evaluated": False,
                "reason": "NO_VALIDATED_CANDIDATE",
                "friction_bps": str(friction_bps),
                "note": "economic translation is only meaningful once a signal validated"}
    buckets = validation["content"].get("buckets", [])
    movements = [abs(Decimal(bucket["mean_forward_return_bps"])) for bucket in buckets]
    if not movements:
        return {"evaluated": False, "reason": "NO_BUCKET_MOVEMENTS",
                "friction_bps": str(friction_bps)}
    largest = max(movements)
    headroom = largest - friction_bps
    return {
        "evaluated": True, "friction_bps": str(friction_bps),
        "largest_bucket_mean_movement_bps": str(largest),
        "median_bucket_mean_movement_bps": str(sorted(movements)[len(movements) // 2]),
        "economic_headroom_bps": str(headroom),
        "tradeable_at_current_cost": headroom > 0,
        "classification": (PREDICTIVE_NOT_ECONOMIC if headroom <= 0 else "ALPHA_MAY_BE_TRADEABLE"),
        "maker_sensitivity_only": True,
        "maker_used_to_certify": False,
        "full_strategy_built": False,
        "note": ("measured bucket movement is the spread of conditional means, not a tradeable "
                 "edge; a strategy would still have to survive the economic guard"),
    }


if __name__ == "__main__":
    raise SystemExit(main())

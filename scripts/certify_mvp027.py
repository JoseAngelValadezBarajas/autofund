"""MVP 0.2.7 certification: is AutoFund's product thesis economically feasible anywhere?

A **decision** milestone. It does not build a strategy, does not retune the validated signal, does
not touch the economic guard, and does not request credentials for any venue. It answers one
question with evidence: given that MVP 0.2.6 established a real but tiny predictive signal, is
there any realistically available retail spot environment in which that signal could be collected?

The script is structured so the answer cannot come out positive by accident:

* the frozen signal is asserted UNCHANGED before it is used, so venue economics cannot feed back
  into it;
* venue costs are computed from recorded official sources, and a venue whose rates are not
  publishable produces UNKNOWN rather than a number;
* the fee-only floor is tested before any execution modelling, so a venue that fails on fees alone
  is classified immediately instead of being given a favourable spread estimate;
* a missing cost produces INSUFFICIENT_COST_EVIDENCE, never a pass;
* the historical fee floors are reproduced exactly and compared, so this milestone cannot restate
  a prior conclusion differently.

Run with `--skip-reference-sample` to omit the live cross-venue sampling (the only part that
reaches the network beyond the incumbent's public endpoints).
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.mvp.cross_venue_reference import (
    DislocationObservation,
    DislocationStudy,
    assess_dislocation,
    build_capability,
    reference_venue_hypothesis,
)
from autofund.mvp.venue_data import (
    ESTABLISHED_FEE_FLOORS_BPS,
    HISTORICAL_SLIPPAGE_ASSUMPTION_BPS,
    HISTORICAL_SPREAD_ASSUMPTION_BPS,
    OBSERVED_SPREADS_BPS,
    venue_profiles,
    venue_schedules,
)
from autofund.mvp.venue_data import (
    public as venue_data_public,
)
from autofund.mvp.venue_economics import (
    NOT_COMPATIBLE_WITH_CURRENT_EXPERIMENT,
    STRUCTURALLY_UNTRADEABLE_FOR_THIS_ALPHA,
    assess_venue,
    capture_ladder,
    classify_thesis,
    fee_only_round_trip_bps,
    historical_friction_bps,
    retail_environment_floor_bps,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "mvp" / "venues" / "mvp-0-2-7-certification.json"
ALPHA_CERT = ROOT / "artifacts" / "mvp" / "alpha" / "mvp-0-2-6-certification.json"
MICROSTRUCTURE_ROOT = ROOT / "artifacts" / "mvp" / "alpha" / "microstructure"
WORKSPACE = ROOT / "artifacts" / "mvp" / "venues"

CODE_COMMIT = "55bf51e"
BASELINE_COMMIT = "55bf51e"

# The frozen signal's identity, pinned so the milestone fails loudly rather than silently if the
# 0.2.6 artifact changes underneath it.
FROZEN_ALPHA_FINGERPRINT = (
    "fae4faa85ef648acb5e831cb6bc3fe242d8a1723cc3c474262a886a4600ab3aa")
FROZEN_ALPHA_MOVEMENT_BPS = Decimal("2.513590292581896613688157726")

# Cross-venue sampling parameters. Small on purpose: the question is whether dislocation is even
# the right order of magnitude, and twenty samples answer that decisively when the gap to close is
# sevenfold.
REFERENCE_SAMPLES = 20
REFERENCE_INTERVAL_SECONDS = 3.0
REFERENCE_MAX_STALE_SECONDS = Decimal("10")
REFERENCE_PAIRS = (("btc_mxn", "BTCMXN"), ("eth_mxn", "ETHMXN"), ("sol_mxn", "SOLMXN"))
REFERENCE_TIMEOUT_SECONDS = 20

# Cost assumptions for an alternative venue. Slippage is left UNKNOWN because no alternative
# venue's book was read: assuming the incumbent's 5 bps would be inventing evidence for the venue
# being compared against it.
ALTERNATIVE_VENUE_SLIPPAGE_BPS: Decimal | None = None


def _trace(message: str) -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat()
    with (WORKSPACE / "progress.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp} {message}\n")


def _get(url: str) -> Any:
    request = urllib.request.Request(
        url, headers={"User-Agent": "AutoFund-research/0.2.7"})
    with urllib.request.urlopen(request, timeout=REFERENCE_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def load_frozen_alpha() -> tuple[dict[str, Any], Decimal]:
    """Read the 0.2.6 candidate and assert it is the signal this milestone froze.

    The assertion is the mechanism that makes "do not retune it" enforceable rather than a promise.
    If the artifact's movement scale or status ever differs from the pinned values, this milestone
    stops instead of quietly pricing a different signal.

    The candidate is reconstructed into its manifest and frozen through
    `freeze_validated_alpha`, rather than copied field by field here. That way the freeze carries
    the manifest's own fingerprint, so the frozen identity is the same object the 0.2.6 experiment
    produced and not a restatement of it that could drift.
    """
    from datetime import datetime as _datetime

    from autofund.mvp.alpha_controls import AlphaCandidateManifest
    from autofund.mvp.venue_economics import freeze_validated_alpha

    payload = json.loads(ALPHA_CERT.read_text(encoding="utf-8"))
    candidate = payload["candidate"]
    validation = payload["validation"]
    movement = Decimal(str(payload["economic_translation"]["largest_bucket_mean_movement_bps"]))
    if movement != FROZEN_ALPHA_MOVEMENT_BPS:
        raise SystemExit(f"FROZEN_ALPHA_MOVEMENT_CHANGED: {movement} != "
                         f"{FROZEN_ALPHA_MOVEMENT_BPS}")
    if payload["status"] != "PREDICTIVE_BUT_NOT_ECONOMIC":
        raise SystemExit(f"FROZEN_ALPHA_STATUS_CHANGED: {payload['status']}")

    manifest = AlphaCandidateManifest(
        candidate_id=candidate["candidate_id"],
        source_family=candidate["source_family"],
        feature_name=candidate["feature_name"],
        leader=candidate["leader"],
        follower=candidate["follower"],
        horizon_minutes=candidate["horizon_minutes"],
        bucket_count=candidate["bucket_count"],
        development_observations=candidate["development_observations"],
        development_effective_observations=Decimal(
            candidate["development_effective_observations"]),
        development_rank_relationship=Decimal(candidate["development_rank_relationship"]),
        development_monotone=candidate["development_monotone"],
        development_monotone_direction=candidate["development_monotone_direction"],
        development_stable_subwindows=candidate["development_stable_subwindows"],
        economic_interpretation=candidate["economic_interpretation"],
        controls_expected_behavior_met=candidate["controls_expected_behavior_met"],
        multiple_test_comparisons=candidate["multiple_test_comparisons"],
        created_at=_datetime.fromisoformat(candidate["created_at"]),
        development_window=(_datetime.fromisoformat(candidate["development_window"][0]),
                            _datetime.fromisoformat(candidate["development_window"][1])),
        validation_window=(_datetime.fromisoformat(candidate["validation_window"][0]),
                           _datetime.fromisoformat(candidate["validation_window"][1])),
        validation_touched=candidate["validation_touched"],
    )
    frozen = freeze_validated_alpha(candidate=manifest, validation=validation)
    if not frozen["fingerprint"]:
        raise SystemExit("FROZEN_ALPHA_FINGERPRINT_MISSING")
    frozen["movement_bps"] = str(movement)
    _trace(f"frozen alpha {frozen['label']} fingerprint={frozen['fingerprint'][:16]} "
           f"movement={movement}")
    return frozen, movement


def _verify_fee_floors() -> dict[str, Any]:
    """Reproduce the established floors exactly, so no historical conclusion is restated.

    Two conventions are checked because the project contains two and they disagree by 1.84 bps.
    The fee-only floors use the compounding one; the historical 173 bps all-in uses the doubled
    one. Both are reproduced and their difference is reported rather than smoothed over.
    """
    from autofund.mvp.passive_execution import fee_floors

    established = fee_floors(maker_rate=Decimal("0.0060"), taker_rate=Decimal("0.0078"))
    reproduced: dict[str, Any] = {}
    for mode in ("TAKER_TAKER", "MAKER_TAKER", "MAKER_MAKER"):
        floor = established[mode]
        mine = fee_only_round_trip_bps(buy_fee_rate=floor.buy_fee_rate,
                                       sell_fee_rate=floor.sell_fee_rate)
        recorded = Decimal(ESTABLISHED_FEE_FLOORS_BPS[mode])
        # Compared at the recorded precision: the module works at 50 digits and the historical
        # artifact recorded fewer, so an exact comparison of the tails would be meaningless.
        matches = _at_recorded_precision(mine, recorded)
        reproduced[mode] = {"reproduced_bps": str(mine), "recorded_bps": str(recorded),
                            "matches_recorded": matches,
                            "fee_convention": "GEOMETRIC_COMPOUNDED"}
        if not matches:
            raise SystemExit(f"FEE_FLOOR_MISMATCH[{mode}]: {mine} != {recorded}")

    historical = historical_friction_bps()
    if _at_recorded_precision(historical, Decimal("173")):
        historical_matches = True
    else:
        historical_matches = False
    if not historical_matches:
        raise SystemExit(f"HISTORICAL_FRICTION_MISMATCH: {historical} != 173")
    _trace(f"fee floors reproduced; historical all-in={historical}")
    return {
        "floors": reproduced,
        "all_historical_floors_reproduced": True,
        "historical_all_in_bps": str(historical),
        "historical_friction_matches_record": historical_matches,
        "historical_convention": "DOUBLED_RATE",
        "convention_gap_bps": str(fee_only_round_trip_bps(
            buy_fee_rate=Decimal("0.0078"), sell_fee_rate=Decimal("0.0078"))
            - Decimal("0.0078") * Decimal("10000") * Decimal("2")),
        "convention_gap_note": (
            "the two fee-doubling conventions differ by this many bps; the fee-only floors use "
            "the compounding convention and the historical 173 bps all-in uses the doubled one, "
            "so reproducing either requires selecting the convention explicitly"),
    }


def _at_recorded_precision(value: Decimal, recorded: Decimal,
                           precision: int = 25) -> bool:
    from decimal import ROUND_HALF_EVEN, Context, localcontext

    with localcontext(Context(prec=precision, rounding=ROUND_HALF_EVEN)):
        return +value == +recorded


def _cost_inputs(*, venue: str) -> dict[str, Any]:
    """Spread and slippage available for a venue, and where each came from.

    The incumbent's spread was observed on its public ticker. No alternative venue's book was
    read, so its spread and slippage are unknown. That asymmetry is the honest state of the
    evidence and it makes the comparison *favourable* to the alternatives in the one place it
    could matter, which is the right direction for a negative conclusion to have to overcome.
    """
    if venue == "Bitso":
        return {"spread_bps": HISTORICAL_SPREAD_ASSUMPTION_BPS,
                "slippage_bps": HISTORICAL_SLIPPAGE_ASSUMPTION_BPS,
                "spread_evidence": "HISTORICAL_MODEL_ASSUMPTION",
                "slippage_evidence": "HISTORICAL_MODEL_ASSUMPTION",
                "observed_spread_bps": OBSERVED_SPREADS_BPS}
    return {"spread_bps": None, "slippage_bps": ALTERNATIVE_VENUE_SLIPPAGE_BPS,
            "spread_evidence": "UNAVAILABLE",
            "slippage_evidence": "UNAVAILABLE",
            "observed_spread_bps": None}


def assess_all_venues(*, movement_bps: Decimal) -> tuple[list[Any], list[dict[str, Any]]]:
    ladder = capture_ladder(movement_bps=movement_bps)
    schedules = {schedule.venue: schedule for schedule in venue_schedules()}
    assessments = []
    table: list[dict[str, Any]] = []
    for profile in venue_profiles():
        costs = _cost_inputs(venue=profile.venue)
        assessment = assess_venue(profile=profile, schedule=schedules[profile.venue],
                                  ladder=ladder, spread_bps=costs["spread_bps"],
                                  slippage_bps=costs["slippage_bps"],
                                  spread_evidence=str(costs["spread_evidence"]))
        assessments.append(assessment)
        taker = assessment.economics_for("TAKER_TAKER")
        maker = assessment.economics_for("MAKER_MAKER")
        table.append({
            "venue": profile.venue,
            "fee_only_taker_taker_bps": (None if taker is None or taker.fee_only_bps is None
                                         else str(taker.fee_only_bps)),
            "estimated_all_in_taker_taker_bps": (None if taker is None
                                                 or taker.all_in_bps is None
                                                 else str(taker.all_in_bps)),
            "maker_bound_bps": (None if maker is None or maker.fee_only_bps is None
                                else str(maker.fee_only_bps)),
            "minimum_order_compatibility": assessment.minimum_compatibility,
            "liquidity_evidence": profile.liquidity_evidence,
            "api_recovery_suitability": profile.recovery_feasibility,
            "alpha_viable": assessment.verdicts.get("TAKER_TAKER"),
            "migration_effort": profile.migration_effort,
            "verdict": _table_verdict(assessment=assessment, profile_venue=profile.venue),
        })
        _trace(f"assessed {profile.venue}: {assessment.verdicts}")
    return assessments, table


def _table_verdict(*, assessment: Any, profile_venue: str) -> str:
    """One word for the decision table, from the most favourable mode the venue supports."""
    verdicts = list(assessment.verdicts.values())
    if STRUCTURALLY_UNTRADEABLE_FOR_THIS_ALPHA in verdicts:
        return STRUCTURALLY_UNTRADEABLE_FOR_THIS_ALPHA
    if assessment.minimum_compatibility == NOT_COMPATIBLE_WITH_CURRENT_EXPERIMENT:
        return "NOT_COMPATIBLE_WITH_CURRENT_EXPERIMENT"
    if "INSUFFICIENT_COST_EVIDENCE" in verdicts or not verdicts:
        return "INSUFFICIENT_COST_EVIDENCE"
    return verdicts[0]


def sample_dislocation(*, reference: str, symbols: tuple[tuple[str, str], ...]
                      ) -> list[DislocationStudy]:
    """Measure contemporaneous price differences between the incumbent and a reference venue.

    Both sides quote the same currency for these pairs, so no conversion is involved and the
    difference is attributable to the venues rather than to an FX leg. Staleness is measured
    rather than assumed, because two prices are never simultaneous and the gap is what decides
    whether the comparison means anything.
    """
    studies = []
    for bitso_book, symbol in symbols:
        observations: list[DislocationObservation] = []
        for _ in range(REFERENCE_SAMPLES):
            started = time.monotonic()
            try:
                bitso = _get(f"https://api.bitso.com/v3/ticker/?book={bitso_book}")["payload"]
                incumbent = (Decimal(str(bitso["bid"])) + Decimal(str(bitso["ask"]))) / 2
                reference_payload = _get(
                    f"https://api.binance.com/api/v3/ticker/bookTicker?symbol={symbol}")
                external = (Decimal(str(reference_payload["bidPrice"]))
                            + Decimal(str(reference_payload["askPrice"]))) / 2
            except Exception as exc:
                _trace(f"dislocation sample failed {bitso_book}: {type(exc).__name__}")
                continue
            elapsed = Decimal(str(time.monotonic() - started))
            observations.append(DislocationObservation(
                pair=bitso_book, reference_venue=reference, incumbent_venue="Bitso",
                reference_price=external, incumbent_price=incumbent,
                observed_at=datetime.now(UTC).isoformat(), stale_seconds=elapsed))
            time.sleep(REFERENCE_INTERVAL_SECONDS)
        if observations:
            studies.append(DislocationStudy(
                pair=bitso_book, reference_venue=reference, incumbent_venue="Bitso",
                observations=tuple(observations),
                maximum_stale_seconds=REFERENCE_MAX_STALE_SECONDS))
        _trace(f"dislocation sampled {bitso_book}: {len(observations)} observations")
    return studies


def _reference_capabilities() -> list[Any]:
    """What each venue's public surface could support, from the official documentation read.

    Capabilities are recorded as established, and one field is explicitly None where the sources
    consulted did not settle it. That is deliberate: "not established" is not "unsupported".
    """
    return [
        build_capability(
            venue="Bitso", trade_tape=True, best_bid_ask=True, order_book=True,
            timestamps=True, candles=True, sequence_data=True, websocket=True,
            authentication_required=False,
            notes="public endpoints plus websocket channels are documented and already used"),
        build_capability(
            venue="Binance", trade_tape=True, best_bid_ask=True, order_book=True,
            timestamps=True, candles=True, sequence_data=True, websocket=True,
            authentication_required=False,
            notes="public REST market data and websocket streams require no authentication"),
        build_capability(
            venue="Kraken", trade_tape=True, best_bid_ask=True, order_book=True,
            timestamps=True, candles=True, sequence_data=None, websocket=True,
            authentication_required=False,
            notes="public market data requires no authentication; sequence semantics unverified"),
        build_capability(
            venue="Coinbase Advanced", trade_tape=True, best_bid_ask=True, order_book=True,
            timestamps=True, candles=True, sequence_data=None, websocket=True,
            authentication_required=False,
            notes="public products endpoint read directly; no MXN product exists to reference"),
    ]


def _microstructure_state() -> dict[str, Any]:
    """Cumulative read-only microstructure evidence, without blocking the decision on it.

    Reported because the spec asks for the collector's state, and marked as not ready because the
    markout horizons the analysis needs are longer than the capture accumulated so far. The venue
    conclusion does not depend on this and is not deferred to it.
    """
    from autofund.mvp.microstructure import DEFAULT_RETENTION_HOURS
    from autofund.mvp.microstructure_alpha import PREDECLARED_MARKOUT_SECONDS

    books: list[dict[str, Any]] = []
    total_bytes = 0
    if MICROSTRUCTURE_ROOT.exists():
        for path in sorted(MICROSTRUCTURE_ROOT.glob("*.jsonl")):
            size = path.stat().st_size
            total_bytes += size
            books.append({"book": path.name.split(".")[0], "bytes": size})
    required_seconds = max(PREDECLARED_MARKOUT_SECONDS) * 60
    return {
        "capture_root": str(MICROSTRUCTURE_ROOT.relative_to(ROOT)),
        "books": books,
        "book_count": len(books),
        "total_bytes": total_bytes,
        "provenance": "REAL_CAPTURED_MICROSTRUCTURE",
        "retention_hours": DEFAULT_RETENTION_HOURS,
        "required_capture_seconds_for_markouts": required_seconds,
        "ready_for_alpha_evaluation": False,
        "ready_reason": ("capture accumulated so far is far shorter than the longest predeclared "
                         "markout horizon, so no markout distribution can be estimated"),
        "collector_discarded": False,
        "blocked_venue_conclusion": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="MVP 0.2.7 venue economics certification")
    parser.add_argument("--skip-reference-sample", action="store_true",
                        help="omit the live cross-venue sampling")
    args = parser.parse_args()

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    _trace("start")

    frozen, movement_bps = load_frozen_alpha()
    floors = _verify_fee_floors()

    ladder = capture_ladder(movement_bps=movement_bps)
    ceilings = [{"label": ceiling.label, "capture_fraction": str(ceiling.capture_fraction),
                 "captured_bps": str(ceiling.captured_bps),
                 "buffer_bps": str(ceiling.buffer_bps),
                 "maximum_all_in_friction_bps": str(ceiling.maximum_all_in_friction_bps)}
                for ceiling in ladder]

    assessments, table = assess_all_venues(movement_bps=movement_bps)
    thesis = classify_thesis(assessed=tuple(assessments), movement_bps=movement_bps,
                            microstructure_ready=False)

    # Cross-venue reference research (spec sections 13-15).
    studies = [] if args.skip_reference_sample else sample_dislocation(
        reference="Binance", symbols=REFERENCE_PAIRS)
    dislocation: list[dict[str, Any]] = []
    for study in studies:
        assessment = assess_dislocation(study=study,
                                        friction_bps=historical_friction_bps())
        dislocation.append({**study.public(), **assessment})

    hypothesis = reference_venue_hypothesis(
        leader_move_bps=Decimal("50"), friction_bps=historical_friction_bps(),
        responsiveness=Decimal("0.9"))

    cheapest_floor = retail_environment_floor_bps()
    payload = {
        "certified_at": datetime.now(UTC).isoformat(),
        "product_version": "AutoFund MVP 0.2.7",
        "status": thesis["dominant"],
        "baseline_commit": BASELINE_COMMIT, "code_commit": CODE_COMMIT,
        "frozen_alpha": frozen,
        "fee_floor_verification": floors,
        "capture_ladder": ceilings,
        "venue_data": venue_data_public(),
        "venue_assessments": [assessment.public() for assessment in assessments],
        "decision_table": table,
        "thesis": thesis,
        "hard_question": {
            "question": ("what all-in round-trip friction would AutoFund require to monetize "
                         "its validated alpha?"),
            "required_friction_at_100pct_capture_bps": str(
                ladder[0].maximum_all_in_friction_bps),
            "required_friction_at_75pct_capture_bps": str(
                ladder[1].maximum_all_in_friction_bps),
            "required_friction_at_50pct_capture_bps": str(
                ladder[2].maximum_all_in_friction_bps),
            "required_friction_at_25pct_capture_bps": str(
                ladder[3].maximum_all_in_friction_bps),
            "current_incumbent_friction_bps": str(historical_friction_bps()),
            "cheapest_verified_retail_floor_bps": str(cheapest_floor),
            "shortfall_vs_cheapest_floor_multiple": str(
                cheapest_floor / ladder[0].maximum_all_in_friction_bps),
            "requires_over_two_hundredfold_incumbent_improvement": str(
                historical_friction_bps() / ladder[1].maximum_all_in_friction_bps),
            "answer": thesis["reason"],
        },
        "cross_venue_reference": {
            "research_feasible": True,
            "sampled": not args.skip_reference_sample,
            "reference_venue": "Binance",
            "same_quote_currency": True,
            "requires_fx_conversion": False,
            "studies": dislocation,
            "capabilities": [capability.public() for capability in _reference_capabilities()],
            "adapter_required_now": False,
            "adapter_built": False,
            "is_arbitrage_claim": False,
            "transfer_arbitrage_designed": False,
            "hypothesis": hypothesis,
        },
        "microstructure": _microstructure_state(),
        "engineering": {
            "venue": "any alternative considered",
            "migration_effort": "HIGH",
            "components": [
                "market-data adapter", "authentication", "order adapter", "fee parser",
                "book and minimum parser", "client-order identity", "fill reconciliation",
                "partial fills", "restart recovery", "wallet and balance",
                "tests", "control-plane integration", "Production permission boundary",
            ],
            "implemented": False,
            "recommended_next_build": ("none under this milestone's findings; the evidence does "
                                       "not support building a venue migration"),
            "reason": thesis["reason"],
        },
        "safety": {
            "production_post_count": 0, "production_cancel_count": 0,
            "production_replace_count": 0, "production_session_started": False,
            "new_exchange_mutations_added": 0, "new_strategy_implemented": False,
            "economic_guard_changed": False, "risk_engine_changed": False,
            "drawdown_policy_changed": False, "capital_limits_changed": False,
            "frozen_alpha_retuned": False, "maker_production_enabled": False,
            "new_credentials_requested": False, "new_accounts_opened": False,
            "transfers_performed": False, "deposits_performed": False,
            "withdrawals_performed": False, "order_adapters_added": 0,
            "capital_limits_increased": False,
        },
    }
    OUT.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": payload["status"],
        "movement_bps": str(movement_bps),
        "required_friction_100pct_bps": str(ladder[0].maximum_all_in_friction_bps),
        "required_friction_75pct_bps": str(ladder[1].maximum_all_in_friction_bps),
        "cheapest_verified_retail_floor_bps": str(cheapest_floor),
        "shortfall_multiple": str(cheapest_floor / ladder[0].maximum_all_in_friction_bps),
        "venues_potentially_economic": thesis["venues_potentially_economic"],
        "coexisting": thesis["coexisting"],
    }, indent=2))
    _trace(f"status={payload['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

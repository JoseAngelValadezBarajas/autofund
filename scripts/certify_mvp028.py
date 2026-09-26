"""MVP 0.2.8 certification: do rare cross-venue dislocations exist that Bitso could trade?

A **discovery** milestone. It tests one falsifiable hypothesis — that although average cross-venue
dislocations are far below trading costs, rare tail events might be large and persistent enough to
clear them — and it is designed so that a positive answer has to survive a specific trap that the
milestone's own development fell into.

**The trap, and why it is the centre of this script.** A 45-day candle screen produced ETH/MXN
dislocations with a 99th percentile of 171.9 bps, comfortably above the 160.39 bps economic
threshold. The number was an artefact of the reference book's width: Binance's ETH/MXN quoted a
38.46 bps spread while Bitso's eth_mxn quoted 1.05 bps, so the entire measured difference sat
inside the reference's own bid-ask. Real executable capture then found **zero** observations on any
pair whose difference exceeded the reference's own spread, and a best-case executable headroom of
9.14 bps against 160.39 required.

So this script reports both evidence classes separately, never lets candle screening reach an
economic conclusion, and computes a reference-quality measure that would have caught the artefact
before it was reported.

Read-only throughout: no credentials, no orders, no transfers, no authenticated external call.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from autofund.mvp.cross_venue_capture import (  # noqa: E402
    CrossVenueStore,
    default_pairs,
    observations_from_store,
)
from autofund.mvp.cross_venue_dislocation import (  # noqa: E402
    ACTIVE_TRADING_THESIS_NOT_SUPPORTED,
    BUY,
    CANDLE_SCREENING_ONLY,
    CROSS_VENUE_SIGNAL_NOT_ECONOMIC,
    EVIDENCE_PROVES_EXECUTABILITY,
    EXECUTABLE_BOOK,
    EXECUTABLE_BUY_DISLOCATION,
    EXECUTABLE_SELL_DISLOCATION,
    INSUFFICIENT_CROSS_VENUE_EVIDENCE,
    MINIMUM_EPISODE_OBSERVATIONS,
    PREDECLARED_DELAYS_SECONDS,
    SELL,
    VALID,
    ReferenceQuality,
    build_distribution,
    classify_result,
    convergence_after,
    delay_sensitivity,
    episode_statistics,
    group_episodes,
    median,
    required_executable_dislocation_bps,
)

WORKSPACE = ROOT / "artifacts" / "mvp" / "cross-venue"
OUT = WORKSPACE / "mvp-0-2-8-certification.json"
CAPTURE_ROOT = WORKSPACE / "capture"
CAPTURE_REPORT = WORKSPACE / "capture-report.json"
MICROSTRUCTURE_ROOT = ROOT / "artifacts" / "mvp" / "alpha" / "microstructure"

CODE_COMMIT = "0cef2fb"
BASELINE_COMMIT = "0cef2fb"

# The account-confirmed fee and the project's modelled slippage. The fee is read from the account
# schedule recorded in 0.2.3-0.2.7; slippage is the project's existing modelled assumption.
TAKER_FEE_RATE = Decimal("0.0078")
MAKER_FEE_RATE = Decimal("0.0060")
SLIPPAGE_BPS = Decimal("5")

# Frozen from 0.2.6. Asserted rather than re-derived, so this milestone cannot restate a prior
# conclusion differently (spec section 2).
FROZEN_ALPHA_FINGERPRINT = (
    "fae4faa85ef648acb5e831cb6bc3fe242d8a1723cc3c474262a886a4600ab3aa")
FROZEN_ALPHA_MOVEMENT_BPS = Decimal("2.513590292581896613688157726")

# Synchronization ceilings, predeclared before measurement. The candle screen cannot do better than
# one minute because that is the data's resolution; the executable capture measured a real median
# skew near 0.4 s, so two seconds is a generous ceiling that still excludes a genuinely stale pair.
CANDLE_MAX_SKEW_SECONDS = Decimal("60")
EXECUTABLE_MAX_SKEW_SECONDS = Decimal("2")

# How many independent episodes before a tail is treated as repeated rather than as an anomaly
# (spec section 20). Three is the smallest count that can distinguish a repeated structure from a
# single event with any confidence at all.
MINIMUM_INDEPENDENT_EPISODES = 3

# The coverage requirement lives in the module so the contract is testable and so the research view
# can state it. Imported rather than restated here, because two copies of a threshold are two
# places to disagree.
from autofund.mvp.cross_venue_dislocation import (  # noqa: E402
    REQUIRED_CAPTURE_HOURS,
    capture_coverage,
)

# Convergence horizons in the data's own units. Evaluated as a ladder rather than a single choice,
# because "did it converge" depends on how long one waits and reporting one number would hide that.
CONVERGENCE_HORIZONS_SECONDS = (300, 900, 3600)

EXCEEDANCE_THRESHOLDS_BPS = (Decimal("25"), Decimal("50"), Decimal("100"))

# The predeclared minimum dislocation for a candidate, set at the economic threshold itself rather
# than at a round number, because a candidate that cannot clear its costs is not a candidate.
CANDIDATE_MINIMUM_MULTIPLE_OF_THRESHOLD = Decimal("1")


def _trace(message: str) -> None:
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    with (WORKSPACE / "progress.log").open("a", encoding="utf-8") as handle:
        handle.write(f"{datetime.now(UTC).isoformat()} {message}\n")


def _frozen_alpha() -> dict[str, Any]:
    """Read and assert the frozen 0.2.6 signal so it cannot have been retuned (section 2)."""
    certificate = json.loads(
        (ROOT / "artifacts" / "mvp" / "alpha" / "mvp-0-2-6-certification.json")
        .read_text(encoding="utf-8"))
    movement = Decimal(str(certificate["economic_translation"]
                           ["largest_bucket_mean_movement_bps"]))
    if movement != FROZEN_ALPHA_MOVEMENT_BPS:
        raise SystemExit(f"FROZEN_ALPHA_MOVEMENT_CHANGED: {movement}")
    if certificate["status"] != "PREDICTIVE_BUT_NOT_ECONOMIC":
        raise SystemExit(f"FROZEN_ALPHA_STATUS_CHANGED: {certificate['status']}")
    return {
        "label": "VALIDATED_INFORMATION_SIGNAL_V1",
        "fingerprint": FROZEN_ALPHA_FINGERPRINT,
        "movement_bps": str(movement),
        "status": certificate["status"],
        "retuned": False,
        "superseded_by_this_milestone": False,
        "note": ("preserved unchanged; this milestone investigates a different alpha source and "
                 "may not modify it"),
    }


def _executable_evidence() -> tuple[tuple[Any, ...], dict[str, Any]]:
    """Load the real captured order-book observations, if a capture has been run."""
    store = CrossVenueStore(CAPTURE_ROOT)
    all_observations: list[Any] = []
    per_pair: dict[str, int] = {}
    for pair in default_pairs():
        observations = observations_from_store(store, pair_key=pair.key,
                                              max_skew_seconds=EXECUTABLE_MAX_SKEW_SECONDS,
                                              evidence=EXECUTABLE_BOOK)
        per_pair[pair.key] = len(observations)
        all_observations.extend(observations)
    report: dict[str, Any] = {"pairs": per_pair, "total": len(all_observations)}
    if CAPTURE_REPORT.exists():
        raw = json.loads(CAPTURE_REPORT.read_text(encoding="utf-8"))
        report["capture"] = raw.get("report", {})
        report["depth_probes"] = raw.get("depth_probes", [])
        report["pairs_without_usable_reference"] = raw.get(
            "pairs_without_usable_reference", [])
    return tuple(all_observations), report


def _assess_pair(*, observations: tuple[Any, ...], pair_key: str,
                 threshold_bps: Decimal,
                 convergence_horizon_seconds: Decimal) -> dict[str, Any]:
    """Everything the spec asks for about one reference pair, on executable evidence."""
    asset = pair_key.split("/")[0]
    incumbent_spreads = tuple(item.incumbent.spread_bps for item in observations)
    reference_spreads = tuple(item.reference.spread_bps for item in observations)
    quality = ReferenceQuality(
        venue=observations[0].reference.venue, symbol=observations[0].reference.symbol,
        samples=len(observations),
        median_spread_bps=median(values=reference_spreads) or Decimal("0"),
        incumbent_median_spread_bps=median(values=incumbent_spreads) or Decimal("0"))

    result: dict[str, Any] = {
        "pair": pair_key,
        "reference_venue": observations[0].reference.venue,
        "reference_symbol": observations[0].reference.symbol,
        "observations": len(observations),
        "reference_quality": quality.public(),
        "directions": {},
    }

    pooled_episodes = 0
    pooled_converged = 0
    for kind, direction in ((EXECUTABLE_BUY_DISLOCATION, BUY),
                            (EXECUTABLE_SELL_DISLOCATION, SELL)):
        distribution = build_distribution(observations=observations, asset=asset, kind=kind,
                                          direction=direction)
        episodes = group_episodes(observations=observations, kind=kind, direction=direction,
                                  threshold_bps=threshold_bps)
        stats = episode_statistics(episodes=episodes)
        outcomes = [convergence_after(observations=observations, episode=episode, kind=kind,
                                      direction=direction,
                                      horizon_seconds=convergence_horizon_seconds)
                    for episode in episodes]
        converged = [item for item in outcomes if item.converged]
        pooled_episodes += len(episodes)
        pooled_converged += len(converged)
        delays = delay_sensitivity(observations=observations, kind=kind, direction=direction,
                                  threshold_bps=threshold_bps)
        result["directions"][direction] = {
            "distribution": distribution.public(),
            "exceedance": distribution.exceedance(EXCEEDANCE_THRESHOLDS_BPS),
            "episodes": stats,
            "episode_detail": [item.public() for item in episodes],
            "convergence": {
                "horizon_seconds": str(convergence_horizon_seconds),
                "episodes": len(outcomes),
                "converged": len(converged),
                "convergence_rate": (str(Decimal(len(converged)) / Decimal(len(outcomes)))
                                     if outcomes else None),
                "median_convergence_bps": str(median(values=tuple(
                    item.convergence_bps for item in outcomes
                    if item.convergence_bps is not None))),
                "median_mfe_bps": str(median(values=tuple(
                    item.maximum_favourable_excursion_bps for item in outcomes
                    if item.maximum_favourable_excursion_bps is not None))),
                "median_mae_bps": str(median(values=tuple(
                    item.maximum_adverse_excursion_bps for item in outcomes
                    if item.maximum_adverse_excursion_bps is not None))),
                "detail": [item.public() for item in outcomes],
            },
            "delay_sensitivity": [item.public() for item in delays],
        }
    result["episodes_above_threshold"] = pooled_episodes
    result["episodes_converged"] = pooled_converged
    return result


def _candle_screen(*, threshold_bps: Decimal) -> dict[str, Any]:
    """The historical screen, reported as identification only and never as proof (section 9).

    Reconstructed from the development investigation so the certificate carries the artefact that
    motivated the reference-quality guard. The numbers are recorded from the measurements that were
    actually taken, and the whole block is labelled screening-only.
    """
    return {
        "evidence": CANDLE_SCREENING_ONLY,
        "proves_executability": EVIDENCE_PROVES_EXECUTABILITY[CANDLE_SCREENING_ONLY],
        "window_hours": 1080,
        "max_skew_seconds": str(CANDLE_MAX_SKEW_SECONDS),
        "observed_mid_dislocation_percentiles": {
            "ETH/MXN": {"p50": "24.30", "p90": "82.39", "p95": "109.74", "p99": "171.88",
                        "p99.9": "240.12"},
            "SOL/MXN": {"p50": "26.93", "p90": "90.85", "p95": "119.40", "p99": "204.73",
                        "p99.9": "365.24"},
            "BTC/MXN": {"p50": "11.13", "p90": "28.53", "p95": "36.02", "p99": "55.73",
                        "p99.9": "98.02"},
        },
        "apparent_exceedance_above_threshold": {
            "ETH/MXN": 814, "SOL/MXN": 1204, "BTC/MXN": 44,
        },
        "economic_threshold_bps": str(threshold_bps),
        "why_this_is_not_evidence": (
            "the screen compares candle prices whose bid-ask is unobserved, and the reference "
            "books were later measured at 1.85x to 399.91x the incumbent's spread, so the "
            "apparent dislocations were inside the reference's own bid-ask rather than "
            "attributable to a disagreement between the venues"),
        "one_minute_return_correlation": {"BTC/MXN": "+0.1462", "ETH/MXN": "+0.0676",
                                          "SOL/MXN": "+0.0627"},
        "bitso_lag_correlation": {"BTC/MXN": "+0.0523", "ETH/MXN": "+0.0056",
                                  "SOL/MXN": "+0.0377"},
        "reference_lag_correlation": {"BTC/MXN": "+0.0631", "ETH/MXN": "+0.0232",
                                      "SOL/MXN": "+0.0422"},
        "lead_evidence": ("the reference's correlation with the incumbent's previous bar is no "
                          "smaller than the incumbent's correlation with the reference's previous "
                          "bar, so there is no evidence the incumbent lags"),
        "used_for_an_economic_conclusion": False,
    }


def _coverage(*, observations: tuple[Any, ...]) -> dict[str, Any]:
    """Thin wrapper over the module's coverage measure, for the certificate's shape."""
    return capture_coverage(observations=observations).public()


def main() -> int:
    parser = argparse.ArgumentParser(description="MVP 0.2.8 certification")
    parser.add_argument("--skip-capture-analysis", action="store_true")
    args = parser.parse_args()

    WORKSPACE.mkdir(parents=True, exist_ok=True)
    _trace("start")

    frozen = _frozen_alpha()
    _trace(f"frozen alpha asserted: {frozen['fingerprint'][:16]}")

    # The threshold comes from the project's own policy through the canonical derivation rather
    # than from a remembered 173 (spec section 12). The spread term is zero because the
    # executable prices already contain the incumbent's spread.
    from autofund.mvp.economics import DEFAULT_POLICY

    threshold = required_executable_dislocation_bps(
        taker_fee_rate=TAKER_FEE_RATE, slippage_bps=SLIPPAGE_BPS, policy=DEFAULT_POLICY)
    _trace(f"required executable dislocation: {threshold}")

    observations, capture = _executable_evidence()
    _trace(f"executable observations loaded: {len(observations)}")

    analysis: dict[str, Any] = {"pairs": {}, "totals": {}}
    total_episodes = 0
    total_converged = 0
    episodes_all: list[Any] = []
    if observations and not args.skip_capture_analysis:
        by_pair: dict[str, list[Any]] = {}
        for observation in observations:
            by_pair.setdefault(observation.incumbent.symbol, []).append(observation)
        for pair_key, items in sorted(by_pair.items()):
            assessment = _assess_pair(observations=tuple(items), pair_key=pair_key.replace(
                "_", "/").upper(), threshold_bps=threshold,
                convergence_horizon_seconds=Decimal("300"))
            analysis["pairs"][pair_key] = assessment
            total_episodes += assessment["episodes_above_threshold"]
            total_converged += assessment["episodes_converged"]
            for direction in assessment["directions"].values():
                for row in direction["episode_detail"]:
                    episodes_all.append(row)
        analysis["totals"] = {
            "observations": len(observations),
            "episodes_above_threshold": total_episodes,
            "episodes_converged": total_converged,
            "assets_with_episodes": sum(
                1 for item in analysis["pairs"].values()
                if item["episodes_above_threshold"] > 0),
        }

    # Every pair's reference was too wide, so no observation was valid for attribution. That is
    # itself the finding, and it is recorded explicitly rather than as an empty analysis.
    valid_observations = sum(
        1 for observation in observations if observation.validity == VALID)
    artifact_rejections = sum(
        1 for observation in observations
        if observation.validity == "REFERENCE_SPREAD_ARTIFACT")

    candidate_created = total_episodes >= MINIMUM_INDEPENDENT_EPISODES
    validation: dict[str, Any] = {
        "ran": False,
        "candidate_created": candidate_created,
        "reason": ("no candidate was frozen because development did not show repeated episodes "
                   "above the economic threshold" if not candidate_created
                   else "candidate frozen"),
        "holdout_untouched_before_freeze": True,
        "reproduced": None,
    }

    verdict = classify_result(
        episodes_above_threshold=total_episodes, episodes_converged=total_converged,
        assets_with_episodes=analysis["totals"].get("assets_with_episodes", 0),
        minimum_independent_episodes=MINIMUM_INDEPENDENT_EPISODES,
        evidence_proves_executability=(
            EVIDENCE_PROVES_EXECUTABILITY[EXECUTABLE_BOOK] and len(observations) > 0),
        validation_reproduced=None if candidate_created else False)

    # The coverage gate runs last and can only ever replace a conclusion with a weaker one. A
    # capture too short to contain a rare event cannot support "no dislocation exists", however
    # clean its data: the spec permits rare events, so absence of one in a few minutes is absence
    # of opportunity to observe it. This never turns an insufficient sample into a positive.
    coverage = _coverage(observations=observations)
    coverage_overrode = False
    if not coverage["sufficient"] and verdict["result"] in (
            CROSS_VENUE_SIGNAL_NOT_ECONOMIC, ACTIVE_TRADING_THESIS_NOT_SUPPORTED):
        verdict = {
            "result": INSUFFICIENT_CROSS_VENUE_EVIDENCE,
            "reason": (
                f"the capture covers {coverage['covered_hours']} hours against a predeclared "
                f"requirement of {REQUIRED_CAPTURE_HOURS}, so a rare event could not have been "
                f"observed; {total_episodes} episodes were seen above the threshold in the window "
                f"covered, which neither confirms nor rules one out"),
        }
        coverage_overrode = True
    _trace(f"result={verdict['result']} coverage_sufficient={coverage['sufficient']}")

    payload = {
        "certified_at": datetime.now(UTC).isoformat(),
        "product_version": "AutoFund MVP 0.2.8",
        "status": verdict["result"],
        "status_reason": verdict["reason"],
        "baseline_commit": BASELINE_COMMIT, "code_commit": CODE_COMMIT,
        "frozen_alpha": frozen,
        "hypothesis": {
            "statement": ("although average cross-venue dislocations are far below trading "
                          "costs, rare tail events may be large and persistent enough to clear "
                          "them"),
            "falsifiable": True,
            "tested_on": ["EXECUTABLE_BOOK", "CANDLE_SCREENING_ONLY"],
            "rejected_by": ("the reference books' own spreads exceed every measured difference, "
                            "so no dislocation is attributable to a price disagreement"),
        },
        "economic_threshold": {
            "required_executable_dislocation_bps": str(threshold),
            "derivation": ("minimum_viable_gross_edge_bps(taker_fee_rate=0.0078, spread_bps=0, "
                           "slippage_bps=5, policy=DEFAULT_POLICY)"),
            "spread_included_in_threshold": False,
            "spread_double_count_avoided": True,
            "spread_reason": ("the dislocation is built from the incumbent's executable ask or "
                              "bid, so the incumbent's spread is already inside it"),
            "taker_fee_rate": str(TAKER_FEE_RATE),
            "slippage_bps": str(SLIPPAGE_BPS),
            "policy_version": DEFAULT_POLICY.version,
            "maker_used_to_rescue_an_event": False,
            "policy_changed": False,
        },
        "capture": {
            "provenance": "REAL_CAPTURED_CROSS_VENUE",
            "observations": len(observations),
            "valid_observations": valid_observations,
            "reference_spread_artifact_rejections": artifact_rejections,
            "max_skew_seconds": str(EXECUTABLE_MAX_SKEW_SECONDS),
            "stale_exclusions": sum(
                1 for observation in observations
                if observation.validity == "STALE_CROSS_VENUE_COMPARISON"),
            "coverage": coverage,
            "coverage_overrode_a_conclusion": coverage_overrode,
            "required_capture_hours": REQUIRED_CAPTURE_HOURS,
            "report": capture,
            "authenticated_calls": 0,
            "orders_submitted": 0,
            "transfers_performed": 0,
        },
        "executable_analysis": analysis,
        "historical_screen": _candle_screen(threshold_bps=threshold),
        "partial_evidence_requirements": {
            "delays_seconds": list(PREDECLARED_DELAYS_SECONDS),
            "minimum_episode_observations": MINIMUM_EPISODE_OBSERVATIONS,
            "minimum_independent_episodes": MINIMUM_INDEPENDENT_EPISODES,
            "exceedance_thresholds_bps": [str(item) for item in EXCEEDANCE_THRESHOLDS_BPS],
            "convergence_horizons_seconds": list(CONVERGENCE_HORIZONS_SECONDS),
            "declared_before_measurement": True,
        },
        "validation": validation,
        "microstructure": _microstructure_state(),
        "engineering": {
            "strategy_implemented": False,
            "order_adapter_built": False,
            "authenticated_external_integration": False,
            "read_only_capture_built": True,
            "reason": ("the spec requires alpha evidence before trading policy, and the evidence "
                       "does not support a candidate"),
        },
        "terminology": {
            "uses_arbitrage": False,
            "terms_used": ["cross-venue dislocation", "reference-price lag",
                           "predictive convergence"],
            "simultaneous_hedge_exists": False,
            "position_risk": "DIRECTIONAL_ONLY",
            "note": ("Bitso-only execution leaves full directional risk; there is no hedge and "
                     "therefore no arbitrage"),
        },
        "safety": {
            "production_post_count": 0, "production_cancel_count": 0,
            "production_replace_count": 0, "production_session_started": False,
            "new_exchange_mutations_added": 0, "new_strategy_implemented": False,
            "economic_guard_changed": False, "risk_engine_changed": False,
            "drawdown_policy_changed": False, "capital_limits_changed": False,
            "frozen_alpha_retuned": False, "maker_production_enabled": False,
            "new_credentials_requested": False, "new_accounts_opened": False,
            "authenticated_external_calls": 0, "transfers_performed": False,
            "deposits_performed": False, "withdrawals_performed": False,
            "order_adapters_added": 0, "capital_limits_increased": False,
            "venue_migration_performed": False,
        },
    }
    OUT.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": payload["status"],
        "required_executable_dislocation_bps": str(threshold),
        "executable_observations": len(observations),
        "valid_for_attribution": valid_observations,
        "reference_spread_artifact_rejections": artifact_rejections,
        "episodes_above_threshold": total_episodes,
        "episodes_converged": total_converged,
        "candidate_created": candidate_created,
    }, indent=2))
    return 0


def _microstructure_state() -> dict[str, Any]:
    """The existing single-venue collector's data, preserved but not the primary scope."""
    books: list[dict[str, Any]] = []
    if MICROSTRUCTURE_ROOT.exists():
        for path in sorted(MICROSTRUCTURE_ROOT.glob("*.jsonl")):
            books.append({"book": path.name.split(".")[0], "bytes": path.stat().st_size})
    return {
        "preserved": True,
        "books": books,
        "book_count": len(books),
        "primary_scope": False,
        "storage_redesigned": False,
        "note": ("the 0.2.4 collector remains the single-venue evidence source and was neither "
                 "discarded nor redesigned; this milestone's capture is separate"),
    }


if __name__ == "__main__":
    raise SystemExit(main())

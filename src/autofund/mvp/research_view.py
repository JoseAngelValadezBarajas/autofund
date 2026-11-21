"""The Strategy Research view: what the research registry, evidence and gates say right now.

Read-only and off the trading path. It reports the profile registry, each profile's declared markets
and the current shadow evidence; it cannot activate a profile and never touches capital or the
Champion. The champion's own economic verdict is computed here so an operator can see *why* its BUYs
are refused at the real fee, rather than inferring it.

This used to be one 525-line method on the orchestrator. It is now one function per research
generation, each returning its own section, so a milestone's declarations can be read, tested and
changed without scrolling through the others. The assembled output is byte-identical.
"""

from decimal import Decimal
from typing import Any

from .alpha_controls import ALPHA_CONTROLS
from .alpha_discovery import (
    FROZEN_PRICE_ONLY_PROFILES,
    INSUFFICIENT_EVIDENCE,
    INSUFFICIENT_SAMPLE,
    MAX_FEATURE_FAMILIES,
    MAX_RELATIONSHIPS,
    MICROSTRUCTURE_ACCUMULATING,
    NO_SIGNAL,
    NOT_PREDICTIVE,
    PREDECLARED_HORIZONS_MINUTES,
    PREDICTIVE,
    PREDICTIVE_NOT_ECONOMIC,
    PRICE_ONLY_RESEARCH_BASELINE,
    TERMINAL_STATUSES,
    VALIDATED_ALPHA_SOURCE,
    WEAK_UNSTABLE_SIGNAL,
)
from .asymmetric_challengers import (
    asymmetric_challenger_ids,
    asymmetric_challengers,
)
from .asymmetry_experiment import (
    COMPARISON_SEMANTICS,
    MAX_CONFIGURATIONS_PER_CHALLENGER,
    configuration_budget,
    threshold_basis_text,
)
from .asymmetry_experiment import (
    SELECTION_RULE as ASYMMETRY_SELECTION_RULE,
)
from .asymmetry_gate import (
    PREDECLARED_REWARD_RISK_THRESHOLDS,
    friction_bps_for,
)
from .challenger_research import CONFIG_SETS, SELECTION_RULE
from .cross_market import FEATURE_INTERPRETATION, PREDECLARED_FEATURES
from .cross_market import (
    PREDECLARED_RELATIONSHIPS as CROSS_MARKET_RELATIONSHIPS,
)
from .cross_venue_dislocation import (
    DISLOCATION_KINDS,
    EVIDENCE_CLASSES,
    MECHANISMS,
    MINIMUM_EPISODE_OBSERVATIONS,
    PREDECLARED_DELAYS_SECONDS,
    required_executable_dislocation_bps,
)
from .cross_venue_dislocation import (
    REQUIRED_CAPTURE_HOURS as CROSS_VENUE_REQUIRED_CAPTURE_HOURS,
)
from .economics import DEFAULT_POLICY
from .execution_gap import MISSING, PARTIAL, PRESENT, architecture_gap
from .horizon import PREDECLARED_TIMEFRAMES
from .horizon_experiment import (
    CERTIFYING_EXECUTION_MODE,
    HORIZON_OUTCOMES,
    MAX_CONFIGURATIONS_PER_HORIZON,
    predeclared_configuration_budget,
)
from .horizon_profiles import horizon_profile_ids, horizon_profiles
from .microstructure import (
    DEFAULT_RETENTION_HOURS,
    PASSIVE_FILL_EXACTNESS,
    QUEUE_POSITION_OBSERVABLE,
    REAL_CAPTURED_MICROSTRUCTURE,
    STOPPED,
)
from .microstructure_alpha import PREDECLARED_MARKOUT_SECONDS
from .passive_execution import (
    CANDLE_ONLY_UNCERTAIN,
    execution_modes,
    fee_floors,
)
from .profile_library import (
    EVALUATORS,
    FROZEN_PROFILES,
    PROFILE_BY_ID,
    PROFILE_REGISTRY,
    RISK_ADJUSTED_CHALLENGERS,
)
from .profiles import PROMOTION, classify_market
from .research import SELECTOR_CONTRACT_VERSION
from .scanner import MarketScanner
from .session_types import ACCOUNT_CONFIRMED_TAKER_RATE
from .venue_data import (
    ESTABLISHED_FEE_FLOORS_BPS,
    VENUE_DATA_VERSION,
    venue_profiles,
    venue_schedules,
)
from .venue_data import (
    RETRIEVED_AT as VENUE_DATA_RETRIEVED_AT,
)
from .venue_economics import (
    ACCOUNT_RATE_UNKNOWN,
    AUTHORIZED_CAPITAL_MXN,
    CAPTURE_SCENARIOS,
    CLEARLY_NOT_ECONOMIC,
    COMPATIBILITY_UNKNOWN,
    COMPATIBLE_WITH_CURRENT_EXPERIMENT,
    EFFORT_HIGH,
    EFFORT_LOW,
    EFFORT_MEDIUM,
    EXECUTION_MODES,
    FEE_CONVENTIONS,
    FROZEN_ALPHA_FINGERPRINT,
    FROZEN_ALPHA_MOVEMENT_BPS,
    INSUFFICIENT_COST_EVIDENCE,
    MAX_DEPLOYMENT_MXN,
    MAX_SINGLE_ORDER_MXN,
    NOT_COMPATIBLE_WITH_CURRENT_EXPERIMENT,
    POTENTIALLY_ECONOMIC,
    STRUCTURALLY_UNTRADEABLE_FOR_THIS_ALPHA,
    THEORETICALLY_POSSIBLE_ONLY_UNDER_PASSIVE_EXECUTION,
    THESIS_CLASSIFICATIONS,
    VALIDATED_INFORMATION_SIGNAL_V1,
    historical_friction_bps,
)
from .viability import minimum_viable_gross_edge_bps


def _risk_adjusted_research() -> dict[str, Any]:
    return {
        "selection_rule": list(SELECTION_RULE),
        "configuration_sets": {key: len(value)
                               for key, value in CONFIG_SETS.items()},
        "mae_mfe_implemented": True,
        "markets_pooled": False,
        "holdout_frozen_before_selection": True,
        "drawdown_policy_changed": False,
        "capital_limits_changed": False,
    }


# Passive execution research (MVP 0.2.3). The fee floors are structural and
# answerable from the account schedule alone; the fill question is separate and
# is NOT answered, which is why the evidence section reports the weakest class.
def _execution_research(taker_fee: Decimal, maker_fee: Decimal | None) -> dict[str, Any]:
    return {
        "maker_fee_rate": (None if maker_fee is None else str(maker_fee)),
        "taker_fee_rate": str(taker_fee),
        "maker_fee_confirmed": maker_fee is not None and maker_fee > Decimal("0"),
        "maker_below_taker": (maker_fee is not None and maker_fee < taker_fee),
        "modes": {mode: definition.public()
                  for mode, definition in execution_modes().items()},
        "fee_floors": {
            mode: str(floor.fee_only_round_trip_bps)
            for mode, floor in fee_floors(
                maker_rate=maker_fee if maker_fee is not None else taker_fee,
                taker_rate=taker_fee).items()},
        "fill_evidence": CANDLE_ONLY_UNCERTAIN,
        "maker_fills_determined": False,
        "research_only": True,
        "production_authorised": False,
        "cancel_implemented": False,
        "production_mutations_added": 0,
    }


# Time-horizon research (MVP 0.2.4). Reported as a *hypothesis under test*, not
# as a finding: the verdict comes from a frozen experiment evaluated on real
# market data, and no result exists until that runs. What is stated here is what
# was predeclared and what is structurally true regardless of the outcome.
def _horizon_research() -> dict[str, Any]:
    return {
        "hypothesis": ("at current trading costs, a coarser horizon may make "
                       "transaction friction a smaller fraction of the expected "
                       "opportunity while keeping risk within policy"),
        "predeclared_timeframes": [spec.name for spec in PREDECLARED_TIMEFRAMES],
        "horizons_tested_by_rule": len(PREDECLARED_TIMEFRAMES) == 2,
        "profiles": [profile.public() for profile in horizon_profiles()],
        "profile_ids": list(horizon_profile_ids()),
        "max_configurations_per_strategy_and_horizon": MAX_CONFIGURATIONS_PER_HORIZON,
        "configuration_budget": {key: count for key, count in
                                 predeclared_configuration_budget()},
        "selection_rule": list(SELECTION_RULE),
        "all_configurations_retained": True,
        "certifying_execution_mode": CERTIFYING_EXECUTION_MODE,
        "maker_bound_may_certify": False,
        "base_bar_seconds": 60,
        "aggregation_uses_existing_base_bars": True,
        "incomplete_buckets_dropped_not_filled": True,
        "same_bar_fills_allowed": False,
        "outcomes": list(HORIZON_OUTCOMES),
        "verdict": None,
        "verdict_pending_frozen_experiment": True,
        "frozen_profile_fingerprints_changed": False,
        "drawdown_policy_changed": False,
        "capital_limits_changed": False,
    }


# Forward microstructure capture (MVP 0.2.4). The evidence is collected, not
# assumed, and the collector cannot trade. Queue position and exact passive fill
# remain unknowable from public data, and saying so is the point.
def _microstructure_capture() -> dict[str, Any]:
    return {
        "enabled": False,
        "provenance": REAL_CAPTURED_MICROSTRUCTURE,
        "endpoints": ["order_book", "trades"],
        "order_capability": "NONE",
        "production_mutation": "NONE",
        "credentials_scope": "READ_ONLY_PUBLIC_ENDPOINTS",
        "separate_from_production_ledger": True,
        "captured_books": [],
        "order_book_events": 0,
        "trade_tape_events": 0,
        "capture_seconds": 0,
        "health": STOPPED,
        "gaps": 0,
        "duplicates": 0,
        "sequence_regressions": 0,
        "anomalies_repaired": 0,
        "bounded_storage": True,
        "retention_hours": DEFAULT_RETENTION_HOURS,
        "queue_position_observable": QUEUE_POSITION_OBSERVABLE,
        "passive_fill_exactness": PASSIVE_FILL_EXACTNESS,
        "adverse_selection_analysis_possible": False,
        "exact_passive_fill_claim_made": False,
    }


# Asymmetric opportunity research (MVP 0.2.5). The gate below is the weakest of
# three and states so: it can only ever refuse, never admit what the economic
# guard or the risk policy has already refused.
def _asymmetry_research(taker_fee: Decimal) -> dict[str, Any]:
    return {
        "hypothesis": ("entries placed near a genuine invalidation boundary may "
                       "improve reward/risk geometry enough to clear friction"),
        "challengers": [profile.public() for profile in asymmetric_challengers()],
        "challenger_ids": list(asymmetric_challenger_ids()),
        "max_configurations_per_challenger": MAX_CONFIGURATIONS_PER_CHALLENGER,
        "configuration_budget": dict(configuration_budget()),
        "reward_risk_thresholds": [str(t)
                                   for t in PREDECLARED_REWARD_RISK_THRESHOLDS],
        "threshold_basis": threshold_basis_text(
            friction_bps=friction_bps_for(taker_fee_rate=taker_fee,
                                          spread_bps=Decimal("12"),
                                          slippage_bps=Decimal("5")),
            max_risk_bps=Decimal("281.54545454545454545454545454545454545454545454545")),
        "selection_rule": list(ASYMMETRY_SELECTION_RULE),
        "comparison_semantics": list(COMPARISON_SEMANTICS),
        "all_configurations_retained": True,
        "thresholds_chosen_before_results": True,
        "certifying_execution_mode": "TAKER_TAKER",
        "maker_bound_computed": False,
        "maker_bound_reason": ("maker fills are unobservable from candle history and "
                               "may not make a challenger pass"),
        "gate_is_research_only": True,
        "gate_can_override_economic_guard": False,
        "gate_can_override_risk_engine": False,
        "gate_can_override_risk_policy": False,
        "no_future_structure": True,
        "verdict": None,
        "verdict_pending_frozen_experiment": True,
        "drawdown_policy_changed": False,
        "risk_engine_changed": False,
        "capital_limits_changed": False,
    }


# Independent alpha-source research (MVP 0.2.6). The question changed here: not
# whether another indicator helps, but whether *any* independent information
# source exists at all. Alpha means information with predictive content, not
# profit, and the two are reported separately so a real signal too small to pay
# 173 bps of friction cannot be mistaken for an edge or hidden by one.
def _alpha_research() -> dict[str, Any]:
    return {
        "question": ("does an independent information source exist with measurable "
                     "predictive content, before another strategy is written"),
        "alpha_is_information_not_pnl": True,
        "price_only_baseline_label": PRICE_ONLY_RESEARCH_BASELINE,
        "price_only_baseline_frozen": True,
        "frozen_price_only_profiles": len(FROZEN_PRICE_ONLY_PROFILES),
        "new_price_only_strategy_implemented": False,
        "source_families": ["CROSS_MARKET_LEAD_LAG",
                            "MARKET_MICROSTRUCTURE_ORDER_FLOW"],
        "predeclared_features": list(PREDECLARED_FEATURES),
        "feature_interpretation": {name: FEATURE_INTERPRETATION[name]
                                   for name in PREDECLARED_FEATURES},
        "predeclared_relationships": [list(pair)
                                      for pair in CROSS_MARKET_RELATIONSHIPS],
        "predeclared_horizons_minutes": list(PREDECLARED_HORIZONS_MINUTES),
        "predeclared_markout_seconds": list(PREDECLARED_MARKOUT_SECONDS),
        "max_feature_families": MAX_FEATURE_FAMILIES,
        "max_relationships": MAX_RELATIONSHIPS,
        "parameters_declared_before_results": True,
        "multiple_testing_budget_enforced": True,
        "negative_controls": list(ALPHA_CONTROLS),
        "negative_controls_run_on_every_measurement": True,
        "positive_control_required_for_a_verdict": True,
        "undetectable_measurements_reported_as_unmeasurable": True,
        "verdicts": [PREDICTIVE, NOT_PREDICTIVE, INSUFFICIENT_SAMPLE],
        "classifications": [NO_SIGNAL, WEAK_UNSTABLE_SIGNAL,
                            PREDICTIVE_NOT_ECONOMIC, VALIDATED_ALPHA_SOURCE,
                            MICROSTRUCTURE_ACCUMULATING, INSUFFICIENT_EVIDENCE],
        "terminal_statuses": list(TERMINAL_STATUSES),
        "development_only_during_discovery": True,
        "validation_read_only_after_freeze": True,
        "holdout_preserved": True,
        "holdout_consumed": False,
        "no_future_structure": True,
        "discovery_pipeline_may_report_absence": True,
        "gate_can_override_economic_guard": False,
        "gate_can_override_risk_engine": False,
        "gate_can_override_risk_policy": False,
        "new_strategy_implemented": False,
        "maker_authorised": False,
        "verdict": None,
        "verdict_pending_frozen_experiment": True,
        "drawdown_policy_changed": False,
        "risk_engine_changed": False,
        "capital_limits_changed": False,
    }


# Venue economics and product-thesis feasibility (MVP 0.2.7). A DECISION milestone:
# it reports whether any realistically available retail venue could collect the
# validated signal, and it deliberately builds no strategy, adapter or credential
# path. The cost arithmetic is stated as a requirement rather than a permission, so a
# reader cannot mistake a budget for an authorisation.
def _venue_economics() -> dict[str, Any]:
    return {
        "question": ("is AutoFund's product thesis economically feasible under any "
                     "realistic execution environment available to a small retail "
                     "spot-trading system"),
        "is_a_decision_milestone": True,
        "builds_a_strategy": False,
        "builds_a_venue_adapter": False,
        "frozen_alpha_label": VALIDATED_INFORMATION_SIGNAL_V1,
        "frozen_alpha_fingerprint": FROZEN_ALPHA_FINGERPRINT,
        "frozen_alpha_movement_bps": str(FROZEN_ALPHA_MOVEMENT_BPS),
        "frozen_alpha_retuned_for_venue_economics": False,
        "capture_scenarios": [label for label, _ in CAPTURE_SCENARIOS],
        "execution_modes": list(EXECUTION_MODES),
        "verdicts": [CLEARLY_NOT_ECONOMIC,
                     THEORETICALLY_POSSIBLE_ONLY_UNDER_PASSIVE_EXECUTION,
                     POTENTIALLY_ECONOMIC, INSUFFICIENT_COST_EVIDENCE],
        "early_fail_classification": STRUCTURALLY_UNTRADEABLE_FOR_THIS_ALPHA,
        "feasibility_classifications": list(THESIS_CLASSIFICATIONS),
        "venue_count": len(venue_profiles()),
        "venues": [profile.public() for profile in venue_profiles()],
        "referenced_venue_data": {
            "version": VENUE_DATA_VERSION,
            "retrieved_at": VENUE_DATA_RETRIEVED_AT,
            "sources": [schedule.public() for schedule in venue_schedules()],
            "account_rate_unknown_venues": [
                schedule.venue for schedule in venue_schedules()
                if schedule.basis == ACCOUNT_RATE_UNKNOWN],
        },
        "established_fee_floors_bps": dict(ESTABLISHED_FEE_FLOORS_BPS),
        "fee_conventions_available": list(FEE_CONVENTIONS),
        "fee_convention_note": (
            "the project holds two fee-doubling conventions that differ by about 1.8 bps; "
            "the fee-only floors use the compounding form and the historical 173 bps "
            "all-in uses the doubled form, and both are reproduced exactly"),
        "historical_friction_bps": str(historical_friction_bps()),
        "authorized_capital_mxn": str(AUTHORIZED_CAPITAL_MXN),
        "max_deployment_mxn": str(MAX_DEPLOYMENT_MXN),
        "max_single_order_mxn": str(MAX_SINGLE_ORDER_MXN),
        "minimum_order_compatibility_values": [
            COMPATIBLE_WITH_CURRENT_EXPERIMENT,
            NOT_COMPATIBLE_WITH_CURRENT_EXPERIMENT, COMPATIBILITY_UNKNOWN],
        "capital_limits_increased_to_qualify_a_venue": False,
        "migration_effort_levels": [EFFORT_LOW, EFFORT_MEDIUM, EFFORT_HIGH],
        "migration_implemented": False,
        "credentials_requested": False,
        "accounts_opened": False,
        "transfers_designed": False,
        "transfer_arbitrage_designed": False,
        "is_arbitrage_claim": False,
        "cross_venue_same_quote_currency_only": True,
        "reference_data_adapter_built": False,
        "gate_can_override_economic_guard": False,
        "gate_can_override_risk_engine": False,
        "gate_can_override_risk_policy": False,
        "verdict": None,
        "verdict_pending_frozen_experiment": True,
        "economic_guard_changed": False,
        "risk_engine_changed": False,
        "drawdown_policy_changed": False,
        "capital_limits_changed": False,
    }


# Cross-venue rare dislocation research (MVP 0.2.8). The last alpha-source experiment
# under the current product assumptions. It reports that the apparent dislocation is a
# property of the reference book's width rather than a disagreement between venues,
# and it states explicitly that its own capture is too short to conclude either way.
def _cross_venue_research(taker_fee: Decimal) -> dict[str, Any]:
    return {
        "hypothesis": ("although average cross-venue dislocations are far below trading "
                       "costs, rare tail events may be large and persistent enough to "
                       "clear them"),
        "reference_venues": ["Binance"],
        "reference_coverage": ["BTC/MXN", "ETH/MXN", "SOL/MXN"],
        "pairs_without_usable_reference": ["XRP/MXN"],
        "pairs_without_reference_reason": ("Binance's XRP/MXN pair was not trading during "
                                           "development, so no reference exists"),
        "same_quote_only": True,
        "fx_conversion_used": False,
        "execution_venue": "Bitso",
        "external_venues_are_reference_only": True,
        "evidence_classes": list(EVIDENCE_CLASSES),
        "candle_screening_proves_executability": False,
        "reference_spread_guard": True,
        "reference_spread_artifact_is_rejected": True,
        "required_executable_dislocation_bps": str(
            required_executable_dislocation_bps(
                taker_fee_rate=(
                    taker_fee if taker_fee > Decimal("0")
                    else ACCOUNT_CONFIRMED_TAKER_RATE),
                slippage_bps=Decimal("5"), policy=DEFAULT_POLICY)),
        "fee_used_for_threshold": str(
            taker_fee if taker_fee > Decimal("0")
            else ACCOUNT_CONFIRMED_TAKER_RATE),
        "fee_source": ("RUNNER" if taker_fee > Decimal("0")
                       else "ACCOUNT_CONFIRMED_SCHEDULE"),
        "threshold_derivation": ("minimum_viable_gross_edge_bps(taker_fee_rate=0.0078, "
                                 "spread_bps=0, slippage_bps=5, policy=DEFAULT_POLICY)"),
        "spread_double_count_avoided": True,
        "maker_used_to_rescue_an_event": False,
        "dislocation_kinds": list(DISLOCATION_KINDS),
        "mechanisms": list(MECHANISMS),
        "uses_arbitrage_terminology": False,
        "simultaneous_hedge_exists": False,
        "position_risk": "DIRECTIONAL_ONLY",
        "predeclared_delays_seconds": list(PREDECLARED_DELAYS_SECONDS),
        "minimum_episode_observations": MINIMUM_EPISODE_OBSERVATIONS,
        "required_capture_hours": CROSS_VENUE_REQUIRED_CAPTURE_HOURS,
        "rare_events_are_acceptable": True,
        "single_anomaly_cannot_be_a_candidate": True,
        "development_only": True,
        "holdout_untouched_before_freeze": True,
        "candidate_created": False,
        "candidate_fingerprint": None,
        "validation_ran": False,
        "capture_is_read_only": True,
        "authenticated_external_calls": 0,
        "transfers_designed": False,
        "strategy_implemented": False,
        "gate_can_override_economic_guard": False,
        "gate_can_override_risk_engine": False,
        "gate_can_override_risk_policy": False,
        "verdict": None,
        "verdict_pending_frozen_experiment": True,
        "economic_guard_changed": False,
        "risk_engine_changed": False,
        "drawdown_policy_changed": False,
        "capital_limits_changed": False,
    }


def _production_gap() -> dict[str, Any]:
    return {
        "missing": [item.name for item in architecture_gap()
                    if item.state == MISSING],
        "partial": [item.name for item in architecture_gap()
                    if item.state == PARTIAL],
        "present": [item.name for item in architecture_gap()
                    if item.state == PRESENT],
        "production_mutations_required": [
            item.name for item in architecture_gap()
            if item.production_mutation_required],
    }


def build_strategy_research_view(*, runner: Any, adaptive: Any,
                                 scanner: MarketScanner | None) -> dict[str, Any]:
    """Assemble every section. ``runner`` supplies the account fee, ``adaptive`` the champion."""
    # Profiles that declare an invalidation price and a holding limit. Read from each
    # profile's own explicit constant rather than probed with a synthetic proposal: a
    # probe with empty candles returns an early no-signal proposal that declares
    # nothing, which would report a boundary-bounded profile as boundary-less.
    RISK_BOUNDED_PROFILE_IDS = {
        definition.profile_id for definition in RISK_ADJUSTED_CHALLENGERS
        if getattr(EVALUATORS.get(definition.evaluator), "DECLARES_RISK_BOUNDARY",
                   False)
    }

    taker_fee = getattr(runner, "_taker_fee_rate", Decimal("0"))
    # The maker rate is only meaningful when the account actually confirmed one. When
    # it is unknown, the research view says so rather than substituting the taker rate,
    # because an unconfirmed maker rate is exactly the assumption this milestone must
    # not make.
    maker_fee = getattr(runner, "_maker_fee_rate", None)
    preflight = getattr(runner, "last_preflight", None)
    spread_bps = Decimal("0")
    if preflight is not None:
        try:
            spread_bps = preflight.depth.spread_bps
        except Exception:
            spread_bps = Decimal("0")
    floor_bps = (minimum_viable_gross_edge_bps(taker_fee_rate=taker_fee,
                                              spread_bps=spread_bps,
                                              policy=DEFAULT_POLICY)
                 if taker_fee > Decimal("0") else None)
    profiles: list[dict[str, Any]] = []
    for definition in PROFILE_REGISTRY:
        profiles.append({
            **definition.public(),
            "markets_certified": sorted(definition.markets),
            "lifecycle": ("ACTIVE_PRODUCTION"
                          if definition.markets == frozenset({"btc_mxn"})
                          else "RESEARCH"),
        })
    champion = adaptive.champion
    champion_intended_bps = Decimal("20")
    return {
        "research_version": SELECTOR_CONTRACT_VERSION,
        "promotion": PROMOTION,
        "multi_market_production": "DISABLED",
        "champion": {"profile_id": champion.profile_id, "strategy_id": champion.strategy_id,
                    "version": champion.version, "fingerprint": champion.fingerprint,
                    "certification_status": champion.certification_status,
                    "lifecycle": "ACTIVE_PRODUCTION",
                    "intended_gross_edge_bps": str(champion_intended_bps)},
        "profiles": profiles,
        "economic_viability": {
            "taker_fee_rate": str(taker_fee),
            "spread_bps": str(spread_bps),
            "account_fee_confirmed": taker_fee > Decimal("0"),
            "minimum_viable_gross_edge_bps": None if floor_bps is None else str(floor_bps),
            "champion_intended_gross_edge_bps": str(champion_intended_bps),
            "champion_economically_viable": (None if floor_bps is None
                                             else champion_intended_bps > floor_bps),
        },
        "markets": [{"market": item.book, "status": item.status,
                     "strategy_compatibility": item.strategy_compatibility,
                     "market_class": classify_market(item.book.replace("_", "/").upper()),
                     "lifecycle": item.lifecycle}
                    for item in (scanner.candidates if scanner else ())],
        "shadow": list(scanner.shadow.values()) if scanner is not None else [],
        "profile_by_id": sorted(PROFILE_BY_ID),
        # Research generation (MVP 0.2.2). The two sets are reported separately so a
        # reader can see which profiles are frozen evidence and which are live
        # challengers, and cannot mistake a new challenger for validated work.
        "frozen_profiles": sorted(d.profile_id for d in FROZEN_PROFILES),
        "risk_adjusted_challengers": [
            {**d.public(), "markets_certified": sorted(d.markets),
             "risk_adjusted_metrics": True,
             "declares_invalidation": d.profile_id in RISK_BOUNDED_PROFILE_IDS,
             "lifecycle": ("ACTIVE_PRODUCTION" if d.markets == frozenset({"btc_mxn"})
                           else "RESEARCH")}
            for d in RISK_ADJUSTED_CHALLENGERS],
        "risk_adjusted_research": _risk_adjusted_research(),
        "execution_research": _execution_research(taker_fee, maker_fee),
        "horizon_research": _horizon_research(),
        "microstructure_capture": _microstructure_capture(),
        "asymmetry_research": _asymmetry_research(taker_fee),
        "alpha_research": _alpha_research(),
        "venue_economics": _venue_economics(),
        "cross_venue_research": _cross_venue_research(taker_fee),
        "production_gap": _production_gap(),
    }

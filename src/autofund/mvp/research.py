"""Strategy research: profile x market evidence, and the certification gate.

This module answers, for each (profile, market) pair on real or fixture candles:

    did this profile find an opportunity, was it economically admissible, and did
    it make money after friction?

and then decides whether the evidence supports certification. Two rules keep that
honest:

**Certification is not "total P&L was positive".** A profile can be lucky on a tiny
sample, or profitable on one trade while losing on ten. The gate requires
sufficient observations, sufficient evaluations, a non-zero opportunity count,
adequate data quality, economic feasibility, bounded drawdown, deterministic
replay, no lookahead, and execution compatibility -- each checked explicitly and
each reported separately so a failure is attributable.

**Too little evidence is a valid, first-class answer.** ``INSUFFICIENT_EVIDENCE`` is
returned rather than a confident-looking number. Nothing here manufactures
confidence, and nothing here promotes anything: ``PROMOTION`` stays DISABLED and
the Champion keeps Production.

The output is also the contract MVP 0.2's market selector will consume, so every
field a selector needs is present and fingerprinted.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial
from autofund.replay.serialization import fingerprint

from .economics import EconomicPolicy
from .historical import CandleSeries
from .profile_library import PROFILE_BY_ID, PROFILE_REGISTRY, evaluator_for
from .profiles import (
    CLASS_STABLE_OR_FIAT,
    COMPATIBLE,
    INCOMPATIBLE,
    INSUFFICIENT_EVIDENCE,
    NOT_VIABLE,
    PRODUCTION_CERTIFIABLE,
    PROMOTION,
    RESEARCH_ONLY,
    classify_market,
    market_compatibility,
)
from .replay_eval import (
    DEFAULT_MIN_EVALUATIONS,
    DEFAULT_MIN_ROUND_TRIPS,
    ProfileReplayResult,
    replay_candles,
    verify_no_lookahead,
)

RESEARCH_VERSION = "autofund.strategy-research.v1"
SELECTOR_CONTRACT_VERSION = "autofund.production-market-selector.v1"


@dataclass(frozen=True, slots=True)
class EvidenceRequirements:
    """Configurable evidence thresholds. Every one is explicit and reportable.

    Defaults are deliberately demanding. A single favourable session cannot certify
    anything, and it should not be possible to certify a profile by accident.
    """

    min_candles: int = 60
    min_evaluations: int = DEFAULT_MIN_EVALUATIONS
    min_round_trips: int = DEFAULT_MIN_ROUND_TRIPS
    min_opportunities: int = 1
    max_drawdown_mxn: Decimal = Decimal("0.50")
    min_median_net_edge_bps: Decimal = ZERO
    require_positive_net_pnl: bool = True
    require_no_lookahead: bool = True
    require_execution_compatible: bool = True
    require_data_quality: bool = True

    def public(self) -> dict[str, str]:
        return {"min_candles": str(self.min_candles), "min_evaluations": str(self.min_evaluations),
                "min_round_trips": str(self.min_round_trips),
                "min_opportunities": str(self.min_opportunities),
                "max_drawdown_mxn": str(self.max_drawdown_mxn),
                "min_median_net_edge_bps": str(self.min_median_net_edge_bps),
                "require_positive_net_pnl": str(self.require_positive_net_pnl),
                "require_no_lookahead": str(self.require_no_lookahead),
                "require_execution_compatible": str(self.require_execution_compatible),
                "require_data_quality": str(self.require_data_quality)}


DEFAULT_REQUIREMENTS = EvidenceRequirements()

# Named evidence checks. A certification failure always names which check failed.
CHECK_CANDLES = "SUFFICIENT_CANDLES"
CHECK_EVALUATIONS = "SUFFICIENT_EVALUATIONS"
CHECK_ROUND_TRIPS = "SUFFICIENT_ROUND_TRIPS"
CHECK_OPPORTUNITY = "NONZERO_OPPORTUNITY_COUNT"
CHECK_DATA_QUALITY = "DATA_QUALITY_PASS"
CHECK_ECONOMIC = "ECONOMIC_FEASIBILITY"
CHECK_DRAWDOWN = "BOUNDED_DRAWDOWN"
CHECK_DETERMINISM = "REPLAY_DETERMINISM"
CHECK_LOOKAHEAD = "NO_LOOKAHEAD"
CHECK_EXECUTION = "EXECUTION_COMPATIBILITY"
CHECK_NET_PNL = "POSITIVE_NET_PNL"
CHECK_MEDIAN_EDGE = "MEDIAN_NET_EDGE_ABOVE_FLOOR"

ALL_CHECKS = (CHECK_CANDLES, CHECK_EVALUATIONS, CHECK_ROUND_TRIPS, CHECK_OPPORTUNITY,
              CHECK_DATA_QUALITY, CHECK_ECONOMIC, CHECK_DRAWDOWN, CHECK_DETERMINISM,
              CHECK_LOOKAHEAD, CHECK_EXECUTION, CHECK_NET_PNL, CHECK_MEDIAN_EDGE)


@dataclass(frozen=True, slots=True)
class CertificationVerdict:
    """Result of the evidence gate, with every check individually reported."""

    status: str
    passed: tuple[str, ...]
    failed: tuple[str, ...]
    requirements: EvidenceRequirements

    @property
    def certified(self) -> bool:
        return self.status == PRODUCTION_CERTIFIABLE

    def telemetry(self) -> dict[str, Any]:
        return {"status": self.status, "certified": self.certified,
                "passed": list(self.passed), "failed": list(self.failed),
                "requirements": self.requirements.public(),
                "promotion": PROMOTION}


@financial
def certify(*, replay: ProfileReplayResult, lookahead_ok: bool, deterministic: bool,
            requirements: EvidenceRequirements = DEFAULT_REQUIREMENTS,
            market_class: str = "VOLATILE_CRYPTO") -> CertificationVerdict:
    """Apply the evidence gate. Certification is earned, never assumed."""
    passed: list[str] = []
    failed: list[str] = []

    def check(name: str, condition: bool) -> None:
        (passed if condition else failed).append(name)

    percentiles = replay.net_edge_percentiles
    median = percentiles.p50
    check(CHECK_CANDLES, replay.candles >= requirements.min_candles)
    check(CHECK_EVALUATIONS, replay.evaluations >= requirements.min_evaluations)
    check(CHECK_ROUND_TRIPS, len(replay.round_trips) >= requirements.min_round_trips)
    check(CHECK_OPPORTUNITY, replay.buy_signals >= requirements.min_opportunities)
    # A fiat-like book is excluded from volatile-crypto research by classification,
    # so it can never satisfy the data-quality requirement for these profiles.
    check(CHECK_DATA_QUALITY, market_class != CLASS_STABLE_OR_FIAT)
    # Economic feasibility: at least one opportunity had to be admissible, otherwise
    # the profile has demonstrated only that friction exceeds its target.
    check(CHECK_ECONOMIC, replay.economic_admissions > 0
          and replay.status not in (NOT_VIABLE, INSUFFICIENT_EVIDENCE))
    check(CHECK_DRAWDOWN, replay.max_drawdown_mxn <= requirements.max_drawdown_mxn)
    check(CHECK_DETERMINISM, deterministic)
    check(CHECK_LOOKAHEAD, lookahead_ok or not requirements.require_no_lookahead)
    check(CHECK_EXECUTION, replay.compatibility == COMPATIBLE
          or not requirements.require_execution_compatible)
    check(CHECK_NET_PNL, (replay.net_pnl_mxn > ZERO) if requirements.require_positive_net_pnl else True)
    check(CHECK_MEDIAN_EDGE, median is not None and median >= requirements.min_median_net_edge_bps)

    if failed:
        # A demonstrated economic failure is a stronger, better-founded statement than
        # "not enough evidence". When the sample is large enough to have observed the
        # failure -- real opportunities were proposed and the guard refused every one
        # on economics -- the profile is NOT_VIABLE, not merely under-evidenced.
        # Otherwise an evidence shortfall is reported as exactly that.
        demonstrated_economic_failure = (
            replay.evaluations >= requirements.min_evaluations
            and replay.buy_signals >= requirements.min_opportunities
            and replay.economic_admissions == 0
            and replay.economic_rejections > 0)
        if demonstrated_economic_failure:
            status = NOT_VIABLE
        else:
            evidence_shortfall = {CHECK_CANDLES, CHECK_EVALUATIONS, CHECK_ROUND_TRIPS,
                                  CHECK_OPPORTUNITY, CHECK_DETERMINISM, CHECK_LOOKAHEAD}
            status = INSUFFICIENT_EVIDENCE if evidence_shortfall & set(failed) else NOT_VIABLE
    else:
        status = PRODUCTION_CERTIFIABLE
    return CertificationVerdict(status=status, passed=tuple(passed), failed=tuple(failed),
                                requirements=requirements)


@dataclass(frozen=True, slots=True)
class ProfileMarketEvidence:
    """One (profile, market) evaluation with its verdict and selector contract."""

    market: str
    market_class: str
    profile_id: str
    profile_fingerprint: str
    strategy_fingerprint: str
    profile_contract: dict[str, Any]
    compatibility: str
    replay: ProfileReplayResult
    verdict: CertificationVerdict
    lookahead_ok: bool
    lookahead_detail: str
    deterministic: bool
    data_source: str
    dataset_fingerprint: str
    notes: tuple[str, ...] = ()

    def selector_contract(self) -> dict[str, Any]:
        """MVP 0.2 contract: everything a market selector needs, and its fingerprint.

        Deliberately complete and self-describing so the selector never has to
        re-derive economics or re-read a strategy. Multi-market Production remains
        disabled in this milestone; this is preparation, not activation.
        """
        gross = self.replay.intended_gross_edge_bps.p50
        friction = self.replay.round_trip_friction_bps.p50
        net = self.replay.net_edge_percentiles.p50
        return {
            "version": SELECTOR_CONTRACT_VERSION, "market": self.market,
            "market_class": self.market_class, "profile_id": self.profile_id,
            "profile_fingerprint": self.profile_fingerprint,
            "strategy_fingerprint": self.strategy_fingerprint,
            "market_certification_status": self.verdict.status,
            "strategy_compatibility": self.compatibility,
            "current_strategy_decision": (self.replay.round_trips[-1].exit_reason
                                          if self.replay.round_trips else "NO_SIGNAL"),
            # The latest observed intention is a fact and is always reported; the
            # percentiles below are statistics and are None until the sample supports
            # them. A selector needs the fact even when the distribution is thin.
            "latest_intended_gross_edge_bps": (
                None if self.replay.latest_intended_gross_edge_bps is None
                else str(self.replay.latest_intended_gross_edge_bps)),
            "latest_round_trip_friction_bps": (
                None if self.replay.latest_round_trip_friction_bps is None
                else str(self.replay.latest_round_trip_friction_bps)),
            "expected_gross_edge_bps": None if gross is None else str(gross),
            "expected_round_trip_friction_bps": None if friction is None else str(friction),
            "expected_net_edge_bps": None if net is None else str(net),
            "economic_guard_result": self.replay.reason_code,
            "economic_admissions": self.replay.economic_admissions,
            "economic_rejections": self.replay.economic_rejections,
            "economic_reject_rate": str(self.replay.economic_reject_rate),
            "evidence_fingerprint": self.evidence_fingerprint,
            "promotion": PROMOTION, "multi_market_production": "DISABLED",
        }

    @property
    def evidence_fingerprint(self) -> str:
        """Identity of the evidence itself, so a selection can be reproduced."""
        return fingerprint({"schema": RESEARCH_VERSION, "market": self.market,
                            "profile_id": self.profile_id,
                            "profile_fingerprint": self.profile_fingerprint,
                            "dataset_fingerprint": self.dataset_fingerprint,
                            "verdict": self.verdict.status,
                            "net_pnl_mxn": str(self.replay.net_pnl_mxn),
                            "round_trips": len(self.replay.round_trips)})

    def public(self) -> dict[str, Any]:
        return {"market": self.market, "market_class": self.market_class,
                "profile_id": self.profile_id, "profile": dict(self.profile_contract),
                "compatibility": self.compatibility, "data_source": self.data_source,
                "verdict": self.verdict.telemetry(),
                "economic_reject_rate": str(self.replay.economic_reject_rate),
                "evidence_fingerprint": self.evidence_fingerprint,
                "selector_contract": self.selector_contract(),
                "lookahead_ok": self.lookahead_ok, "lookahead_detail": self.lookahead_detail,
                "deterministic": self.deterministic, "notes": list(self.notes)}


@dataclass(frozen=True, slots=True)
class ResearchReport:
    """Every (profile, market) evidence row, plus what was deliberately excluded."""

    generated_at: str
    requirements: EvidenceRequirements
    rows: tuple[ProfileMarketEvidence, ...]
    excluded_markets: tuple[dict[str, str], ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def champion_row(self) -> ProfileMarketEvidence | None:
        for row in self.rows:
            if row.profile_id.startswith("mean-reversion-safe"):
                return row
        return None

    @property
    def challenger_rows(self) -> tuple[ProfileMarketEvidence, ...]:
        return tuple(row for row in self.rows
                     if not row.profile_id.startswith("mean-reversion-safe"))

    @property
    def certifiable(self) -> tuple[ProfileMarketEvidence, ...]:
        return tuple(row for row in self.rows if row.verdict.certified)

    def telemetry(self) -> dict[str, Any]:
        return {"version": RESEARCH_VERSION, "generated_at": self.generated_at,
                "requirements": self.requirements.public(),
                "promotion": PROMOTION, "multi_market_production": "DISABLED",
                "champion": None if self.champion_row is None else self.champion_row.public(),
                "challengers": [row.public() for row in self.challenger_rows],
                "markets": sorted({row.market for row in self.rows}),
                "certifiable": [row.profile_id for row in self.certifiable],
                "excluded_markets": [dict(item) for item in self.excluded_markets],
                "notes": list(self.notes)}


def _is_deterministic(*, series: CandleSeries, profile_id: str, market: str,
                      taker_fee_rate: Decimal, spread_bps: Decimal,
                      bids: tuple[Any, ...], asks: tuple[Any, ...],
                      budget_mxn: Decimal, policy: EconomicPolicy,
                      compatibility: str) -> bool:
    """Re-run the replay with a fresh evaluator and require an identical result.

    Determinism is asserted, not assumed. A fresh evaluator is used for each run so
    shared mutable state cannot mask non-determinism, and the comparison covers the
    full telemetry payload -- decisions, admissions, fees and P&L.
    """
    first = replay_candles(candles=series.candles, profile_id=profile_id, market=market,
                           evaluator=evaluator_for(profile_id), taker_fee_rate=taker_fee_rate,
                           spread_bps=spread_bps, bids=bids, asks=asks, budget_mxn=budget_mxn,
                           policy=policy, compatibility=compatibility)
    second = replay_candles(candles=series.candles, profile_id=profile_id, market=market,
                            evaluator=evaluator_for(profile_id), taker_fee_rate=taker_fee_rate,
                            spread_bps=spread_bps, bids=bids, asks=asks, budget_mxn=budget_mxn,
                            policy=policy, compatibility=compatibility)
    return first.telemetry() == second.telemetry()


@financial
def evaluate_profile_market(*, market: str, profile_id: str, series: CandleSeries,
                            taker_fee_rate: Decimal, spread_bps: Decimal,
                            bids: tuple[Any, ...], asks: tuple[Any, ...],
                            budget_mxn: Decimal, policy: EconomicPolicy,
                            requirements: EvidenceRequirements = DEFAULT_REQUIREMENTS,
                            markets: frozenset[str] | None = None) -> ProfileMarketEvidence:
    """Evaluate one profile on one market and apply the evidence gate.

    Only MXN-quoted markets are evaluated. AutoFund's financial envelope, ledger and
    fee accounting are MXN-denominated, so a non-MXN quote could not be accounted
    for even if it were profitable. Refusing it here with an explicit cause is
    clearer than letting it fail deep inside the accounting boundary.
    """
    quote = market.split("/")[1].upper() if "/" in market else ""
    if quote != "MXN":
        raise ValueError(f"UNSUPPORTED_QUOTE_CURRENCY:{quote or market}")
    definition = PROFILE_BY_ID[profile_id]
    declared = markets if markets is not None else definition.markets
    compatibility = market_compatibility(declared, market)
    market_class = classify_market(market)
    evaluator = evaluator_for(profile_id)

    replay = replay_candles(
        candles=series.candles, profile_id=profile_id, market=market, evaluator=evaluator,
        taker_fee_rate=taker_fee_rate, spread_bps=spread_bps, bids=bids, asks=asks,
        budget_mxn=budget_mxn, policy=policy, compatibility=compatibility)
    lookahead_ok, lookahead_detail = verify_no_lookahead(candles=series.candles,
                                                         evaluator=evaluator, market=market)
    deterministic = _is_deterministic(series=series, profile_id=profile_id, market=market,
                                      taker_fee_rate=taker_fee_rate, spread_bps=spread_bps,
                                      bids=bids, asks=asks, budget_mxn=budget_mxn,
                                      policy=policy, compatibility=compatibility)
    verdict = certify(replay=replay, lookahead_ok=lookahead_ok, deterministic=deterministic,
                      requirements=requirements, market_class=market_class)

    notes: list[str] = []
    if compatibility == INCOMPATIBLE:
        notes.append("MARKET_CLASS_INCOMPATIBLE_WITH_PROFILE")
    elif compatibility == RESEARCH_ONLY:
        notes.append("PROFILE_NOT_CERTIFIED_FOR_THIS_MARKET")
    if not series.is_real_market_data:
        notes.append("FIXTURE_EVIDENCE_NOT_REAL_MARKET_PERFORMANCE")
    if series.gaps:
        notes.append(f"CAPTURED_SERIES_HAS_{series.gaps}_GAPS")

    return ProfileMarketEvidence(
        market=market, market_class=market_class, profile_id=profile_id,
        profile_fingerprint=definition.fingerprint,
        strategy_fingerprint=definition.identity.strategy_fingerprint,
        profile_contract=definition.public(), compatibility=compatibility, replay=replay,
        verdict=verdict, lookahead_ok=lookahead_ok, lookahead_detail=lookahead_detail,
        deterministic=deterministic, data_source=series.source,
        dataset_fingerprint=series.fingerprint, notes=tuple(notes))


@financial
def build_research_report(*, series_by_market: dict[str, CandleSeries],
                          taker_fee_rate: Decimal, spread_bps: Decimal,
                          books: tuple[dict[str, Any], ...] = (),
                          budget_mxn: Decimal = Decimal("11"),
                          policy: EconomicPolicy | None = None,
                          requirements: EvidenceRequirements = DEFAULT_REQUIREMENTS,
                          generated_at: str = "") -> ResearchReport:
    """Evaluate every registered profile on every supplied market series.

    Markets with no usable candles are recorded as excluded rather than silently
    dropped, so the report cannot hide a market it failed to evaluate.
    """
    from .economics import DEFAULT_POLICY

    active_policy = policy if policy is not None else DEFAULT_POLICY
    rows: list[ProfileMarketEvidence] = []
    excluded: list[dict[str, str]] = []
    books_by_name = {str(book.get("book", "")): book for book in books}

    for market, series in sorted(series_by_market.items()):
        if not series.candles:
            excluded.append({"market": market, "reason": "NO_USABLE_CANDLES",
                             "source": series.source})
            continue
        profile = books_by_name.get(market.replace("/", "_").lower(), {})
        bids = tuple(profile.get("bids", ()))
        asks = tuple(profile.get("asks", ()))
        for definition in PROFILE_REGISTRY:
            rows.append(evaluate_profile_market(
                market=market, profile_id=definition.profile_id, series=series,
                taker_fee_rate=taker_fee_rate, spread_bps=spread_bps, bids=bids, asks=asks,
                budget_mxn=budget_mxn, policy=active_policy, requirements=requirements))

    notes: list[str] = []
    if not any(series.is_real_market_data for series in series_by_market.values()):
        notes.append("ALL_EVIDENCE_IS_FIXTURE_NOT_REAL_MARKET_PERFORMANCE")
    return ResearchReport(generated_at=generated_at, requirements=requirements,
                          rows=tuple(rows), excluded_markets=tuple(excluded),
                          notes=tuple(notes))

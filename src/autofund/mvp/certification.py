"""Market + profile certification.

Certification is a property of a *pair*, never of a profile in the abstract. "SOL/MXN
+ trend-continuation-v1" is certified or it is not; "trend-continuation-v1" is not
certified anywhere, and a profile performing well on one book says nothing about
another book's spread, depth or fee structure.

The gate is deliberately conservative, and every criterion is reported individually so
a failure is attributable rather than a bare "not certified":

- Only certifying provenance counts. Fixture evidence can never certify Production,
  and the store is asked for its certifying aggregate specifically so this cannot be
  bypassed by a careless caller.
- A demonstrated economic failure is `NOT_VIABLE`, not `INSUFFICIENT_EVIDENCE`. Those
  are different statements and collapsing them would misreport what was observed.
- Zero opportunities is `INSUFFICIENT_EVIDENCE`: the strategy never proposed anything,
  so nothing about its economics was established.
- Aggregate net P&L is descriptive and never sufficient. Round trips, win/loss, worst
  trade, drawdown and the economic rejection rate are all required alongside it.
- A previous certification can be SUSPENDED when current market quality fails, and
  suspending never deletes the historical evidence that earned it.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import financial

from .backfill import CERTIFYING_PROVENANCE
from .evidence import AggregateEvidence, ResearchEvidenceStore

CERTIFICATION_VERSION = "autofund.pair-certification.v1"

# ---------------------------------------------------------------------------
# Certification states (spec section 9)
# ---------------------------------------------------------------------------

RESEARCH_ONLY = "RESEARCH_ONLY"
ACCUMULATING_EVIDENCE = "ACCUMULATING_EVIDENCE"
NOT_VIABLE = "NOT_VIABLE"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
PRODUCTION_CERTIFIABLE = "PRODUCTION_CERTIFIABLE"
CERTIFIED = "CERTIFIED"
SUSPENDED = "SUSPENDED"

ALL_STATES = (RESEARCH_ONLY, ACCUMULATING_EVIDENCE, NOT_VIABLE, INSUFFICIENT_EVIDENCE,
              PRODUCTION_CERTIFIABLE, CERTIFIED, SUSPENDED)

# States that may be selected for a Production order.
SELECTABLE_STATES = frozenset({CERTIFIED, PRODUCTION_CERTIFIABLE})

# ---------------------------------------------------------------------------
# Named certification criteria. Every one is reported with its measured value, so a
# refusal always says which requirement was missed and by how much.
# ---------------------------------------------------------------------------

CRITERION_CANDLES = "MINIMUM_CLOSED_CANDLES"
CRITERION_EVALUATIONS = "MINIMUM_VALID_EVALUATIONS"
CRITERION_ROUND_TRIPS = "MINIMUM_REAL_ROUND_TRIPS"
CRITERION_OPPORTUNITY = "NONZERO_OPPORTUNITY_COUNT"
CRITERION_PROVENANCE = "CERTIFYING_PROVENANCE_ONLY"
CRITERION_DATA_QUALITY = "DATA_QUALITY_PASS"
CRITERION_LOOKAHEAD = "NO_LOOKAHEAD"
CRITERION_DETERMINISM = "REPLAY_DETERMINISM"
CRITERION_ECONOMIC = "ECONOMIC_FEASIBILITY"
CRITERION_ACCOUNTING = "ACCOUNTING_COMPATIBILITY"
CRITERION_EXECUTION = "EXECUTION_COMPATIBILITY"
CRITERION_DRAWDOWN = "BOUNDED_DRAWDOWN"
CRITERION_WORST_TRADE = "BOUNDED_WORST_TRADE"
CRITERION_SPREAD = "ACCEPTABLE_OBSERVED_SPREAD"

# The economic rejection rate is deliberately NOT a certification criterion. The
# specification lists it as something to *report*, and correctly so: a strategy that
# refuses most opportunities on economics is the guard working, not a defect. Making it
# a gate would reward strategies that trade more, which is the opposite of this
# project's stated economic rule.
ALL_CRITERIA = (CRITERION_CANDLES, CRITERION_EVALUATIONS, CRITERION_ROUND_TRIPS,
                CRITERION_OPPORTUNITY, CRITERION_PROVENANCE, CRITERION_DATA_QUALITY,
                CRITERION_LOOKAHEAD, CRITERION_DETERMINISM, CRITERION_ECONOMIC,
                CRITERION_ACCOUNTING, CRITERION_EXECUTION, CRITERION_DRAWDOWN,
                CRITERION_WORST_TRADE, CRITERION_SPREAD)


@dataclass(frozen=True, slots=True)
class CertificationFloor:
    """Configurable evidence floor, proportional to an ~11 MXN experiment.

    These are not months of data: they are the smallest sample that can distinguish
    "the strategy has a chance here" from "one trade happened to win". Defaults are
    deliberately modest in size but strict in kind, because what protects capital is
    requiring real evidence and bounded downside, not requiring a large sample.
    """

    min_closed_candles: int = 500
    min_evaluations: int = 200
    min_round_trips: int = 5
    min_opportunities: int = 1
    max_drawdown_mxn: Decimal = Decimal("0.50")
    max_worst_trade_mxn: Decimal = Decimal("-0.30")
    max_median_spread_bps: Decimal = Decimal("60")
    require_positive_net_pnl: bool = True
    require_five_round_trips_when_opportunities_exist: bool = True

    def __post_init__(self) -> None:
        for name in ("max_drawdown_mxn", "max_worst_trade_mxn", "max_median_spread_bps"):
            if not isinstance(getattr(self, name), Decimal):
                raise ValueError(f"{name} must be Decimal, never float/int")

    def public(self) -> dict[str, str]:
        return {name: str(getattr(self, name)) for name in self.__dataclass_fields__}


DEFAULT_FLOOR = CertificationFloor()


@dataclass(frozen=True, slots=True)
class CriterionResult:
    """One criterion's outcome, with the measured value and the requirement it faced."""

    name: str
    passed: bool
    measured: str
    required: str
    note: str = ""

    def public(self) -> dict[str, Any]:
        return {"criterion": self.name, "passed": self.passed, "measured": self.measured,
                "required": self.required, "note": self.note}


@dataclass(frozen=True, slots=True)
class PairCertification:
    """Certification verdict for one market/profile pair, with full attribution."""

    market: str
    profile_id: str
    state: str
    criteria: tuple[CriterionResult, ...]
    floor: CertificationFloor
    evidence: AggregateEvidence | None
    provenance_kinds: tuple[str, ...]
    suspended_reason: str = ""

    @property
    def certified(self) -> bool:
        return self.state == CERTIFIED

    @property
    def selectable(self) -> bool:
        return self.state in SELECTABLE_STATES

    @property
    def failed_criteria(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.criteria if not item.passed)

    @property
    def passed_criteria(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.criteria if item.passed)

    def public(self) -> dict[str, Any]:
        return {"version": CERTIFICATION_VERSION, "market": self.market,
                "profile_id": self.profile_id, "state": self.state,
                "certified": self.certified, "selectable": self.selectable,
                "provenance_kinds": list(self.provenance_kinds),
                "failed_criteria": list(self.failed_criteria),
                "criteria": [item.public() for item in self.criteria],
                "evidence": None if self.evidence is None else self.evidence.telemetry(),
                "floor": self.floor.public(), "suspended_reason": self.suspended_reason,
                "promotion": "DISABLED"}


@financial
def certify_pair(*, market: str, profile_id: str, store: ResearchEvidenceStore,
                 floor: CertificationFloor = DEFAULT_FLOOR,
                 market_quality_ok: bool = True,
                 suspension_reason: str = "") -> PairCertification:
    """Certify one pair from its accumulated real evidence.

    `market_quality_ok` is the *current* market state. When it is false, an already
    certified pair becomes SUSPENDED rather than losing its evidence: the historical
    record stays intact and the pair can return to CERTIFIED without re-earning it,
    because the evidence never became invalid.
    """
    observed_kinds = tuple(sorted({
        kind for kind in CERTIFYING_PROVENANCE if store.aggregate_for(
            market=market, profile_id=profile_id, provenance_kind=kind) is not None
    }))
    evidence = store.certifying_aggregate(market=market, profile_id=profile_id)

    if evidence is None:
        # No certifying evidence at all. Either the pair has never been evaluated, or
        # only fixture evidence exists -- and fixture evidence cannot certify.
        any_evidence = store.aggregate_for(market=market, profile_id=profile_id) is not None
        return PairCertification(
            market=market, profile_id=profile_id,
            state=RESEARCH_ONLY if not any_evidence else ACCUMULATING_EVIDENCE,
            criteria=(), floor=floor, evidence=None, provenance_kinds=(),
            suspended_reason=("ONLY_NON_CERTIFYING_EVIDENCE" if any_evidence else ""))

    criteria = _evaluate_criteria(evidence, floor)
    failed = {item.name for item in criteria if not item.passed}

    if not failed:
        state = CERTIFIED if market_quality_ok else SUSPENDED
        reason = suspension_reason or ("MARKET_QUALITY_FAILED" if not market_quality_ok else "")
        return PairCertification(market=market, profile_id=profile_id, state=state,
                                 criteria=criteria, floor=floor, evidence=evidence,
                                 provenance_kinds=observed_kinds, suspended_reason=reason)

    if evidence.economic_passes == 0 and evidence.economic_rejects > 0 and \
            evidence.evaluations >= floor.min_evaluations:
        # Opportunities existed and every one failed economics. That is a demonstrated
        # failure, not a shortage of data, and saying "insufficient evidence" here
        # would understate what has actually been established.
        state = NOT_VIABLE
    elif evidence.signals == 0:
        # The strategy never proposed anything, so nothing about its economics is known.
        state = INSUFFICIENT_EVIDENCE
    elif CRITERION_ROUND_TRIPS in failed or CRITERION_CANDLES in failed or \
            CRITERION_EVALUATIONS in failed:
        state = ACCUMULATING_EVIDENCE
    else:
        state = NOT_VIABLE
    return PairCertification(market=market, profile_id=profile_id, state=state,
                             criteria=criteria, floor=floor, evidence=evidence,
                             provenance_kinds=observed_kinds)


def _evaluate_criteria(evidence: AggregateEvidence, floor: CertificationFloor) -> tuple[
        CriterionResult, ...]:
    """Every certification criterion, measured against the floor."""
    results: list[CriterionResult] = []

    def add(name: str, passed: bool, measured: Any, required: Any, note: str = "") -> None:
        results.append(CriterionResult(name, passed, str(measured), str(required), note))

    add(CRITERION_PROVENANCE, evidence.is_real, evidence.provenance_kind,
        "REAL_CAPTURED|REAL_HISTORICAL",
        "fixture evidence tests the architecture and cannot certify Production")
    add(CRITERION_CANDLES, evidence.closed_candles >= floor.min_closed_candles,
        evidence.closed_candles, floor.min_closed_candles)
    add(CRITERION_EVALUATIONS, evidence.evaluations >= floor.min_evaluations,
        evidence.evaluations, floor.min_evaluations)
    add(CRITERION_OPPORTUNITY, evidence.signals >= floor.min_opportunities,
        evidence.signals, floor.min_opportunities,
        "zero opportunities means nothing about economics was established")
    # The round-trip floor applies only when the strategy actually produced
    # opportunities. With zero signals there is nothing to complete, so the criterion
    # is vacuously satisfied and the honest failure is NONZERO_OPPORTUNITY_COUNT;
    # requiring round trips here would report the wrong cause.
    round_trip_required = (floor.min_round_trips
                           if (floor.require_five_round_trips_when_opportunities_exist
                               and evidence.signals > 0) else 0)
    add(CRITERION_ROUND_TRIPS, evidence.round_trips >= round_trip_required,
        evidence.round_trips, round_trip_required,
        "real-data completed round trips, not simulated entries")
    add(CRITERION_DATA_QUALITY, evidence.data_quality_failures == 0,
        evidence.data_quality_failures, 0)
    add(CRITERION_LOOKAHEAD, evidence.lookahead_ok, evidence.lookahead_ok, True)
    add(CRITERION_DETERMINISM, evidence.deterministic, evidence.deterministic, True)
    add(CRITERION_ECONOMIC, evidence.economic_passes > 0, evidence.economic_passes, 1,
        "at least one opportunity had to be admitted by the economic guard")
    add(CRITERION_ACCOUNTING, evidence.accounting_compatible,
        evidence.accounting_compatible, True)
    add(CRITERION_EXECUTION, evidence.execution_compatible,
        evidence.execution_compatible, True)
    add(CRITERION_DRAWDOWN, evidence.max_drawdown_mxn <= floor.max_drawdown_mxn,
        evidence.max_drawdown_mxn, floor.max_drawdown_mxn)
    worst = evidence.worst_trade_mxn
    add(CRITERION_WORST_TRADE, worst is None or worst >= floor.max_worst_trade_mxn,
        worst, floor.max_worst_trade_mxn)
    spread = evidence.median_spread_bps
    add(CRITERION_SPREAD, spread is None or spread <= floor.max_median_spread_bps,
        spread, floor.max_median_spread_bps)
    return tuple(results)


def certify_all(*, store: ResearchEvidenceStore,
                market_quality: dict[str, bool] | None = None,
                floor: CertificationFloor = DEFAULT_FLOOR) -> tuple[PairCertification, ...]:
    """Certify every pair the store knows about, deterministically ordered."""
    quality = market_quality or {}
    return tuple(
        certify_pair(market=market, profile_id=profile_id, store=store, floor=floor,
                     market_quality_ok=quality.get(market, True))
        for market, profile_id in store.pairs())


__all__ = [
    "ACCUMULATING_EVIDENCE",
    "ALL_CRITERIA",
    "ALL_STATES",
    "CERTIFICATION_VERSION",
    "CERTIFIED",
    "DEFAULT_FLOOR",
    "INSUFFICIENT_EVIDENCE",
    "NOT_VIABLE",
    "PRODUCTION_CERTIFIABLE",
    "RESEARCH_ONLY",
    "SELECTABLE_STATES",
    "SUSPENDED",
    "CertificationFloor",
    "CriterionResult",
    "PairCertification",
    "certify_all",
    "certify_pair",
]

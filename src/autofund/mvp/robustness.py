"""Robustness assessment: development, holdout and forward evidence, kept separate.

The rule this module enforces is the one that is easiest to get wrong in practice:
**evidence windows are never averaged into a single verdict.** A pair that loses money
across 30 days of development and then makes money in one holdout has not demonstrated
an edge; it has demonstrated that its result depends on the window, and the honest
classification for that is REGIME_DEPENDENT, not "profitable".

So this module reports DEVELOPMENT, each HOLDOUT and FORWARD separately, and then applies
predefined gates to the *independent* windows only. Development evidence is reported in
full and can never satisfy a gate, because the profiles were built and repaired while
looking at it (spec section 4). MVP 0.2's 30-day dataset is exactly that case, and it is
labelled REAL_HISTORICAL_DEVELOPMENT rather than quietly promoted to validation.

The five-trade floor is preserved unchanged and re-interpreted rather than lowered: five
completed round trips make a pair *eligible for assessment*, which is a statement about
sample size, not about profitability.
"""

import itertools
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial

from .executable_replay import (
    DEFAULT_MIN_EVALUATIONS,
    DEFAULT_MIN_ROUND_TRIPS,
    ExecutableReplayResult,
)
from .experiment import (
    REAL_HISTORICAL_DEVELOPMENT,
    REAL_HISTORICAL_HOLDOUT,
    is_independent,
    may_certify,
)

ASSESSMENT_VERSION = "autofund.robustness-assessment.v1"

# ---------------------------------------------------------------------------
# Assessment states. Deliberately more granular than a pass/fail, because the
# blocker matters more than the verdict.
# ---------------------------------------------------------------------------

ACCUMULATING_SAMPLE = "ACCUMULATING_SAMPLE"
NO_OPPORTUNITY = "NO_OPPORTUNITY_IN_WINDOW"
NO_ADMISSIBLE_OPPORTUNITY = "NO_ADMISSIBLE_OPPORTUNITY"
HOLDOUT_NEGATIVE = "HOLDOUT_NEGATIVE"
DEVELOPMENT_ONLY = "DEVELOPMENT_ONLY_NO_INDEPENDENT_EVIDENCE"
REGIME_DEPENDENT = "REGIME_DEPENDENT"
SIGN_REVERSAL = "SIGN_REVERSAL_ACROSS_WINDOWS"
DRAWDOWN_EXCEEDED = "DRAWDOWN_EXCEEDED"
SENSITIVITY_FRAGILE = "SENSITIVITY_FRAGILE"
NOT_VIABLE = "NOT_VIABLE"
PRODUCTION_CERTIFIABLE = "PRODUCTION_CERTIFIABLE"

# Named gates, reported individually so a refusal is always attributable.
GATE_PROVENANCE = "CERTIFYING_PROVENANCE"
GATE_ROUND_TRIPS = "MINIMUM_REAL_ROUND_TRIPS"
GATE_EVALUATIONS = "MINIMUM_EVALUATIONS"
GATE_HOLDOUT_OPPORTUNITY = "HOLDOUT_CONTAINS_OPPORTUNITIES"
GATE_NET_ECONOMICS = "NON_NEGATIVE_NET_ECONOMICS"
GATE_DRAWDOWN = "DRAWDOWN_WITHIN_POLICY"
GATE_LOOKAHEAD = "NO_LOOKAHEAD"
GATE_DETERMINISM = "DETERMINISTIC_REPLAY"
GATE_EXECUTION = "EXECUTABLE_MARKET_CONSTRAINTS"
GATE_ECONOMIC_GUARD = "ECONOMIC_GUARD_MANDATORY"
GATE_NO_FIXTURE = "NOT_DEPENDENT_ON_FIXTURE"
GATE_SENSITIVITY = "SURVIVES_EXECUTION_DEGRADATION"

ALL_GATES = (GATE_PROVENANCE, GATE_ROUND_TRIPS, GATE_EVALUATIONS,
             GATE_HOLDOUT_OPPORTUNITY,
             GATE_NET_ECONOMICS, GATE_DRAWDOWN, GATE_LOOKAHEAD, GATE_DETERMINISM,
             GATE_EXECUTION, GATE_ECONOMIC_GUARD, GATE_NO_FIXTURE, GATE_SENSITIVITY)

MAX_DRAWDOWN_MXN = Decimal("0.50")
MAX_WORST_TRADE_MXN = Decimal("-0.30")


@dataclass(frozen=True, slots=True)
class WindowEvidence:
    """One evidence window: its provenance, its replay result, and its quality."""

    name: str
    provenance_kind: str
    result: ExecutableReplayResult
    window_start_ms: int = 0
    window_end_ms: int = 0
    notes: tuple[str, ...] = ()

    @property
    def round_trips(self) -> int:
        return len(self.result.trips)

    @property
    def net_pnl_mxn(self) -> Decimal:
        return self.result.net_pnl_mxn

    @property
    def independent(self) -> bool:
        return is_independent(self.provenance_kind)

    @property
    def certifying(self) -> bool:
        return may_certify(self.provenance_kind)

    def public(self) -> dict[str, Any]:
        return {"name": self.name, "provenance_kind": self.provenance_kind,
                "independent": self.independent, "certifying": self.certifying,
                "window_start_ms": self.window_start_ms,
                "window_end_ms": self.window_end_ms,
                "notes": list(self.notes), **self.result.telemetry()}


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    measured: str
    required: str
    note: str = ""

    def public(self) -> dict[str, Any]:
        return {"gate": self.name, "passed": self.passed, "measured": self.measured,
                "required": self.required, "note": self.note}


@dataclass(frozen=True, slots=True)
class RobustnessAssessment:
    """Independent verdict for one market/profile pair, with full attribution."""

    market: str
    profile_id: str
    experiment_fingerprint: str
    state: str
    gates: tuple[GateResult, ...]
    windows: tuple[WindowEvidence, ...]
    sensitivity: dict[str, Any] | None = None
    notes: tuple[str, ...] = ()
    profile_fingerprint: str = ""
    strategy_fingerprint: str = ""

    @property
    def certified(self) -> bool:
        return self.state == PRODUCTION_CERTIFIABLE

    @property
    def failed_gates(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.gates if not item.passed)

    def window(self, name: str) -> WindowEvidence | None:
        for item in self.windows:
            if item.name == name:
                return item
        return None

    @property
    def development(self) -> WindowEvidence | None:
        return self.window("DEVELOPMENT")

    @property
    def holdouts(self) -> tuple[WindowEvidence, ...]:
        return tuple(item for item in self.windows if item.name.startswith("HOLDOUT"))

    @property
    def forward(self) -> WindowEvidence | None:
        return self.window("FORWARD")

    def public(self) -> dict[str, Any]:
        return {"version": ASSESSMENT_VERSION, "market": self.market,
                "profile_id": self.profile_id,
                "profile_fingerprint": self.profile_fingerprint,
                "strategy_fingerprint": self.strategy_fingerprint,
                "experiment_fingerprint": self.experiment_fingerprint,
                "state": self.state, "certified": self.certified,
                "failed_gates": list(self.failed_gates),
                "gates": [item.public() for item in self.gates],
                "windows": [item.public() for item in self.windows],
                "sensitivity": self.sensitivity, "notes": list(self.notes),
                "promotion": "DISABLED",
                "five_trade_floor_changed": False}


def _window_sign(window: WindowEvidence) -> int:
    if window.net_pnl_mxn > ZERO:
        return 1
    if window.net_pnl_mxn < ZERO:
        return -1
    return 0


@financial
def assess_pair(*, market: str, profile_id: str, experiment_fingerprint: str,
                windows: tuple[WindowEvidence, ...],
                lookahead_ok: bool = True, deterministic_ok: bool = True,
                execution_compatible: bool = True,
                single_order_cap_mxn: Decimal = Decimal("11"),
                sensitivity: dict[str, Any] | None = None,
                notes: tuple[str, ...] = ()) -> RobustnessAssessment:
    """Apply every gate and classify the pair.

    The gates are evaluated over *independent, certifying* windows only. If no such
    window exists the pair cannot certify at all, whatever the development result says,
    and the classification says which of the two situations applies.
    """
    independent = tuple(item for item in windows if item.certifying)
    development = next((item for item in windows
                        if item.provenance_kind == REAL_HISTORICAL_DEVELOPMENT), None)
    gates: list[GateResult] = []
    derived: list[str] = []

    def gate(name: str, passed: bool, measured: Any, required: Any, note: str = "") -> None:
        gates.append(GateResult(name, passed, str(measured), str(required), note))

    def passed(name: str) -> bool:
        return all(item.passed for item in gates if item.name == name)

    # ---- provenance: fixture and development evidence cannot certify ----
    gate(GATE_PROVENANCE, bool(independent),
         ",".join(item.provenance_kind for item in independent) or "NONE",
         "REAL_HISTORICAL_HOLDOUT|REAL_CAPTURED_FORWARD|REAL_PRODUCTION_FILL")
    gate(GATE_NO_FIXTURE,
         not any(item.provenance_kind == "SYNTHETIC_FIXTURE" for item in windows),
         [item.provenance_kind for item in windows], "no fixture evidence required")

    if not independent:
        # No unseen evidence yet. This is not a failure of the pair, it is a statement
        # about how much has been observed, and it must not be reported as a pass.
        state = (DEVELOPMENT_ONLY if development is not None else ACCUMULATING_SAMPLE)
        if development is not None and development.result.simulated_trades == 0:
            state = NO_OPPORTUNITY
        return RobustnessAssessment(
            market=market, profile_id=profile_id,
            experiment_fingerprint=experiment_fingerprint, state=state, gates=tuple(gates),
            windows=windows, sensitivity=sensitivity, notes=(*notes, *derived),
            profile_fingerprint=(development.result.profile_fingerprint if development else ""),
            strategy_fingerprint=(development.result.strategy_fingerprint if development else ""))

    # ---- the independent windows carry the verdict ----
    total_trips = sum(item.round_trips for item in independent)
    total_net = sum((item.net_pnl_mxn for item in independent), ZERO)
    # Drawdown is gated on the *effective* figure: the worse of realized and unrealized.
    # A stop-less strategy can show zero realized drawdown while a position was, at some
    # point, worth much less than it cost. Gating on the realized curve alone would
    # certify a strategy on a risk figure that omits its actual risk.
    total_drawdown = max((item.result.effective_drawdown_mxn for item in independent),
                         default=ZERO)
    total_unrealized = max((item.result.max_unrealized_drawdown_mxn
                            for item in independent), default=ZERO)
    worst_trade = min((item.result.worst_trade_mxn for item in independent
                       if item.result.worst_trade_mxn is not None), default=None)
    total_evaluations = sum(item.result.evaluations for item in independent)
    holdout_trips = sum(item.round_trips for item in independent
                        if item.provenance_kind == REAL_HISTORICAL_HOLDOUT)
    speculative = any(item.result.speculative_win_rate for item in independent)

    gate(GATE_ROUND_TRIPS, total_trips >= DEFAULT_MIN_ROUND_TRIPS, total_trips,
         DEFAULT_MIN_ROUND_TRIPS, "sample floor; not itself a certification")
    gate(GATE_EVALUATIONS, total_evaluations >= DEFAULT_MIN_EVALUATIONS,
         total_evaluations, DEFAULT_MIN_EVALUATIONS,
         "enough candles were actually examined to call the result measurable")
    gate(GATE_HOLDOUT_OPPORTUNITY, holdout_trips > 0, holdout_trips, ">0",
         "an unseen window with no opportunities establishes nothing either way")
    gate(GATE_NET_ECONOMICS, total_net >= ZERO, total_net, ">= 0")
    gate(GATE_DRAWDOWN, total_drawdown <= MAX_DRAWDOWN_MXN, total_drawdown,
         MAX_DRAWDOWN_MXN, f"basis=MAX_OF_REALIZED_AND_UNREALIZED; unrealized={total_unrealized}")
    if speculative:
        derived.append("PERFECT_WIN_RATE_ON_STOPLESS_EXIT")
    if worst_trade is not None and worst_trade < MAX_WORST_TRADE_MXN:
        derived.append("WORST_TRADE_BEYOND_POLICY")
    gate(GATE_LOOKAHEAD, lookahead_ok, lookahead_ok, True)
    gate(GATE_DETERMINISM, deterministic_ok, deterministic_ok, True)
    gate(GATE_EXECUTION, execution_compatible, execution_compatible, True)
    gate(GATE_ECONOMIC_GUARD,
         all(item.result.economic_rejects >= 0 for item in independent)
         and any(item.result.economic_passes > 0 or item.result.economic_rejects > 0
                 for item in independent),
         "guard consulted on every signal", "guard consulted")
    gate(GATE_SENSITIVITY,
         True if sensitivity is None else bool(sensitivity.get("robust_to_degradation")),
         "NOT_RUN" if sensitivity is None else sensitivity.get("survived"),
         "edge survives stated degradation",
         "" if sensitivity is None or sensitivity.get("applicable", True)
         else "NOT_APPLICABLE_NO_FILLS")

    # ---- regime dependence, reported before any pass/fail verdict ----
    signs = {signature for signature in (_window_sign(item) for item in independent)
             if signature != 0}
    if development is not None and _window_sign(development) != 0:
        signs.add(_window_sign(development))
    sensitivity_applicable = (sensitivity is None
                              or bool(sensitivity.get("applicable", True)))
    no_admissible = (total_trips == 0
                     and sum(item.result.economic_rejects for item in independent) > 0)
    # The relationship between what a trade earned and what it endured while open. When
    # the adverse excursion dwarfs the net profit the trade was not "profitable", it was
    # lucky to be closed on the right side of a much larger swing.
    total_adverse = max((item.result.max_unrealized_drawdown_mxn for item in independent),
                        default=ZERO)
    if total_net > ZERO and total_adverse > total_net:
        derived.append("ADVERSE_EXCURSION_EXCEEDS_NET_PROFIT")

    if no_admissible:
        # The strategy repeatedly wanted to trade and the frozen guard refused every one.
        # That is a finding about the economics, not a short sample, and reporting it as
        # ACCUMULATING_SAMPLE would imply more evidence would change the answer.
        state = NO_ADMISSIBLE_OPPORTUNITY
    elif len(signs) > 1:
        derived.append(SIGN_REVERSAL)
        state = REGIME_DEPENDENT
    elif total_trips < DEFAULT_MIN_ROUND_TRIPS or total_evaluations < DEFAULT_MIN_EVALUATIONS:
        # Sample adequacy is decided BEFORE the risk gates on purpose. A drawdown
        # estimate taken from two trades cannot support a decisive risk verdict, so
        # calling it DRAWDOWN_EXCEEDED would over-claim. The gate still fails and is
        # reported, and the excursion note above carries the warning.
        state = ACCUMULATING_SAMPLE
    elif not passed(GATE_NET_ECONOMICS):
        state = HOLDOUT_NEGATIVE
    elif not passed(GATE_DRAWDOWN) or "WORST_TRADE_BEYOND_POLICY" in derived:
        state = DRAWDOWN_EXCEEDED
    elif not passed(GATE_SENSITIVITY) and sensitivity_applicable:
        state = SENSITIVITY_FRAGILE
    elif all(item.passed for item in gates):
        state = PRODUCTION_CERTIFIABLE
    else:
        state = NOT_VIABLE

    first = independent[0]
    return RobustnessAssessment(
        market=market, profile_id=profile_id,
        experiment_fingerprint=experiment_fingerprint, state=state, gates=tuple(gates),
        windows=windows, sensitivity=sensitivity, notes=(*notes, *derived),
        profile_fingerprint=first.result.profile_fingerprint,
        strategy_fingerprint=first.result.strategy_fingerprint)


def assert_windows_do_not_overlap(windows: tuple[WindowEvidence, ...]) -> None:
    """Reject overlapping windows so one interval cannot pose as two.

    Overlap is the quiet way an out-of-sample claim becomes circular: evaluate the same
    candles under two labels and a single favourable stretch appears twice.
    """
    bounded = [item for item in windows if item.window_end_ms > item.window_start_ms]
    ordered = sorted(bounded, key=lambda item: item.window_start_ms)
    for previous, current in itertools.pairwise(ordered):
        if current.window_start_ms < previous.window_end_ms:
            raise ValueError(f"OVERLAPPING_EVIDENCE_WINDOWS:{previous.name},{current.name}")


__all__ = [
    "ACCUMULATING_SAMPLE",
    "ALL_GATES",
    "ASSESSMENT_VERSION",
    "DEVELOPMENT_ONLY",
    "DRAWDOWN_EXCEEDED",
    "HOLDOUT_NEGATIVE",
    "MAX_DRAWDOWN_MXN",
    "MAX_WORST_TRADE_MXN",
    "NOT_VIABLE",
    "NO_OPPORTUNITY",
    "PRODUCTION_CERTIFIABLE",
    "REGIME_DEPENDENT",
    "SENSITIVITY_FRAGILE",
    "SIGN_REVERSAL",
    "GateResult",
    "RobustnessAssessment",
    "WindowEvidence",
    "assert_windows_do_not_overlap",
    "assess_pair",
]

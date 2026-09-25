"""Champion decision evidence.

This module owns the *single* implementation of the certified Champion decision.
The autonomous runner calls it to trade, and the same call returns the evidence
that telemetry serializes. Telemetry therefore never recalculates a decision, and
the recorded evidence cannot drift from what actually drove the trade.

The arithmetic below is a faithful extraction of the logic the runner previously
inlined, so live trading behavior is unchanged.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ONE, ZERO, decimal, financial

EVIDENCE_VERSION = "autofund.strategy-evidence.v1"
DISTANCE_VERSION = "autofund.distance-to-signal.v1"

# Distance semantics are part of the public contract and must not change silently.
# distance = (close - boundary) / boundary, computed against the decision boundary
# that the branch in question actually uses (entry mean*(1-t), exit cost*(1+t)).
#   negative -> the boundary has been exceeded (condition met, signal fires)
#   zero     -> exactly at the boundary
#   positive -> remaining distance before the condition can be met
DISTANCE_SEMANTICS = "NEGATIVE_BOUNDARY_EXCEEDED_ZERO_AT_BOUNDARY_POSITIVE_REMAINING"
DISTANCE_UNITS = "FRACTION_OF_DECISION_BOUNDARY_PRICE"

# An evaluation is "near signal" when the remaining distance is at most this
# fraction of the boundary price. Documented and deliberately conservative; it is
# a reporting threshold only and never influences a trading decision.
NEAR_SIGNAL_THRESHOLD = Decimal("0.001")

DECISION_NO_SIGNAL = "NO_SIGNAL"
DECISION_BUY = "BUY"
DECISION_SELL = "SELL"

NO_CLOSED_CANDLE = "NO_CLOSED_CANDLE"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
ENTRY_CONDITION_NOT_MET = "ENTRY_CONDITION_NOT_MET"
EXIT_CONDITION_NOT_MET = "EXIT_CONDITION_NOT_MET"
SIGNAL_BUY = "SIGNAL_BUY"
SIGNAL_SELL = "SIGNAL_SELL"

REASON_MESSAGES: dict[str, str] = {
    NO_CLOSED_CANDLE: "No closed candle observed yet",
    INSUFFICIENT_HISTORY: "Insufficient closed-candle evidence",
    ENTRY_CONDITION_NOT_MET: "Champion chose no trade",
    EXIT_CONDITION_NOT_MET: "Champion retained owned position",
    SIGNAL_BUY: "BUY",
    SIGNAL_SELL: "SELL",
}


@dataclass(frozen=True, slots=True)
class ChampionParameters:
    """Certified Champion thresholds, mirroring the runner's live constants."""

    min_history: int = 3
    max_window: int = 20
    entry_threshold: Decimal = Decimal("0.001")
    exit_threshold: Decimal = Decimal("0.002")

    @property
    def thresholds(self) -> dict[str, str]:
        return {"min_history": str(self.min_history), "max_window": str(self.max_window),
                "entry_threshold": str(self.entry_threshold),
                "exit_threshold": str(self.exit_threshold)}


CHAMPION_PARAMETERS = ChampionParameters()


@financial
def exit_boundary(average_cost_mxn: Decimal, parameters: ChampionParameters = CHAMPION_PARAMETERS) -> Decimal:
    """The Champion's SELL boundary for a given average cost.

    Exposed so the economic guard can evaluate the *strategy's own* target price
    without duplicating or reinterpreting the strategy's arithmetic. The average
    cost passed in must already reflect the buy fee, because that is what the
    runner's position carries.
    """
    decimal(average_cost_mxn, "average_cost_mxn")
    if average_cost_mxn <= ZERO:
        return ZERO
    return average_cost_mxn * (ONE + parameters.exit_threshold)


@financial
def entry_boundary(closes: tuple[Decimal, ...],
                   parameters: ChampionParameters = CHAMPION_PARAMETERS) -> Decimal:
    """The Champion's BUY boundary for a rolling closed-candle series."""
    if not closes:
        return ZERO
    window = closes[-parameters.max_window:]
    mean = sum(window, ZERO) / Decimal(len(window))
    return mean * (ONE - parameters.entry_threshold)


@dataclass(frozen=True, slots=True)
class ChampionEvaluation:
    """Decision plus the exact deterministic inputs and margin behind it."""

    decision: str
    reason_code: str
    distance_to_signal: Decimal | None
    eligible: bool
    features: dict[str, str]
    thresholds: dict[str, str]
    profile_id: str
    strategy_id: str
    strategy_version: str
    market: str
    market_regime: str
    closed_candle_count: int
    position_open: bool
    strategy_fingerprint: str

    @property
    def message(self) -> str:
        return str(REASON_MESSAGES.get(self.reason_code, self.reason_code))

    @property
    def distance_bps(self) -> Decimal | None:
        return None if self.distance_to_signal is None else self.distance_to_signal * Decimal("10000")

    @property
    def near_signal(self) -> bool:
        distance = self.distance_to_signal
        return bool(distance is not None and Decimal("0") <= distance <= NEAR_SIGNAL_THRESHOLD)

    @financial
    def telemetry(self) -> dict[str, Any]:
        """Telemetry payload for STRATEGY_EVALUATED / NO_SIGNAL checkpoints."""
        return {"profile_id": self.profile_id, "strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version, "evidence_version": EVIDENCE_VERSION,
                "strategy_fingerprint": self.strategy_fingerprint, "market": self.market,
                "market_regime": self.market_regime, "closed_candles": self.closed_candle_count,
                "position_open": self.position_open, "eligible": self.eligible,
                "features": dict(self.features), "thresholds": dict(self.thresholds),
                "decision": self.decision, "reason_code": self.reason_code,
                "distance_to_signal": None if self.distance_to_signal is None else str(self.distance_to_signal),
                "distance_to_signal_bps": None if self.distance_bps is None else str(self.distance_bps),
                "distance_semantics": DISTANCE_SEMANTICS, "distance_units": DISTANCE_UNITS,
                "distance_version": DISTANCE_VERSION, "near_signal": self.near_signal}

    def fingerprint(self, fingerprint_fn: Callable[[object], str]) -> str:
        """Deterministic identity of this evaluation's evidence."""
        return str(fingerprint_fn({"schema": EVIDENCE_VERSION, **self.telemetry()}))


def evaluate_champion(
    *,
    market: str,
    closes: tuple[Decimal, ...],
    quantity: Decimal = ZERO,
    cost_basis_mxn: Decimal = ZERO,
    candle_present: bool = True,
    parameters: ChampionParameters = CHAMPION_PARAMETERS,
    profile_id: str = "mean-reversion-safe",
    strategy_id: str = "mean_reversion",
    strategy_version: str = "0.1",
    strategy_fingerprint: str = "",
    market_regime: str = "NORMAL",
) -> ChampionEvaluation:
    """Certified Champion decision for one closed candle, with its evidence.

    ``closes`` is the rolling closed-candle series ending at the current candle,
    exactly as the runner maintains it.
    """
    position_open = quantity > ZERO
    thresholds = parameters.thresholds

    def build(decision: str, reason: str, distance: Decimal | None, eligible: bool,
              features: dict[str, str]) -> ChampionEvaluation:
        return ChampionEvaluation(decision=decision, reason_code=reason, distance_to_signal=distance,
                                  eligible=eligible, features=features, thresholds=thresholds,
                                  profile_id=profile_id, strategy_id=strategy_id,
                                  strategy_version=strategy_version, market=market,
                                  market_regime=market_regime, closed_candle_count=len(closes),
                                  position_open=position_open,
                                  strategy_fingerprint=strategy_fingerprint)

    if not candle_present:
        return build(DECISION_NO_SIGNAL, NO_CLOSED_CANDLE, None, False, {})
    if len(closes) < parameters.min_history:
        return build(DECISION_NO_SIGNAL, INSUFFICIENT_HISTORY, None, False,
                     {"required_history": str(parameters.min_history)})

    window = closes[-parameters.max_window:]
    mean = sum(window, ZERO) / Decimal(len(window))
    close = closes[-1]

    if position_open:
        average_cost = cost_basis_mxn / quantity
        boundary = average_cost * (ONE + parameters.exit_threshold)
        distance = (close - boundary) / boundary
        features = {"window": str(len(window)), "mean_mxn": str(mean), "close_mxn": str(close),
                    "average_cost_mxn": str(average_cost), "exit_boundary_mxn": str(boundary),
                    "quantity": str(quantity), "cost_basis_mxn": str(cost_basis_mxn)}
        if close > boundary:
            return build(DECISION_SELL, SIGNAL_SELL, distance, True, features)
        return build(DECISION_NO_SIGNAL, EXIT_CONDITION_NOT_MET, distance, True, features)

    boundary = mean * (ONE - parameters.entry_threshold)
    distance = (close - boundary) / boundary
    features = {"window": str(len(window)), "mean_mxn": str(mean), "close_mxn": str(close),
                "entry_boundary_mxn": str(boundary)}
    if close < boundary:
        return build(DECISION_BUY, SIGNAL_BUY, distance, True, features)
    return build(DECISION_NO_SIGNAL, ENTRY_CONDITION_NOT_MET, distance, True, features)

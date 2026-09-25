"""Versioned, fee-aware strategy profiles.

MVP 0.1.3 proved something important and uncomfortable: the certified Champion's
20 bps profit target is structurally below the real ~156 bps two-sided taker cost,
so every BUY it proposes is correctly refused by EconomicEdgeGuard. The guard was
right. The *strategy* was the thing that could not work.

This module generalises strategy output without touching that guard, without
retuning the Champion, and without ever forcing a trade. Three separations are
load-bearing:

1. **Proposal vs. admission.** A profile proposes an opportunity and states the
   gross price move it intends to capture. It cannot approve anything.
   ``EconomicEdgeGuard`` remains the sole authority on financial admissibility.
2. **Signal vs. economics.** A profile may signal BUY whenever its price condition
   fires. Whether that signal becomes an intent is a separate question, answered
   with confirmed fees, real depth and real spread.
3. **Evidence vs. promotion.** Profiles carry evidence status. Nothing here
   promotes anything: ``PROMOTION = DISABLED`` and the Champion is unchanged.

A profile that cannot clear friction produces ``NOT_VIABLE``. That is a successful
outcome of the architecture, not a failure of it. ``NO_VIABLE_OPPORTUNITY`` means
no trade.

Every threshold is a bounded, versioned Decimal. There are no floats and no
parameters fitted to any single observed session.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from autofund.decimal_utils import ONE, ZERO, decimal, financial
from autofund.replay.data import Candle
from autofund.replay.serialization import fingerprint

# The Champion's own reason codes are re-exported rather than redefined, so the
# frozen profile reports byte-identical evidence and the two vocabularies cannot
# drift apart.
from .champion import ENTRY_CONDITION_NOT_MET, EXIT_CONDITION_NOT_MET, NO_CLOSED_CANDLE

__all__ = [
    "CLASS_STABLE_OR_FIAT",
    "CLASS_UNKNOWN",
    "CLASS_VOLATILE_CRYPTO",
    "COMPATIBLE",
    "DECISION_BUY",
    "DECISION_NO_SIGNAL",
    "DECISION_SELL",
    "DEFAULT_TARGET_MODEL",
    "ENTRY_CONDITION_NOT_MET",
    "EXIT_CONDITION_NOT_MET",
    "EXIT_INVALIDATED",
    "EXIT_TARGET_REACHED",
    "EXIT_TIME_STOP",
    "INCOMPATIBLE",
    "INSUFFICIENT_EVIDENCE",
    "NOT_VIABLE",
    "NO_CLOSED_CANDLE",
    "NO_VIABLE_OPPORTUNITY",
    "PRODUCTION_CERTIFIABLE",
    "PROFILE_CONTRACT_VERSION",
    "PROMOTION",
    "RESEARCH_ONLY",
    "SIGNAL_BUY",
    "SIGNAL_SELL",
    "STABLE_OR_FIAT_BASES",
    "TARGET_BELOW_FRICTION",
    "TARGET_MODEL_VERSION",
    "VIABLE",
    "ProfileError",
    "ProfileIdentity",
    "StrategyEvaluator",
    "StrategyProposal",
    "TargetModel",
    "VolatilityFeatures",
    "base_is_stable_or_fiat",
    "classify_market",
    "market_compatibility",
    "true_range_bps",
    "volatility_features",
]

from .champion import INSUFFICIENT_HISTORY as CHAMPION_INSUFFICIENT_HISTORY

PROFILE_CONTRACT_VERSION = "autofund.strategy-profile.v2"
TARGET_MODEL_VERSION = "autofund.volatility-aware-target.v1"

BPS = Decimal("10000")

# Promotion is not part of this milestone. It is a constant so it cannot be
# flipped by a code path, and it is reported in every profile payload.
PROMOTION = "DISABLED"

DECISION_NO_SIGNAL = "NO_SIGNAL"
DECISION_BUY = "BUY"
DECISION_SELL = "SELL"

# ---------------------------------------------------------------------------
# Evidence / compatibility vocabulary
# ---------------------------------------------------------------------------

# Profile-level viability, always relative to a *specific* market and fee.
NOT_VIABLE = "NOT_VIABLE"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
PRODUCTION_CERTIFIABLE = "PRODUCTION_CERTIFIABLE"
VIABLE = "VIABLE"

# Market compatibility states (spec section 9). Declared, never assumed.
RESEARCH_ONLY = "RESEARCH_ONLY"
COMPATIBLE = "COMPATIBLE"
INCOMPATIBLE = "INCOMPATIBLE"

# Market classes. A stablecoin/fiat-like book is not a volatile-crypto research
# target, and saying so explicitly stops "most volatile coin wins" reasoning.
CLASS_VOLATILE_CRYPTO = "VOLATILE_CRYPTO"
CLASS_STABLE_OR_FIAT = "STABLE_OR_FIAT_LIKE"
CLASS_UNKNOWN = "UNKNOWN"

# Stablecoin and fiat tickers Bitso exposes on *_mxn books. Classified explicitly
# so they are excluded from volatile-crypto strategy research by name rather than
# by a volatility heuristic that could silently misclassify a real crypto asset.
STABLE_OR_FIAT_BASES = frozenset({
    "usd", "usdt", "usdc", "dai", "tusd", "pyusd", "rlusd", "usds", "usdpt", "usat",
    "mxnb", "mxnbj", "brl1", "paxg", "xaut",
})

# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------

# Profile reason codes. The Champion's own codes are reused verbatim (imported
# below) so the frozen profile reports byte-identical evidence to the certified
# Champion rather than a parallel vocabulary that could drift.
NO_CANDLE = "NO_CANDLE"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
SIGNAL_BUY = "SIGNAL_BUY"
SIGNAL_SELL = "SIGNAL_SELL"
TARGET_BELOW_FRICTION = "TARGET_BELOW_FRICTION"
NO_VIABLE_OPPORTUNITY = "NO_VIABLE_OPPORTUNITY"

# Exit vocabulary (MVP 0.2.2). Every exit is attributed to exactly one cause, because
# collapsing them into a single SELL makes it impossible to tell a profit target from a
# stop: they are opposite findings that a bare P&L number cannot separate.
EXIT_TARGET_REACHED = "TARGET_REACHED"
EXIT_INVALIDATED = "STRATEGY_INVALIDATED"
EXIT_TIME_STOP = "TIME_STOP"


class ProfileError(ValueError):
    """A profile definition or proposal violated the contract."""


@dataclass(frozen=True, slots=True)
class VolatilityFeatures:
    """Deterministic market features derived only from candles known at decision time."""

    observations: int
    last_close_mxn: Decimal
    mean_mxn: Decimal
    realized_range_bps: Decimal
    recent_range_bps: Decimal
    atr_bps: Decimal

    @property
    def volatility_bps(self) -> Decimal:
        """Primary volatility measure: average true range as bps of price."""
        return self.atr_bps

    def telemetry(self) -> dict[str, str]:
        return {"observations": str(self.observations),
                "last_close_mxn": str(self.last_close_mxn), "mean_mxn": str(self.mean_mxn),
                "realized_range_bps": str(self.realized_range_bps),
                "recent_range_bps": str(self.recent_range_bps), "atr_bps": str(self.atr_bps),
                "volatility_bps": str(self.volatility_bps)}


@financial
def true_range_bps(previous_close: Decimal, candle: Candle) -> Decimal:
    """True range of one candle as bps of its close.

    Uses `max(high-low, |high-prev_close|, |low-prev_close|)`, the standard ATR
    construction. Only the previous close is used, so nothing here can see forward.
    """
    decimal(previous_close, "previous_close")
    if previous_close <= ZERO or candle.close <= ZERO:
        return ZERO
    candidates = (candle.high - candle.low,
                  abs(candle.high - previous_close),
                  abs(candle.low - previous_close))
    return max(candidates) / candle.close * BPS


@financial
def volatility_features(candles: Sequence[Candle], *, window: int) -> VolatilityFeatures:
    """Features for the last `window` candles. Strictly backward-looking.

    `candles` must end at the current closed candle. Every statistic is computed
    over that slice only, so a decision at candle N can never observe candle N+1.
    """
    if not candles:
        return VolatilityFeatures(0, ZERO, ZERO, ZERO, ZERO, ZERO)
    history = tuple(candles[-window:]) if window > 0 else ()
    if not history:
        return VolatilityFeatures(0, ZERO, ZERO, ZERO, ZERO, ZERO)
    closes = tuple(c.close for c in history)
    mean = sum(closes, ZERO) / Decimal(len(closes))
    last = closes[-1]
    lowest = min(c.low for c in history)
    highest = max(c.high for c in history)
    realized = (highest - lowest) / last * BPS if last > ZERO else ZERO
    recent = history[-1]
    recent_range = ((recent.high - recent.low) / recent.close * BPS
                    if recent.close > ZERO else ZERO)
    true_ranges: list[Decimal] = []
    for index in range(1, len(history)):
        true_ranges.append(true_range_bps(history[index - 1].close, history[index]))
    atr = (sum(true_ranges, ZERO) / Decimal(len(true_ranges))) if true_ranges else recent_range
    return VolatilityFeatures(observations=len(history), last_close_mxn=last, mean_mxn=mean,
                              realized_range_bps=realized, recent_range_bps=recent_range,
                              atr_bps=atr)


@dataclass(frozen=True, slots=True)
class TargetModel:
    """Bounded, deterministic mapping from volatility to a profit target.

    A single fixed target cannot be right in both a quiet and a violent market, so
    the target scales with observed volatility. It is deliberately conservative:

    - floored, so a dead market never produces a tiny target that friction trivially
      consumes into a permanent no-trade;
    - capped, so volatility cannot license an unbounded expectation;
    - linearly interpolated by an explicit multiple of ATR, not curve-fitted;
    - pure function of features, so identical candles always give identical targets.

    This does not make the strategy profitable. It makes the *intent* sized
    realistically against the friction the profile will actually pay.
    """

    atr_multiple: Decimal = Decimal("1.5")
    floor_bps: Decimal = Decimal("60")
    cap_bps: Decimal = Decimal("1200")
    version: str = TARGET_MODEL_VERSION

    def __post_init__(self) -> None:
        for name in ("atr_multiple", "floor_bps", "cap_bps"):
            decimal(getattr(self, name), name)
        if self.atr_multiple <= ZERO:
            raise ProfileError("atr_multiple must be positive")
        if not ZERO < self.floor_bps <= self.cap_bps:
            raise ProfileError("target bounds must satisfy 0 < floor_bps <= cap_bps")

    @financial
    def target_bps(self, features: VolatilityFeatures) -> Decimal:
        raw = features.atr_bps * self.atr_multiple
        return min(max(raw, self.floor_bps), self.cap_bps)

    @financial
    def target_price_mxn(self, entry_reference_mxn: Decimal, features: VolatilityFeatures) -> Decimal:
        decimal(entry_reference_mxn, "entry_reference_mxn")
        if entry_reference_mxn <= ZERO:
            return ZERO
        return entry_reference_mxn * (ONE + self.target_bps(features) / BPS)

    def public(self) -> dict[str, str]:
        return {"version": self.version, "atr_multiple": str(self.atr_multiple),
                "floor_bps": str(self.floor_bps), "cap_bps": str(self.cap_bps)}

    @property
    def fingerprint(self) -> str:
        return fingerprint({"schema": TARGET_MODEL_VERSION, **self.public()})


DEFAULT_TARGET_MODEL = TargetModel()


@dataclass(frozen=True, slots=True)
class StrategyProposal:
    """What a profile proposes. Not an approval, and not an intent.

    This is the contract EconomicEdgeGuard consumes. Every field the guard needs
    to reach an independent verdict is here, so the guard never has to re-derive
    what the strategy meant.
    """

    decision: str
    reason_code: str
    profile_id: str
    strategy_id: str
    strategy_version: str
    strategy_fingerprint: str
    market: str
    entry_reference_mxn: Decimal
    expected_exit_reference_mxn: Decimal
    expected_gross_edge_bps: Decimal
    expected_holding_horizon: int
    target_model: TargetModel
    confidence_evidence: dict[str, str] = field(default_factory=dict)
    features: VolatilityFeatures = field(
        default_factory=lambda: VolatilityFeatures(0, ZERO, ZERO, ZERO, ZERO, ZERO))
    closed_candle_count: int = 0
    position_open: bool = False

    # ---- Risk boundary, known when the position is opened (MVP 0.2.2) ----
    # A challenger must state where its thesis is wrong *before* entering, not discover
    # it from a drawdown. `ZERO` means the profile declares no invalidation price and
    # relies only on its target and time limit; that is permitted but is reported as
    # such, so a profile with no risk boundary cannot be mistaken for one that has one.
    invalidation_price_mxn: Decimal = ZERO
    invalidation_distance_bps: Decimal = ZERO
    max_holding_bars: int = 0

    @property
    def declares_invalidation(self) -> bool:
        return self.invalidation_price_mxn > ZERO

    @property
    def declares_time_stop(self) -> bool:
        return self.max_holding_bars > 0

    @property
    def declared_reward_risk_ratio(self) -> Decimal | None:
        """Reward per unit of declared risk at entry. None when no boundary is declared.

        Computed from the proposal's own numbers, so it is available before the trade is
        taken and cannot be back-filled from what actually happened.
        """
        if not self.declares_invalidation or self.entry_reference_mxn <= ZERO:
            return None
        risk = self.entry_reference_mxn - self.invalidation_price_mxn
        if risk <= ZERO:
            return None
        reward = self.expected_exit_reference_mxn - self.entry_reference_mxn
        return reward / risk

    @property
    def actionable(self) -> bool:
        return self.decision in (DECISION_BUY, DECISION_SELL)

    @property
    def message(self) -> str:
        return PROPOSAL_MESSAGES.get(self.reason_code, self.reason_code)

    @financial
    def telemetry(self) -> dict[str, Any]:
        return {"profile_contract_version": PROFILE_CONTRACT_VERSION,
                "profile_id": self.profile_id, "strategy_id": self.strategy_id,
                "strategy_version": self.strategy_version,
                "strategy_fingerprint": self.strategy_fingerprint, "market": self.market,
                "decision": self.decision, "reason_code": self.reason_code,
                "entry_reference_mxn": str(self.entry_reference_mxn),
                "expected_exit_reference_mxn": str(self.expected_exit_reference_mxn),
                "expected_gross_edge_bps": str(self.expected_gross_edge_bps),
                "expected_holding_horizon": self.expected_holding_horizon,
                "target_model": self.target_model.public(),
                "target_model_fingerprint": self.target_model.fingerprint,
                "confidence_evidence": dict(self.confidence_evidence),
                "features": self.features.telemetry(),
                "closed_candles": self.closed_candle_count,
                "position_open": self.position_open, "promotion": PROMOTION,
                "invalidation_price_mxn": str(self.invalidation_price_mxn),
                "invalidation_distance_bps": str(self.invalidation_distance_bps),
                "max_holding_bars": self.max_holding_bars,
                "declares_invalidation": self.declares_invalidation,
                "declares_time_stop": self.declares_time_stop,
                "declared_reward_risk_ratio": (None if self.declared_reward_risk_ratio is None
                                               else str(self.declared_reward_risk_ratio))}


PROPOSAL_MESSAGES: dict[str, str] = {
    NO_CLOSED_CANDLE: "No closed candle observed yet",
    CHAMPION_INSUFFICIENT_HISTORY: "Insufficient closed-candle evidence",
    ENTRY_CONDITION_NOT_MET: "Entry condition not met",
    EXIT_CONDITION_NOT_MET: "Profile retains its own position",
    SIGNAL_BUY: "BUY",
    SIGNAL_SELL: "SELL",
    TARGET_BELOW_FRICTION: "Intended move is below the estimated round-trip friction",
    NO_VIABLE_OPPORTUNITY: "No economically viable opportunity",
}


class StrategyEvaluator(Protocol):
    """A profile evaluates a past-only context into a proposal."""

    def propose(self, *, candles: Sequence[Candle], quantity: Decimal = ...,
                cost_basis_mxn: Decimal = ..., market: str = ...,
                entry_price_mxn: Decimal = ...) -> StrategyProposal: ...


@dataclass(frozen=True, slots=True)
class ProfileIdentity:
    """Stable, fingerprinted identity of a profile.

    The fingerprint covers the profile's declared parameters and target model, so
    two profiles that behave differently can never share an identity. The
    *strategy* fingerprint is separate: it identifies the decision logic
    independent of any market.
    """

    profile_id: str
    strategy_id: str
    version: str
    parameters: tuple[tuple[str, str], ...] = ()
    target_model: TargetModel = DEFAULT_TARGET_MODEL

    def __post_init__(self) -> None:
        for name in ("profile_id", "strategy_id", "version"):
            if not getattr(self, name):
                raise ProfileError(f"{name} is required")
        object.__setattr__(self, "parameters", tuple(sorted(self.parameters)))

    @property
    def fingerprint(self) -> str:
        """Identity of profile behaviour: parameters + target model."""
        return fingerprint({"schema": PROFILE_CONTRACT_VERSION, "profile_id": self.profile_id,
                            "strategy_id": self.strategy_id, "version": self.version,
                            "parameters": dict(self.parameters),
                            "target_model": self.target_model.public()})

    @property
    def strategy_fingerprint(self) -> str:
        """Identity of the decision logic alone, independent of parameters."""
        return fingerprint({"schema": PROFILE_CONTRACT_VERSION, "strategy_id": self.strategy_id,
                            "version": self.version})

    def public(self) -> dict[str, Any]:
        return {"profile_id": self.profile_id, "strategy_id": self.strategy_id,
                "version": self.version, "parameters": dict(self.parameters),
                "target_model": self.target_model.public(),
                "fingerprint": self.fingerprint,
                "strategy_fingerprint": self.strategy_fingerprint,
                "profile_contract_version": PROFILE_CONTRACT_VERSION}


def base_is_stable_or_fiat(book: str) -> bool:
    """Whether a book's base asset is a stablecoin or fiat-like instrument.

    Accepts either form -- `usd_mxn` or `USD/MXN` -- because a caller that passes the
    wrong form must not silently get a different answer. Returning False for
    `USD/MXN` would classify a fiat pair as volatile crypto and let it into a
    strategy universe it does not belong in, which is exactly the failure this
    predicate exists to prevent.
    """
    text = book.replace("/", "_").lower()
    base = text.split("_", 1)[0] if "_" in text else text
    return base in STABLE_OR_FIAT_BASES


def classify_market(book: str) -> str:
    """Deterministic market class, used to exclude fiat-like books from research.

    Normalises `BASE/QUOTE` and `base_quote` to one form so the two spellings can
    never disagree about an instrument's class.
    """
    parts = book.replace("/", "_").split("_")
    if len(parts) != 2 or not all(parts):
        return CLASS_UNKNOWN
    return CLASS_STABLE_OR_FIAT if base_is_stable_or_fiat(book) else CLASS_VOLATILE_CRYPTO


def market_compatibility(profile_markets: frozenset[str], book: str) -> str:
    """Compatibility of one profile with one market.

    A profile declares the markets it has evidence for. Everything else is
    RESEARCH_ONLY rather than silently assumed equivalent: a BTC profile is not an
    ETH profile and an ETH profile is not an XRP profile.
    """
    if book in profile_markets:
        return COMPATIBLE
    if classify_market(book) == CLASS_STABLE_OR_FIAT:
        return INCOMPATIBLE
    return RESEARCH_ONLY

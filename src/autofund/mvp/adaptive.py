"""Allowlisted live selection and research-only challenger records."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from autofund.decimal_utils import financial
from autofund.replay.serialization import fingerprint

# Evidence required before the engine will even consider proposing a challenger.
# One short session with zero signals is a valid result, not a mandate to change
# the Champion, so the Champion is left untouched until real evidence exists.
MIN_SESSIONS_FOR_CHALLENGER = 5
MIN_SIGNALS_FOR_CHALLENGER = 1

INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
NO_ACTIONABLE_SIGNAL = "NO_ACTIONABLE_SIGNAL"
SIGNALS_OBSERVED = "SIGNALS_OBSERVED"
CHALLENGER_PROPOSED = "CHALLENGER_PROPOSED"


class MarketRegime(StrEnum):
    LOW_VOLATILITY = "LOW_VOLATILITY"
    NORMAL = "NORMAL"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    HIGH_SPREAD = "HIGH_SPREAD"
    POOR_LIQUIDITY = "POOR_LIQUIDITY"
    DEGRADED_DATA = "DEGRADED_DATA"


@dataclass(frozen=True)
class StrategyProfile:
    profile_id: str
    strategy_id: str
    version: str
    parameters: dict[str, str]
    certification_status: str

    @property
    def fingerprint(self) -> str:
        return fingerprint(self)


CHAMPION = StrategyProfile("mean-reversion-safe", "mean_reversion", "0.1", {"market": "btc_mxn"}, "CERTIFIED")


class AdaptiveEngine:
    """May skip or select a certified profile; it cannot alter hard limits."""

    def __init__(self, profiles: tuple[StrategyProfile, ...] = (CHAMPION,)) -> None:
        if any(profile.certification_status != "CERTIFIED" for profile in profiles):
            raise ValueError("live profiles must be certified")
        self._profiles = profiles
        self._champion = profiles[0]
        self.challengers: list[dict[str, Any]] = []
        self.auto_promotion = False
        self.observations: list[dict[str, Any]] = []
        self.sessions_observed = 0
        self.eligible_evaluations = 0
        self.total_signals = 0

    @property
    def champion(self) -> StrategyProfile:
        return self._champion

    def classify(self, *, volatility_bps: float, spread_bps: float, depth_mxn: float,
                 quality: str) -> MarketRegime:
        if quality != "VALID":
            return MarketRegime.DEGRADED_DATA
        if spread_bps > 100:
            return MarketRegime.HIGH_SPREAD
        if depth_mxn < 11:
            return MarketRegime.POOR_LIQUIDITY
        if volatility_bps > 300:
            return MarketRegime.HIGH_VOLATILITY
        if volatility_bps < 20:
            return MarketRegime.LOW_VOLATILITY
        return MarketRegime.NORMAL

    def select(self, regime: MarketRegime) -> StrategyProfile | None:
        if regime in {MarketRegime.HIGH_SPREAD, MarketRegime.POOR_LIQUIDITY, MarketRegime.DEGRADED_DATA}:
            return None
        return self._champion

    def add_challenger(self, candidate: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
        row = {"fingerprint": fingerprint(candidate), "candidate": candidate,
               "evidence": evidence, "status": "RESEARCH_ONLY", "evidence_count": 1}
        self.challengers.append(row)
        return row

    def promote(self, *_: object) -> None:
        raise PermissionError("automatic production promotion is disabled")

    # ------------------------------------------------------------- evidence
    @financial
    def observe_session(self, *, rows: list[dict[str, Any]], metrics: dict[str, Any],
                        stop_reason: str | None = None,
                        scanner: dict[str, Any] | None = None) -> dict[str, Any]:
        """Convert one completed session into deterministic learning evidence.

        The Champion is never modified here, no challenger is fabricated, and the
        engine cannot touch capital, caps, loss limits or risk semantics.
        """
        evaluations = [row for row in rows if row.get("event") == "STRATEGY_EVALUATED"]
        eligible = sum(1 for row in evaluations if row.get("eligible") is True)
        signals = sum(1 for row in rows if row.get("event") == "SIGNAL_GENERATED")
        near = sum(1 for row in rows if row.get("event") == "STRATEGY_EVALUATED" and row.get("near_signal") is True)
        reasons: dict[str, int] = {}
        for row in rows:
            if row.get("event") == "NO_SIGNAL" and row.get("reason_code"):
                reasons[str(row["reason_code"])] = reasons.get(str(row["reason_code"]), 0) + 1
        regimes: dict[str, int] = {}
        for row in evaluations:
            if row.get("market_regime"):
                regimes[str(row["market_regime"])] = regimes.get(str(row["market_regime"]), 0) + 1
        distances = sorted(Decimal(str(row["distance_to_signal"])) for row in evaluations
                           if row.get("distance_to_signal") not in (None, ""))
        self.sessions_observed += 1
        self.eligible_evaluations += eligible
        self.total_signals += signals

        observations: list[str] = []
        if eligible > 0 and signals == 0:
            observations.append("ZERO_SIGNALS_ACROSS_ELIGIBLE_EVALUATIONS")
        if near:
            observations.append("N_EVALUATIONS_WITHIN_DISTANCE_OF_ENTRY")
        if not any(row.get("event") == "ORDER_INTENT_CREATED" for row in rows):
            observations.append("EXECUTION_PATH_NOT_EXERCISED")
        if scanner and scanner.get("eligible_count"):
            observations.append("SCANNER_FOUND_ELIGIBLE_ALTERNATIVE_MARKETS")
        if scanner and scanner.get("degraded"):
            observations.append("MARKET_SCANNER_DEGRADED")

        # Insufficient evidence is a first-class, valid learning result.
        if self.sessions_observed < MIN_SESSIONS_FOR_CHALLENGER or (self.total_signals < MIN_SIGNALS_FOR_CHALLENGER
                                                                   and not (scanner and scanner.get("eligible_count"))):
            classification = INSUFFICIENT_EVIDENCE
        elif signals > 0:
            classification = SIGNALS_OBSERVED
        else:
            classification = NO_ACTIONABLE_SIGNAL

        observation = {"classification": classification, "sessions_observed": self.sessions_observed,
                       "eligible_evaluations": eligible, "signals": signals, "near_signal_count": near,
                       "reason_distribution": dict(sorted(reasons.items())),
                       "regime_distribution": dict(sorted(regimes.items())),
                       "distance_to_signal_min": str(distances[0]) if distances else None,
                       "stop_reason": stop_reason,
                       "net_pnl_mxn": str(metrics.get("net_pnl_mxn", "0")),
                       "fees_mxn": str(metrics.get("fees_mxn", "0")),
                       "champion_fingerprint": self._champion.fingerprint,
                       "champion_changed": False, "auto_promotion": self.auto_promotion,
                       "challengers_created": 0, "observations": observations,
                       "scanner": scanner or {}}
        # A challenger is only ever proposed from a separately defined research
        # path; this bridge never invents one from a single session.
        self.observations.append(observation)
        return observation

    def learning_view(self, *, scanner: dict[str, Any] | None = None) -> dict[str, Any]:
        """Structured Learning-page contract. Contains no fabricated data."""
        reasons: dict[str, int] = {}
        regimes: dict[str, int] = {}
        distances: list[Decimal] = []
        near = 0
        for observation in self.observations:
            for key, value in observation.get("reason_distribution", {}).items():
                reasons[key] = reasons.get(key, 0) + int(value)
            for key, value in observation.get("regime_distribution", {}).items():
                regimes[key] = regimes.get(key, 0) + int(value)
            near += int(observation.get("near_signal_count", 0))
            if observation.get("distance_to_signal_min") is not None:
                distances.append(Decimal(str(observation["distance_to_signal_min"])))
        return {"champion": {"profile_id": self._champion.profile_id, "strategy_id": self._champion.strategy_id,
                             "version": self._champion.version, "fingerprint": self._champion.fingerprint,
                             "certification_status": self._champion.certification_status},
                "sessions_observed": self.sessions_observed,
                "eligible_evaluations": self.eligible_evaluations,
                "signals": self.total_signals,
                "reason_distribution": dict(sorted(reasons.items())),
                "regime_distribution": dict(sorted(regimes.items())),
                "near_signal_count": near,
                "distance_to_signal_min": str(min(distances)) if distances else None,
                "observations": self.observations,
                "challengers": [{"fingerprint": row["fingerprint"], "status": row["status"],
                                 "evidence_count": row.get("evidence_count", 0)}
                                for row in self.challengers],
                "challenger_count": len(self.challengers),
                "promotion": "MANUAL", "auto_promotion": "DISABLED",
                "market_promotion": "DISABLED",
                "min_sessions_for_challenger": MIN_SESSIONS_FOR_CHALLENGER,
                "min_signals_for_challenger": MIN_SIGNALS_FOR_CHALLENGER,
                "scanner": scanner or {}}

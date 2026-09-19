"""Allowlisted live selection and research-only challenger records."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from autofund.replay.serialization import fingerprint


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
               "evidence": evidence, "status": "RESEARCH_ONLY"}
        self.challengers.append(row)
        return row

    def promote(self, *_: object) -> None:
        raise PermissionError("automatic production promotion is disabled")

"""Directional observation only. No portfolio mutation, sizing, orders or fills."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from autofund.decimal_utils import ZERO
from autofund.replay.data import Candle
from autofund.replay.strategy import (
    PortfolioSnapshot,
    SimpleMeanReversionV0,
    StrategyContext,
)

from .models import LiveCandleUpdate


class ShadowAction(StrEnum):
    WOULD_BUY = "WOULD_BUY"
    WOULD_SELL = "WOULD_SELL"
    NO_ACTION = "NO_ACTION"


@dataclass(frozen=True, slots=True)
class ShadowDecision:
    market: str
    candle_open: datetime
    candle_close: datetime
    action: ShadowAction


class ShadowObserver:
    def __init__(self, strategy: SimpleMeanReversionV0 | None = None) -> None:
        self.strategy = strategy or SimpleMeanReversionV0()
        self._history: tuple[Candle, ...] = ()
        # No balances exist in F2. Neutral, fixed inputs merely enable F1's callback.
        self._neutral = PortfolioSnapshot(ZERO, ZERO, ZERO, ZERO, ZERO)

    def on_closed(self, event: LiveCandleUpdate) -> ShadowDecision:
        if not event.is_closed:
            raise ValueError("shadow callback requires a closed candle")
        self._history = (*self._history, event.to_candle())
        # Non-MXN observations retain original prices; the sentinel is only the
        # syntactic label needed by F1's OrderIntent. No amount leaves this bridge.
        context_market = event.market if event.market.endswith("/MXN") else "SHADOW/MXN"
        candidate = self.strategy.on_candle(
            StrategyContext(
                event.open_time, context_market, self._history, self._neutral
            )
        )
        action = (
            ShadowAction.NO_ACTION
            if candidate is None
            else ShadowAction("WOULD_" + candidate.side.value)
        )
        return ShadowDecision(event.market, event.open_time, event.close_time, action)

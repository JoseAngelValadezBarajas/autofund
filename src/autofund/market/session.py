import logging
from dataclasses import dataclass
from datetime import datetime

from autofund.replay.data import Candle, HistoricalDataset
from autofund.replay.serialization import fingerprint
from autofund.replay.strategy import SimpleMeanReversionV0

from .errors import ConfigurationError, UnsupportedQuote
from .models import (
    LiveCandleUpdate,
    MarketInfo,
    QualityStatus,
    SessionQualityReport,
    SourceItem,
)
from .quality import SequenceValidator
from .shadow import ShadowAction, ShadowDecision, ShadowObserver

_LOG = logging.getLogger(__name__)


def normalized_fingerprint(
    market: str, interval: str, events: tuple[LiveCandleUpdate, ...]
) -> str:
    return fingerprint(
        {
            "schema": "autofund.normalized-session.v1",
            "market": market,
            "interval": interval,
            "events": events,
        }
    )


@dataclass(frozen=True, slots=True)
class LiveSessionResult:
    schema_version: str
    session_id: str
    source: str
    market: str
    interval: str
    started_at: datetime
    ended_at: datetime
    raw_messages: int
    normalized_updates: int
    closed_candles: int
    strategy_signal_count: int
    quality: SessionQualityReport
    normalized_session_fingerprint: str
    closed_candle_fingerprint: str
    signals_fingerprint: str
    first_event: datetime | None
    last_event: datetime | None
    stop_reason: str
    capture_path: str | None
    events: tuple[LiveCandleUpdate, ...]
    candles: tuple[Candle, ...]
    decisions: tuple[ShadowDecision, ...]

    def report(self) -> dict[str, object]:
        from dataclasses import fields

        return {
            field.name: getattr(self, field.name)
            for field in fields(self)
            if field.name not in ("events", "candles", "decisions")
        }

    def to_f1_dataset(self, *, allow_degraded: bool = False) -> HistoricalDataset:
        if self.quality.status is QualityStatus.INVALID or (
            self.quality.status is QualityStatus.DEGRADED and not allow_degraded
        ):
            raise ConfigurationError(
                "research conversion requires VALID data (DEGRADED needs explicit opt-in)"
            )
        if not self.market.endswith("/MXN"):
            raise UnsupportedQuote("F1 accounts only in MXN; no implicit FX conversion")
        return HistoricalDataset(self.market, self.candles)


class ObservationPipeline:
    def __init__(
        self,
        info: MarketInfo,
        interval: str,
        *,
        strategy: SimpleMeanReversionV0 | None = None,
        max_invalid_messages: int = 3,
    ) -> None:
        if type(max_invalid_messages) is not int or max_invalid_messages <= 0:
            raise ConfigurationError("max_invalid_messages must be positive")
        self.info, self.interval = info, interval
        self.validator = SequenceValidator(info.market, interval, max_invalid_messages)
        self.observer = ShadowObserver(strategy)
        self.events: list[LiveCandleUpdate] = []
        self.candles: list[Candle] = []
        self.decisions: list[ShadowDecision] = []
        self.raw_count = 0

    def feed(self, item: SourceItem) -> None:
        if item.raw_message is not None:
            self.raw_count += 1
        if item.notice is not None:
            self.validator.notice(item.notice)
            if item.notice.kind == "invalid_message":
                _LOG.warning("invalid_message reason=%s", item.notice.reason)
        if item.event is not None:
            self.events.append(item.event)
            old_issue_count = len(self.validator.issues)
            if self.validator.accept(item.event):
                self.candles.append(item.event.to_candle())
                decision = self.observer.on_closed(item.event)
                self.decisions.append(decision)
                _LOG.info(
                    "closed_candle market=%s time=%s decision=%s",
                    item.event.market,
                    item.event.open_time.isoformat(),
                    decision.action,
                )
            for issue in self.validator.issues[old_issue_count:]:
                _LOG.warning("%s reason=%s", issue.kind, issue.reason)

    def result(
        self,
        *,
        session_id: str,
        source: str,
        started_at: datetime,
        ended_at: datetime,
        stop_reason: str,
        capture_path: str | None = None,
    ) -> LiveSessionResult:
        events, candles, decisions = (
            tuple(self.events),
            tuple(self.candles),
            tuple(self.decisions),
        )
        return LiveSessionResult(
            "autofund.market-session.v1",
            session_id,
            source,
            self.info.market,
            self.interval,
            started_at,
            ended_at,
            self.raw_count,
            len(events),
            len(candles),
            sum(d.action is not ShadowAction.NO_ACTION for d in decisions),
            self.validator.report(),
            normalized_fingerprint(self.info.market, self.interval, events),
            fingerprint(
                {
                    "market": self.info.market,
                    "interval": self.interval,
                    "candles": candles,
                }
            ),
            fingerprint(decisions),
            events[0].source_event_time if events else None,
            events[-1].source_event_time if events else None,
            stop_reason,
            capture_path,
            events,
            candles,
            decisions,
        )

from datetime import datetime, timedelta

from .models import (
    LiveCandleUpdate,
    MarketDataGap,
    QualityStatus,
    SessionQualityReport,
    Severity,
    SourceNotice,
    interval_seconds,
)


class SequenceValidator:
    def __init__(
        self, market: str, interval: str, max_invalid_messages: int = 3
    ) -> None:
        self.market, self.interval = market, interval
        self._step = timedelta(seconds=interval_seconds(interval))
        self._max_invalid = max_invalid_messages
        self._closed: dict[tuple[str, str, datetime], LiveCandleUpdate] = {}
        self._latest_open: datetime | None = None
        self._latest_event: datetime | None = None
        self._last_closed: datetime | None = None
        self.duplicates = self.gaps = self.out_of_order = self.invalid_messages = 0
        self.reconnects = self.stale_events = 0
        self.invalid = False
        self.issues: list[SourceNotice] = []
        self.gap_events: list[MarketDataGap] = []

    def notice(self, notice: SourceNotice) -> None:
        if notice.kind == "invalid_message":
            self.invalid_messages += 1
        elif notice.kind == "reconnect":
            self.reconnects += 1
        elif notice.kind == "stale":
            self.stale_events += 1
        if (
            notice.severity is Severity.FATAL
            or self.invalid_messages >= self._max_invalid
        ):
            self.invalid = True
        if notice.severity is not Severity.NORMAL:
            self.issues.append(notice)

    def accept(self, event: LiveCandleUpdate) -> bool:
        """Return True exactly once per coherent, ordered, closed candle."""
        if self.invalid:
            return False
        if (event.market, event.interval) != (self.market, self.interval):
            self.notice(
                SourceNotice(
                    "wrong_stream", Severity.FATAL, "unexpected market or interval"
                )
            )
            return False
        previous = self._closed.get(event.identity)
        if previous is not None:
            if event.is_closed:
                if event.closed_content() != previous.closed_content():
                    self.notice(
                        SourceNotice(
                            "contradictory_closed",
                            Severity.FATAL,
                            "closed OHLCV changed",
                        )
                    )
                else:
                    self.duplicates += 1
                    self.issues.append(SourceNotice("duplicate", Severity.WARNING))
            else:
                self.out_of_order += 1
                self.issues.append(SourceNotice("late_partial", Severity.WARNING))
            return False
        if (self._latest_open is not None and event.open_time < self._latest_open) or (
            self._latest_event is not None
            and event.source_event_time < self._latest_event
        ):
            self.out_of_order += 1
            self.notice(
                SourceNotice(
                    "out_of_order",
                    Severity.FATAL,
                    "candle or event timestamp regressed",
                )
            )
            return False
        self._latest_open, self._latest_event = event.open_time, event.source_event_time
        if not event.is_closed:
            return False
        if self._last_closed is not None:
            expected = self._last_closed + self._step
            if event.open_time > expected:
                missing = (event.open_time - expected) // self._step
                self.gaps += missing
                self.gap_events.append(
                    MarketDataGap(expected, event.open_time, missing)
                )
                self.issues.append(
                    SourceNotice(
                        "gap", Severity.WARNING, f"missing {missing} closed candles"
                    )
                )
        self._closed[event.identity] = event
        self._last_closed = event.open_time
        return True

    def report(self) -> SessionQualityReport:
        harmful = any(issue.kind != "duplicate" for issue in self.issues)
        status = (
            QualityStatus.INVALID
            if self.invalid
            else QualityStatus.DEGRADED
            if harmful or not self._closed
            else QualityStatus.VALID
        )
        return SessionQualityReport(
            status,
            len(self._closed),
            self.duplicates,
            self.gaps,
            self.out_of_order,
            self.invalid_messages,
            self.reconnects,
            self.stale_events,
            tuple(self.issues),
            tuple(self.gap_events),
        )

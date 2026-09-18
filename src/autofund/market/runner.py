import asyncio
import logging
from contextlib import aclosing
from pathlib import Path
from uuid import uuid4

from autofund.replay.serialization import canonical_json
from autofund.replay.strategy import SimpleMeanReversionV0

from .capture import RawCaptureWriter, RecordedMarketDataSource
from .clock import Clock, SystemClock
from .errors import CaptureIntegrityError, ConfigurationError
from .models import Severity, SourceItem, SourceNotice
from .session import LiveSessionResult, ObservationPipeline
from .source import PublicMarketDataSource

_LOG = logging.getLogger(__name__)


class LiveMarketRunner:
    def __init__(
        self,
        *,
        clock: Clock | None = None,
        strategy: SimpleMeanReversionV0 | None = None,
        max_invalid_messages: int = 3,
    ) -> None:
        self._clock = clock or SystemClock()
        self._strategy = strategy or SimpleMeanReversionV0()
        self._max_invalid = max_invalid_messages

    async def run(
        self,
        source: PublicMarketDataSource,
        *,
        output: str | Path,
        closed_candles: int,
        max_seconds: int = 120,
    ) -> LiveSessionResult:
        if (
            type(closed_candles) is not int
            or closed_candles <= 0
            or type(max_seconds) is not int
            or max_seconds <= 0
        ):
            raise ConfigurationError(
                "closed_candles and max_seconds must be positive integers"
            )
        info = await source.get_market_info()
        pipeline = ObservationPipeline(
            info,
            source.interval,
            strategy=self._strategy,
            max_invalid_messages=self._max_invalid,
        )
        session_id, started = str(uuid4()), self._clock.now()
        writer = RawCaptureWriter(
            Path(output),
            source=source.source_name,
            info=info,
            interval=source.interval,
            session_id=session_id,
            started_at=started,
            metadata_raw=source.metadata_raw,
            strategy=self._strategy,
            max_invalid_messages=self._max_invalid,
        )
        stop_reason = "source_exhausted"
        try:
            async with (
                asyncio.timeout(max_seconds),
                aclosing(source.stream_candles()) as stream,
            ):
                async for item in stream:
                    writer.append(item)
                    pipeline.feed(item)
                    if pipeline.validator.invalid:
                        stop_reason = "session_invalid"
                        break
                    if len(pipeline.candles) == closed_candles:
                        stop_reason = "closed_candle_target"
                        break
        except asyncio.CancelledError:
            # asyncio.run's first Ctrl+C cancels this task; preserve a valid partial bundle.
            stop_reason = "interrupted"
        except TimeoutError:
            stop_reason = "duration_limit"
            notice = SourceItem(
                self._clock.now(),
                notice=SourceNotice(
                    "duration_limit", Severity.WARNING, "capture time budget exhausted"
                ),
            )
            writer.append(notice)
            pipeline.feed(notice)
        except Exception as exc:
            notice = SourceItem(
                self._clock.now(),
                notice=SourceNotice(
                    "capture_error", Severity.FATAL, type(exc).__name__
                ),
            )
            writer.append(notice)
            pipeline.feed(notice)
            stop_reason = "error"
            raise
        finally:
            result = pipeline.result(
                session_id=session_id,
                source=source.source_name,
                started_at=started,
                ended_at=self._clock.now(),
                stop_reason=stop_reason,
                capture_path=str(output),
            )
            writer.finish(result)
        _LOG.info(
            "capture_complete closed=%s quality=%s",
            result.closed_candles,
            result.quality.status,
        )
        return result


async def replay_capture(path: str | Path) -> LiveSessionResult:
    source = RecordedMarketDataSource(path)
    pipeline = ObservationPipeline(
        source.info,
        source.interval,
        strategy=source.strategy,
        max_invalid_messages=source.max_invalid_messages,
    )
    async for item in source.stream_candles():
        pipeline.feed(item)
    from datetime import datetime

    result = pipeline.result(
        session_id=source.session_id,
        source=source.source_name,
        started_at=source.started_at,
        ended_at=datetime.fromisoformat(str(source.expected_report["ended_at"])),
        stop_reason=str(source.expected_report["stop_reason"]),
        capture_path=str(path),
    )
    # Recompute every deterministic report value, not just a supplied fingerprint.
    actual = result.report()
    for key in actual:
        if key == "capture_path":
            continue
        if canonical_json(actual[key]) != canonical_json(
            source.expected_report.get(key)
        ):
            raise CaptureIntegrityError(f"replayed report mismatch: {key}")
    _LOG.info(
        "parity_result status=PASS fingerprint=%s",
        result.normalized_session_fingerprint,
    )
    return result

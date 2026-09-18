import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from autofund.observer.client import BitsoProductionReadOnlyClient
from autofund.observer.errors import MarketDataInvalid, ReadUnavailable
from autofund.observer.models import ShadowFee
from autofund.observer.source import BitsoPublicMarketDataSource, MarketNotice

from .aggregation import minute
from .capture import ShadowCapture, read_capture, restore_session
from .config import ShadowConfig
from .session import ShadowSession


def run_public(
    *,
    output: Path,
    book: str = "btc_mxn",
    config: ShadowConfig = ShadowConfig(),
    duration: int = 900,
    closed_candles: int | None = None,
    fee: ShadowFee | None = None,
    resume: bool = False,
) -> dict[str, object]:
    if duration <= 0 or (closed_candles is not None and closed_candles <= 0):
        raise MarketDataInvalid("finite positive duration/candle target required")
    client = BitsoProductionReadOnlyClient()  # Public only; no credential source.
    if resume:
        header, _, _ = read_capture(output, verify_manifest=False)
        session = restore_session(header)
        if session.config != config or session.limits.book != book:
            raise MarketDataInvalid("resume must keep original config/book")
    else:
        limits, public_fee = client.market_info(book)
        session = ShadowSession(
            config,
            limits,
            fee or public_fee,
            minute(datetime.now(UTC)) + timedelta(minutes=1),
        )
    capture = ShadowCapture(output, session, resume=resume)
    session = capture.session
    marker = max(session.aggregator.seen, default=None)
    source = BitsoPublicMarketDataSource(book, client=client, marker=marker)
    started, reason, failures = time.monotonic(), "duration", 0
    target = len(session.candles) + closed_candles if closed_candles else None
    try:
        while time.monotonic() - started < duration:
            try:
                frame = source.poll()
                capture.consume(frame)
                failures = 0
            except ReadUnavailable:
                capture.consume(MarketNotice(datetime.now(UTC), "read_unavailable"))
                failures += 1
                if failures >= 3:
                    reason = "network_unavailable"
                    break
                time.sleep(min(30, config.poll_seconds * 2**failures))
                continue
            except MarketDataInvalid:
                capture.consume(
                    MarketNotice(datetime.now(UTC), "invalid_payload", "INVALID")
                )
                reason = "MARKET_DATA_HALT"
                break
            if session.halt_reason:
                reason = session.halt_reason
                break
            if target is not None and len(session.candles) >= target:
                reason = "closed_candles"
                break
            time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        reason = "interrupted"
    except Exception:
        reason = "error"
        raise
    finally:
        try:
            result = capture.finalize(reason)
        finally:
            capture.close()
    return result

import logging
from collections.abc import AsyncGenerator
from datetime import datetime

from .binance_normalizer import is_server_shutdown, normalize_kline, parse_market_info
from .clock import Clock, NetworkPolicy, StaleMonitor, SystemClock
from .errors import ConfigurationError, InvalidMessage, NetworkUnavailable, WrongStream
from .models import (
    MarketInfo,
    Severity,
    SourceItem,
    SourceNotice,
    interval_seconds,
    validate_symbol,
)
from .transport import BinancePublicTransport, PublicTransport

_LOG = logging.getLogger(__name__)


class BinancePublicMarketDataSource:
    source_name = "binance_spot"

    def __init__(
        self,
        symbol: str,
        interval: str = "1m",
        *,
        transport: PublicTransport | None = None,
        clock: Clock | None = None,
        policy: NetworkPolicy | None = None,
    ) -> None:
        self.symbol = validate_symbol(symbol)
        interval_seconds(interval)
        self.interval = interval
        self.policy = policy or NetworkPolicy()
        self._transport = transport or BinancePublicTransport(
            self.policy.connect_timeout
        )
        self._clock = clock or SystemClock()
        self._info: MarketInfo | None = None
        self.metadata_raw: str | None = None

    async def get_market_info(self) -> MarketInfo:
        if self._info is not None:
            return self._info
        for attempt in range(self.policy.metadata_attempts):
            delay = self.policy.delay(attempt)
            try:
                reply = await self._transport.exchange_info(self.symbol)
            except (OSError, TimeoutError):
                reply = None
            if reply is not None:
                if reply.status == 200:
                    info = parse_market_info(reply.body, self.symbol)
                    if info.status != "TRADING":
                        raise ConfigurationError("symbol is not currently TRADING")
                    self.metadata_raw, self._info = reply.body, info
                    return info
                if reply.status not in (429, 500, 502, 503, 504):
                    raise ConfigurationError(
                        f"public metadata HTTP {reply.status}; unavailable symbol/access"
                    )
                if reply.retry_after is not None:
                    # Do not shorten a server-directed cooldown to the backoff cap.
                    if reply.retry_after > self.policy.maximum_delay:
                        raise NetworkUnavailable(
                            f"server requests cooldown of {reply.retry_after}s; stop, do not retry early"
                        )
                    delay = max(delay, reply.retry_after)
            if attempt + 1 < self.policy.metadata_attempts:
                await self._clock.sleep(delay)
        raise NetworkUnavailable("public metadata retry budget exhausted")

    def _notice(
        self, kind: str, severity: Severity = Severity.WARNING, reason: str = ""
    ) -> SourceItem:
        _LOG.log(
            logging.INFO if severity is Severity.NORMAL else logging.WARNING,
            "%s reason=%s",
            kind,
            reason,
        )
        return SourceItem(
            self._clock.now(), notice=SourceNotice(kind, severity, reason)
        )

    async def stream_candles(self) -> AsyncGenerator[SourceItem, None]:
        info = await self.get_market_info()
        latest_event_time: datetime | None = None
        for attempt in range(self.policy.max_reconnects + 1):
            if attempt:
                await self._clock.sleep(self.policy.delay(attempt - 1))
                yield self._notice("reconnect")
            try:
                async with self._transport.connect(
                    self.symbol, self.interval
                ) as socket:
                    yield self._notice("connected", Severity.NORMAL)
                    yield self._notice(
                        "subscribed", Severity.NORMAL, "URL subscription"
                    )
                    monitor = StaleMonitor(self._clock, self.policy.stale_after)
                    while True:
                        if monitor.is_stale:
                            raise TimeoutError
                        raw = await socket.receive(monitor.remaining)
                        received = self._clock.now()
                        try:
                            if is_server_shutdown(raw):
                                yield SourceItem(
                                    received,
                                    raw_message=raw,
                                    notice=SourceNotice(
                                        "disconnect",
                                        Severity.WARNING,
                                        "server_shutdown",
                                    ),
                                )
                                break
                            event = normalize_kline(raw, info, self.interval)
                        except WrongStream as exc:
                            yield SourceItem(
                                received,
                                raw_message=raw,
                                notice=SourceNotice(
                                    "invalid_message", Severity.FATAL, str(exc)
                                ),
                            )
                            return
                        except InvalidMessage as exc:
                            yield SourceItem(
                                received,
                                raw_message=raw,
                                notice=SourceNotice(
                                    "invalid_message", Severity.WARNING, str(exc)
                                ),
                            )
                            continue
                        if (
                            latest_event_time is None
                            or event.source_event_time > latest_event_time
                        ):
                            monitor.seen()
                            latest_event_time = event.source_event_time
                        yield SourceItem(received, event=event, raw_message=raw)
            except TimeoutError:
                yield self._notice("stale", reason="no valid update within stale_after")
            except OSError as exc:
                yield self._notice("disconnect", reason=str(exc))
            except ConfigurationError as exc:
                yield self._notice("configuration_error", Severity.FATAL, str(exc))
                return
        yield self._notice(
            "retry_exhausted", Severity.FATAL, "reconnect budget exhausted"
        )

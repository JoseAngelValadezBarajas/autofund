"""Fixed market-data host, GET-only allowlist, and sanitized errors.

The trading host is deliberately not representable here: it is not a constant, not a parameter and
not in the allowlist. Reaching it would need a new, reviewed change.
"""

import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from autofund.replay.data import Candle

from . import parsing
from .errors import (
    AlpacaAuthenticationError,
    AlpacaError,
    AlpacaRateLimitError,
    AlpacaSubscriptionError,
    AlpacaTransportError,
    AlpacaValidationError,
)
from .models import (
    AlpacaBarSeries,
    Quote,
    classify_gaps,
    validate_adjustment,
    validate_feed,
    validate_symbol,
)

DATA_BASE_URL = "https://data.alpaca.markets"
BARS_PATH = "/v2/stocks/bars"
QUOTES_PATH = "/v2/stocks/quotes/latest"
KEY_ENV = "AUTOFUND_ALPACA_DATA_API_KEY"
SECRET_ENV = "AUTOFUND_ALPACA_DATA_API_SECRET"  # nosec B105 - the NAME of an environment variable, not a secret

MAX_SYMBOLS = 50
MAX_PAGES = 200
PAGE_LIMIT = "10000"
_ALLOWED_QUERY = frozenset(
    {"symbols", "timeframe", "start", "end", "adjustment", "feed", "limit", "page_token", "sort"}
)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_=+/.-]{1,512}\Z")


@dataclass(frozen=True)
class AlpacaCredentials:
    key_id: str = field(repr=False)
    secret: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.key_id, self.secret):
            if not isinstance(value, str) or not value.strip() or not value.isascii():
                raise AlpacaAuthenticationError("Alpaca credentials are missing or malformed")
        # Paper keys begin with PK; live-account keys begin with AK. This package is paper-only,
        # so a live key is refused outright rather than used for "just reading".
        if not self.key_id.startswith("PK"):
            raise AlpacaAuthenticationError("only Alpaca paper keys (PK...) are accepted")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AlpacaCredentials":
        env = os.environ if environ is None else environ
        key, secret = env.get(KEY_ENV, ""), env.get(SECRET_ENV, "")
        if not key or not secret:
            raise AlpacaAuthenticationError(f"set {KEY_ENV} and {SECRET_ENV} in the environment")
        return cls(key, secret)


@dataclass(frozen=True)
class AlpacaRequest:
    method: str
    path: str
    credentials: AlpacaCredentials = field(repr=False)


@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: bytes = field(repr=False)
    retry_after: float = 60


def validate_request(method: str, path: str) -> None:
    """Allow exactly one read: historical stock bars. Everything else is refused."""
    if method != "GET":
        raise AlpacaValidationError("only GET is permitted")
    parts = urlsplit(path)
    if parts.scheme or parts.netloc or parts.fragment or "%" in parts.path:
        raise AlpacaValidationError("invalid request path")
    if parts.path not in (BARS_PATH, QUOTES_PATH):
        raise AlpacaValidationError("operation outside the market-data allowlist")
    pairs = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    keys = [k for k, _ in pairs]
    allowed = _ALLOWED_QUERY if parts.path == BARS_PATH else frozenset({"symbols", "feed"})
    if len(keys) != len(set(keys)) or set(keys) - allowed:
        raise AlpacaValidationError("unsupported or repeated query field")
    query = dict(pairs)
    if parts.path == QUOTES_PATH:
        symbols = query.get("symbols", "").split(",")
        if not 1 <= len(symbols) <= MAX_SYMBOLS:
            raise AlpacaValidationError("between 1 and 50 symbols are required")
        for symbol in symbols:
            validate_symbol(symbol)
        validate_feed(query.get("feed", ""))
        return
    if query.get("timeframe") != "1Day":
        raise AlpacaValidationError("only 1Day bars are supported")
    symbols = query.get("symbols", "").split(",")
    if not 1 <= len(symbols) <= MAX_SYMBOLS:
        raise AlpacaValidationError("between 1 and 50 symbols are required")
    for symbol in symbols:
        validate_symbol(symbol)
    validate_adjustment(query.get("adjustment", ""))
    validate_feed(query.get("feed", ""))
    for name in ("start", "end"):
        if name in query and not _DATE.fullmatch(query[name]):
            raise AlpacaValidationError(f"{name} must be YYYY-MM-DD")
    if "page_token" in query and not _TOKEN.fullmatch(query["page_token"]):
        raise AlpacaValidationError("malformed page token")


class AlpacaTransport(Protocol):
    def send(self, request: AlpacaRequest) -> TransportResponse: ...


class HttpxAlpacaTransport:
    def send(self, request: AlpacaRequest) -> TransportResponse:
        validate_request(request.method, request.path)
        headers = {
            "APCA-API-KEY-ID": request.credentials.key_id,
            "APCA-API-SECRET-KEY": request.credentials.secret,
            "Accept": "application/json",
        }
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20, connect=5),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.request(
                    request.method, DATA_BASE_URL + request.path, headers=headers
                )
                retry = response.headers.get("Retry-After", "60")
                return TransportResponse(
                    response.status_code,
                    response.content,
                    float(retry) if retry.isdigit() else 60,
                )
        except (httpx.HTTPError, OSError, ValueError):
            raise AlpacaTransportError("market-data transport failed") from None


class AlpacaDataClient:
    """Read-only historical daily bars."""

    def __init__(
        self,
        credentials: AlpacaCredentials,
        *,
        transport: AlpacaTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credentials = credentials
        self._transport = transport or HttpxAlpacaTransport()
        self._sleep, self._monotonic = sleep, monotonic
        self._next_request = 0.0
        self._auth_failed = False

    def _get(self, query: dict[str, str], *, endpoint: str = BARS_PATH) -> bytes:
        path = endpoint + "?" + urlencode(query)
        validate_request("GET", path)
        if self._auth_failed:
            raise AlpacaAuthenticationError("market-data authentication unavailable")
        attempts = 3
        for attempt in range(attempts):
            delay = max(0.0, self._next_request - self._monotonic())
            if delay:
                self._sleep(delay)
            # 200 requests/minute on the free plan; stay well under it.
            self._next_request = self._monotonic() + 0.4
            try:
                response = self._transport.send(AlpacaRequest("GET", path, self._credentials))
                if response.status == 403 and b"subscription" in response.body.lower():
                    # A plan limit, not a credential problem: it must not latch authentication.
                    raise AlpacaSubscriptionError("the data plan does not permit this request")
                if response.status in (401, 403):
                    self._auth_failed = True
                    raise AlpacaAuthenticationError("market-data authentication failed")
                if response.status == 429:
                    self._next_request = self._monotonic() + max(60, response.retry_after)
                    raise AlpacaRateLimitError("market-data rate limit")
                if response.status >= 500:
                    raise AlpacaTransportError("market-data server unavailable")
                if response.status != 200:
                    raise AlpacaValidationError(f"market-data rejected request; http={response.status}")
                return response.body
            except AlpacaTransportError:
                if attempt + 1 == attempts:
                    raise AlpacaTransportError("bounded read attempts exhausted") from None
                self._next_request = max(self._next_request, self._monotonic() + 2**attempt)
        raise AlpacaTransportError("read attempts exhausted")

    def get_daily_bars(
        self,
        symbols: list[str],
        start: date,
        end: date,
        *,
        adjustment: str,
        feed: str,
    ) -> dict[str, AlpacaBarSeries]:
        """Daily bars. ``adjustment`` and ``feed`` are required: there is no silent default."""
        if not symbols or len(set(symbols)) != len(symbols):
            raise AlpacaValidationError("symbols must be a nonempty list without duplicates")
        if not isinstance(start, date) or not isinstance(end, date) or start > end:
            raise AlpacaValidationError("start must not be after end")
        for symbol in symbols:
            validate_symbol(symbol)
        validate_adjustment(adjustment)
        validate_feed(feed)
        collected: dict[str, list[Candle]] = {s: [] for s in symbols}
        token: str | None = None
        for _ in range(MAX_PAGES):
            query = {
                "symbols": ",".join(symbols),
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "adjustment": adjustment,
                "feed": feed,
                "limit": PAGE_LIMIT,
                "sort": "asc",
            }
            if token:
                query["page_token"] = token
            rows, token = parsing.page(self._get(query))
            for symbol, candles in rows.items():
                if symbol not in collected:
                    raise AlpacaError("response contained a symbol that was not requested")
                collected[symbol].extend(candles)
            if token is None:
                break
        else:
            raise AlpacaTransportError("pagination did not terminate")
        empty = [s for s, c in collected.items() if not c]
        if empty:
            raise AlpacaValidationError("no bars returned for: " + ",".join(empty))
        ordered: dict[str, tuple[Candle, ...]] = {}
        for symbol, candles in collected.items():
            stamps = [c.timestamp for c in candles]
            if stamps != sorted(stamps) or len(set(stamps)) != len(stamps):
                raise AlpacaError("bars were duplicated or out of order")
            ordered[symbol] = tuple(candles)
        gaps = classify_gaps(ordered)
        return {
            symbol: AlpacaBarSeries(
                symbol=symbol,
                adjustment=adjustment,
                feed=feed,
                candles=candles,
                gaps=gaps[symbol][0],
                unclassified_weekdays=gaps[symbol][1],
            )
            for symbol, candles in ordered.items()
        }

    def get_latest_quotes(self, symbols: list[str], *, feed: str) -> dict[str, Quote]:
        """The latest top-of-book quote per symbol, for measuring spreads."""
        if not symbols or len(set(symbols)) != len(symbols):
            raise AlpacaValidationError("symbols must be a nonempty list without duplicates")
        for symbol in symbols:
            validate_symbol(symbol)
        validate_feed(feed)
        body = self._get({"symbols": ",".join(symbols), "feed": feed}, endpoint=QUOTES_PATH)
        quotes = parsing.quotes(body)
        if set(quotes) - set(symbols):
            raise AlpacaError("response contained a symbol that was not requested")
        return quotes

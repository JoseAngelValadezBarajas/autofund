"""Hard GET-only Production boundary, deliberately independent of Stage client."""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

# Only shared cryptographic primitives, never the Stage execution surface.
from autofund.exchanges.bitso.auth import (
    BitsoCredentials,
    BitsoNonceProvider,
    SignedRequest,
    sign,
)

from . import parsing
from .errors import (
    AuthenticationUnavailable,
    MarketDataInvalid,
    ReadOnlyViolation,
    ReadUnavailable,
    StrictReadOnlyLimitation,
)
from .models import (
    AccountFeeSchedule,
    BookConstraints,
    MarketLimits,
    ObservedBalance,
    OhlcCandle,
    OrderBookSnapshot,
    PublicTrade,
    ShadowFee,
    Ticker,
    book_name,
)
from .parsing import depth

PRODUCTION_BASE_URL = "https://bitso.com"
PUBLIC_ENDPOINTS = frozenset({"available_books", "order_book", "trades", "ticker", "ohlc"})
PRIVATE_ENDPOINTS = frozenset({"balance", "fees"})
CONFIRMATION_MESSAGE = "Dedicated Bitso production read-only API key must be confirmed."


def validate_read(method: str, path: str, base_url: str = PRODUCTION_BASE_URL) -> None:
    if method != "GET":
        raise ReadOnlyViolation("Production transport accepts GET only")
    if base_url != PRODUCTION_BASE_URL:
        raise ReadOnlyViolation("Production observer host is fixed")
    parts = urlsplit(path)
    allowed = {
        "/api/v3/" + endpoint for endpoint in PUBLIC_ENDPOINTS | PRIVATE_ENDPOINTS
    }
    if parts.scheme or parts.netloc or parts.fragment or parts.path not in allowed:
        raise ReadOnlyViolation("endpoint outside strict read-only allowlist")
    query = parse_qs(parts.query, keep_blank_values=True)
    endpoint = parts.path.rsplit("/", 1)[-1]
    allowed_query = {
        "trades": {"book", "limit", "marker", "sort"},
        "order_book": {"book", "aggregate"},
        "ticker": {"book"},
        # OHLC is public historical candles: watched for real research backfill.
        "ohlc": {"book", "time_bucket", "start", "end", "limit"},
    }.get(endpoint, set())
    if set(query) - allowed_query or any(len(v) != 1 for v in query.values()):
        raise ReadOnlyViolation("query outside read-only allowlist")


@dataclass(frozen=True)
class ReadResponse:
    status: int
    body: bytes = field(repr=False)
    retry_after: int = 60


class ReadTransport(Protocol):
    def request(
        self, method: str, path: str, authorization: str = ""
    ) -> ReadResponse: ...


class ReadOnlyBitsoTransport:
    def __init__(self, *, base_url: str = PRODUCTION_BASE_URL) -> None:
        validate_read("GET", "/api/v3/available_books", base_url)

    def request(self, method: str, path: str, authorization: str = "") -> ReadResponse:
        validate_read(method, path)
        headers = {"Authorization": authorization} if authorization else {}
        try:
            with httpx.Client(
                timeout=httpx.Timeout(10, connect=5),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                result = client.get(PRODUCTION_BASE_URL + path, headers=headers)
                cooldown = result.headers.get("Retry-After", "60")
                return ReadResponse(
                    result.status_code,
                    result.content,
                    int(cooldown) if cooldown.isdigit() else 60,
                )
        except (httpx.HTTPError, OSError, ValueError):
            raise ReadUnavailable("Production GET unavailable") from None


@dataclass(frozen=True, repr=False)
class ProductionCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    readonly_confirmed: bool = False

    def __repr__(self) -> str:
        return "ProductionCredentials(api_key=***, api_secret=***, confirmation=operator_attestation)"

    def __post_init__(self) -> None:
        if self.readonly_confirmed is not True:
            raise AuthenticationUnavailable(CONFIRMATION_MESSAGE)
        if not self.api_key or not self.api_secret:
            raise AuthenticationUnavailable("Production credentials missing")
        try:
            BitsoCredentials(self.api_key, self.api_secret)
        except Exception:
            raise AuthenticationUnavailable(
                "invalid Production credential format"
            ) from None

    @classmethod
    def from_environment(cls) -> "ProductionCredentials":
        return cls(
            os.getenv("AUTOFUND_BITSO_PROD_API_KEY", ""),
            os.getenv("AUTOFUND_BITSO_PROD_API_SECRET", ""),
            os.getenv("AUTOFUND_BITSO_PROD_READONLY_CONFIRMED") == "true",
        )


class BitsoProductionReadOnlyClient:
    def __init__(
        self,
        credentials: ProductionCredentials | None = None,
        *,
        transport: ReadTransport | None = None,
        nonce: BitsoNonceProvider | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credentials = credentials
        self._transport = transport or ReadOnlyBitsoTransport()
        self._nonce = nonce or BitsoNonceProvider()
        self._sleep, self._monotonic = sleep, monotonic
        self._next = 0.0
        self._auth_failed = False
        self.outbound_methods: list[str] = []
        self.retries = 0

    def read(self, endpoint: str, query: dict[str, str] | None = None) -> object:
        path = "/api/v3/" + endpoint + (("?" + urlencode(query)) if query else "")
        validate_read("GET", path)
        private = endpoint in PRIVATE_ENDPOINTS
        if private and (self._credentials is None or self._auth_failed):
            raise AuthenticationUnavailable("confirmed Production credentials required")
        for attempt in range(3):
            delay = max(0.0, self._next - self._monotonic())
            if delay:
                self._sleep(delay)
            self._next = self._monotonic() + 1.05
            request = SignedRequest("GET", path, b"", "")
            if private:
                assert self._credentials is not None
                request = sign(
                    BitsoCredentials(
                        self._credentials.api_key, self._credentials.api_secret
                    ),
                    self._nonce.next(),
                    "GET",
                    path,
                    b"",
                )
            try:
                self.outbound_methods.append("GET")
                response = self._transport.request("GET", path, request.authorization)
                if response.status in (420, 429):
                    self._next = self._monotonic() + max(60, response.retry_after)
                    raise ReadUnavailable("Production rate limited; cooldown required")
                if response.status >= 500:
                    raise ReadUnavailable("Production service unavailable")
                if 300 <= response.status < 400:
                    raise ReadOnlyViolation("Production redirects prohibited")
                row = parsing.decode(response.body)
                if row.get("success") is not True:
                    error = parsing.obj(row.get("error"))
                    code = error.get("code")
                    if code == "0202" and endpoint == "fees":
                        raise StrictReadOnlyLimitation(
                            "UNAVAILABLE_IN_STRICT_READ_ONLY_MODE"
                        )
                    if response.status in (401, 403) or (
                        isinstance(code, str) and code.startswith("02")
                    ):
                        self._auth_failed = True
                        raise AuthenticationUnavailable(
                            "Production authentication or permission failed"
                        )
                    if isinstance(code, str) and code.startswith("08"):
                        self._next = self._monotonic() + 60
                    raise ReadUnavailable("Production read rejected")
                if response.status != 200:
                    raise ReadUnavailable("unexpected Production response status")
                return row.get("payload")
            except ReadUnavailable:
                if attempt == 2 or self._next - self._monotonic() >= 60:
                    raise
                self.retries += 1
                self._next = max(self._next, self._monotonic() + 2**attempt)
        raise ReadUnavailable("Production read attempts exhausted")

    def market_info(self, book: str) -> tuple[MarketLimits, ShadowFee]:
        return parsing.market(self.read("available_books"), book_name(book))

    def books(self) -> tuple[BookConstraints, ...]:
        """All exchange books, for read-only market discovery. GET-only."""
        return parsing.books(self.read("available_books"))

    def available_books(self) -> tuple[BookConstraints, ...]:
        """Alias kept explicit for discovery callers."""
        return self.books()

    def ticker(self, book: str) -> "Ticker":
        return parsing.ticker(self.read("ticker", {"book": book_name(book)}), book_name(book))

    def order_book(self, book: str) -> OrderBookSnapshot:
        return depth(self.read("order_book", {"book": book_name(book)}), book_name(book))

    def trades(self, book: str, *, limit: int | None = None,
               marker: str | None = None,
               sort: str | None = None) -> tuple[PublicTrade, ...]:
        """Recent public trade tape. GET-only, no credentials, no session.

        The trade tape is the only evidence that can support a maker-fill claim: a limit
        order fills because someone traded against it, so a fill model built on candles
        alone is inference, not observation. This accessor exists so that evidence can be
        collected rather than assumed.
        """
        query: dict[str, str] = {"book": book_name(book)}
        if limit is not None:
            query["limit"] = str(int(limit))
        if marker is not None:
            query["marker"] = str(marker)
        if sort is not None:
            query["sort"] = str(sort)
        return parsing.trades(self.read("trades", query), book_name(book))

    def ohlc(self, book: str, *, time_bucket: int = 60, start_ms: int | None = None,
             end_ms: int | None = None, limit: int | None = None) -> tuple[OhlcCandle, ...]:
        """Public historical candles. GET-only, no credentials, no session.

        This is the real historical evidence source: it lets the same deterministic
        strategy that would run live be evaluated over exchange-published candles,
        instead of requiring weeks of local capture before any evidence exists.
        """
        if time_bucket <= 0:
            raise MarketDataInvalid("time_bucket must be positive")
        query: dict[str, str] = {"book": book_name(book), "time_bucket": str(time_bucket)}
        if start_ms is not None:
            query["start"] = str(int(start_ms))
        if end_ms is not None:
            query["end"] = str(int(end_ms))
        if limit is not None:
            query["limit"] = str(int(limit))
        resolved = book_name(book)
        return parsing.ohlc(self.read("ohlc", query), resolved)

    def balances(self) -> tuple[ObservedBalance, ...]:
        return parsing.balances(self.read("balance"))

    def fee_schedule(self, book: str) -> ShadowFee:
        return parsing.fees(self.read("fees"), book_name(book))

    def account_fee_schedule(self, book: str) -> AccountFeeSchedule:
        """Account-confirmed maker AND taker rates for one book.

        GET-only. `fee_schedule` above keeps its taker-only contract, because changing it
        would silently alter the fee every existing economic verdict was computed from.
        This accessor is the one that can see the maker rate.
        """
        observed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return parsing.fee_schedule(self.read("fees"), book_name(book),
                                   observed_at=observed_at)

    def status(
        self, book: str = "btc_mxn", *, show_balances: bool = False
    ) -> dict[str, object]:
        balances = self.balances()
        self.market_info(book)
        result: dict[str, object] = {
            "environment": "PRODUCTION",
            "mode": "STRICT READ ONLY",
            "authentication": "PASS",
            "balances": "AVAILABLE",
            "readonly_confirmation": "OPERATOR_CONFIRMED_NOT_API_VERIFIED",
            "write_transport": "BLOCKED",
            "place_orders_permission_required": False,
        }
        try:
            result["fee_source"] = self.fee_schedule(book).source
            result["fees"] = "PASS"
        except StrictReadOnlyLimitation:
            result["fees"] = "UNAVAILABLE_IN_STRICT_READ_ONLY_MODE"
            result["fee_source"] = "unavailable_authenticated_readonly"
        if show_balances:
            result["real_exchange_balances"] = balances
        return result

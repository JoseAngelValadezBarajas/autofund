"""Fixed Stage host, constrained HTTP surface, and sanitized errors."""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode, urlsplit

import httpx

from . import parsing
from .auth import BitsoCredentials, BitsoNonceProvider, SignedRequest, sign
from .errors import (
    BitsoAuthenticationError,
    BitsoError,
    BitsoInsufficientFundsError,
    BitsoOrderNotFoundError,
    BitsoPermissionError,
    BitsoRateLimitError,
    BitsoTransportError,
    BitsoUnknownSubmissionOutcome,
    BitsoValidationError,
    ExchangeInvariantError,
)
from .models import (
    BitsoOrderRequest,
    Book,
    ExchangeBalance,
    ExchangeTradeFill,
    FeeSchedule,
    RemoteOrder,
    book_name,
    identifier,
    origin_id,
)

STAGE_BASE_URL = "https://stage.bitso.com"
PREFIX = "/api/v3/"


def validate_stage_url(url: str) -> None:
    if url != STAGE_BASE_URL:
        raise BitsoValidationError("only the fixed Bitso Stage host is permitted")


def validate_request(method: str, path: str, body: bytes) -> None:
    parts = urlsplit(path)
    if parts.scheme or parts.netloc or parts.fragment or "%" in parts.path:
        raise BitsoValidationError("invalid request path")
    endpoint = parts.path.removeprefix(PREFIX)
    if not parts.path.startswith(PREFIX):
        raise BitsoValidationError("invalid API prefix")
    if method == "GET" and re.fullmatch(
        r"(?:available_books|order_book|balance|fees|open_orders|orders|order_trades|user_trades)(?:/[a-zA-Z0-9_-]+)?",
        endpoint,
    ):
        if body:
            raise BitsoValidationError("GET cannot carry a body")
        return
    if method == "POST" and endpoint == "orders" and not parts.query:
        row = parsing.decode(body)
        if set(row) - {
            "book",
            "side",
            "type",
            "major",
            "minor",
            "price",
            "origin_id",
            "time_in_force",
            "slippage_tolerance",
        }:
            raise BitsoValidationError("unsupported order fields")
        return
    if (
        method == "DELETE"
        and re.fullmatch(r"orders/[a-zA-Z0-9_-]+", endpoint)
        and not parts.query
        and not body
    ):
        identifier(endpoint.split("/")[1])
        return
    raise BitsoValidationError("operation outside Stage execution allowlist")


@dataclass(frozen=True)
class TransportResponse:
    status: int
    body: bytes = field(repr=False)
    retry_after: float = 60


class BitsoTransport(Protocol):
    def send(self, request: SignedRequest) -> TransportResponse: ...


class HttpxBitsoTransport:
    def __init__(self, *, base_url: str = STAGE_BASE_URL) -> None:
        validate_stage_url(base_url)

    def send(self, request: SignedRequest) -> TransportResponse:
        validate_request(request.method, request.path, request.body)
        headers = {"Content-Type": "application/json"}
        if request.authorization:
            headers["Authorization"] = request.authorization
        try:
            with httpx.Client(
                timeout=httpx.Timeout(10, connect=5),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.request(
                    request.method,
                    STAGE_BASE_URL + request.path,
                    content=request.body,
                    headers=headers,
                )
                retry = response.headers.get("Retry-After", "60")
                return TransportResponse(
                    response.status_code,
                    response.content,
                    float(retry) if retry.isdigit() else 60,
                )
        except (httpx.HTTPError, OSError, ValueError):
            raise BitsoTransportError("Stage transport failed") from None


class ExchangeExecutionPort(Protocol):
    def get_balances(self) -> tuple[ExchangeBalance, ...]: ...
    def get_fees(self, book: str) -> FeeSchedule: ...
    def place_order(self, request: BitsoOrderRequest) -> str: ...
    def get_order(
        self, *, oid: str | None = None, origin: str | None = None
    ) -> tuple[RemoteOrder, ...]: ...
    def get_order_trades(
        self, *, oid: str | None = None, origin: str | None = None
    ) -> tuple[ExchangeTradeFill, ...]: ...
    def get_open_orders(self) -> tuple[RemoteOrder, ...]: ...
    def get_user_trades(self) -> tuple[ExchangeTradeFill, ...]: ...
    def cancel_order(self, oid: str, origin: str) -> bool: ...


class BitsoClient:
    def __init__(
        self,
        credentials: BitsoCredentials | None = None,
        *,
        transport: BitsoTransport | None = None,
        nonce: BitsoNonceProvider | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credentials = credentials
        self._transport = transport or HttpxBitsoTransport()
        self._nonce = nonce or BitsoNonceProvider()
        self._sleep, self._monotonic = sleep, monotonic
        self._next_request = 0.0
        self._auth_failed = False
        self._known: dict[str, str] = {}

    def register_known_order(self, oid: str, origin: str) -> None:
        identifier(oid)
        origin_id(origin)
        if oid in self._known and self._known[oid] != origin:
            raise ExchangeInvariantError("remote identity contradiction")
        self._known[oid] = origin

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        query: dict[str, str] | None = None,
        payload: dict[str, str] | None = None,
        public: bool = False,
    ) -> object:
        path = PREFIX + endpoint + (("?" + urlencode(query)) if query else "")
        body = (
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
            if payload
            else b""
        )
        validate_request(method, path, body)
        if not public and (self._credentials is None or self._auth_failed):
            raise BitsoAuthenticationError("Stage authentication unavailable")
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            delay = max(0.0, self._next_request - self._monotonic())
            if delay:
                self._sleep(delay)
            # One second between every request: at most 60/min, cancellations too.
            self._next_request = self._monotonic() + 1.05
            request = SignedRequest(method, path, body, "")
            if not public:
                assert self._credentials is not None
                request = sign(
                    self._credentials, self._nonce.next(), method, path, body
                )
            try:
                response = self._transport.send(request)
                if response.status >= 500:
                    raise BitsoTransportError("Stage server unavailable")
                if response.status in (420, 429):
                    self._next_request = self._monotonic() + max(
                        60, response.retry_after
                    )
                    raise BitsoRateLimitError("Stage rate limit")
                if not 200 <= response.status < 500 or 300 <= response.status < 400:
                    raise BitsoTransportError("unexpected Stage HTTP status")
                row = parsing.decode(response.body)
                if row.get("success") is not True:
                    error = parsing.obj(row.get("error"))
                    code = error.get("code")
                    code = (
                        code
                        if isinstance(code, str) and re.fullmatch(r"\d{4}", code)
                        else "unknown"
                    )
                    if code in ("0202", "0213"):
                        raise BitsoPermissionError(
                            "Stage permission denied; code=" + code
                        )
                    if code.startswith("02") or code == "0326":
                        raise BitsoAuthenticationError(
                            "Stage auth failed; code=" + code
                        )
                    if code.startswith("08"):
                        self._next_request = self._monotonic() + 60
                        raise BitsoRateLimitError("Stage rate limit; code=" + code)
                    if code == "0379":
                        raise BitsoInsufficientFundsError("Stage insufficient funds")
                    if code in ("0312", "0357"):
                        raise BitsoOrderNotFoundError("Stage order not found")
                    if code in ("0356", "0377"):
                        raise BitsoUnknownSubmissionOutcome(
                            "order identity or settlement needs reconciliation"
                        )
                    raise BitsoValidationError("Stage rejected request; code=" + code)
                if response.status >= 400:
                    raise ExchangeInvariantError(
                        "success envelope contradicts HTTP status"
                    )
                return row.get("payload")
            except BitsoAuthenticationError:
                self._auth_failed = True
                raise
            except (BitsoTransportError, ExchangeInvariantError):
                if method != "GET":
                    raise BitsoUnknownSubmissionOutcome(
                        "write outcome unknown; reconcile original identity"
                    ) from None
                if attempt + 1 == attempts:
                    raise BitsoTransportError(
                        "bounded Stage read attempts exhausted"
                    ) from None
                self._next_request = max(
                    self._next_request, self._monotonic() + 2**attempt
                )
            except BitsoRateLimitError:
                # No aggressive wait loop; caller resumes after explicit cooldown.
                raise
        raise BitsoTransportError("read attempts exhausted")

    def get_books(self) -> tuple[Book, ...]:
        return parsing.books(self._request("GET", "available_books", public=True))

    def get_book(self, book: str) -> Book:
        book_name(book)
        for item in self.get_books():
            if item.book == book:
                return item
        raise BitsoValidationError("book not available on Stage")

    def get_depth(self, book: str) -> dict[str, object]:
        return parsing.obj(
            self._request(
                "GET", "order_book", query={"book": book_name(book)}, public=True
            )
        )

    def get_balances(self) -> tuple[ExchangeBalance, ...]:
        return parsing.balances(self._request("GET", "balance"))

    def get_fees(self, book: str) -> FeeSchedule:
        matches = [
            f
            for f in parsing.fees(self._request("GET", "fees"))
            if f.book == book_name(book)
        ]
        if len(matches) != 1:
            raise ExchangeInvariantError(
                "account fee schedule unavailable or duplicated"
            )
        return matches[0]

    def place_order(self, request: BitsoOrderRequest) -> str:
        payload = self._request("POST", "orders", payload=request.payload())
        try:
            oid = identifier(parsing.text(parsing.obj(payload).get("oid")))
            self.register_known_order(oid, request.origin_id)
            return oid
        except BitsoError:
            raise BitsoUnknownSubmissionOutcome(
                "unusable acknowledgement; reconcile identity"
            ) from None

    @staticmethod
    def _lookup(
        endpoint: str, oid: str | None, origin: str | None
    ) -> tuple[str, dict[str, str] | None]:
        if (oid is None) == (origin is None):
            raise BitsoValidationError("exactly one order identity required")
        if oid is not None:
            return endpoint + "/" + identifier(oid), None
        assert origin is not None
        return endpoint, {
            "origin_ids" if endpoint == "orders" else "origin_id": origin_id(origin)
        }

    def get_order(
        self, *, oid: str | None = None, origin: str | None = None
    ) -> tuple[RemoteOrder, ...]:
        endpoint, query = self._lookup("orders", oid, origin)
        try:
            return parsing.orders(self._request("GET", endpoint, query=query))
        except BitsoOrderNotFoundError:
            return ()

    def get_order_trades(
        self, *, oid: str | None = None, origin: str | None = None
    ) -> tuple[ExchangeTradeFill, ...]:
        endpoint, query = self._lookup("order_trades", oid, origin)
        try:
            return parsing.fills(self._request("GET", endpoint, query=query))
        except BitsoOrderNotFoundError:
            return ()

    def get_open_orders(self) -> tuple[RemoteOrder, ...]:
        orders = parsing.orders(
            self._request("GET", "open_orders", query={"limit": "500"})
        )
        if len(orders) >= 500:
            raise ExchangeInvariantError("open-order response may be truncated")
        return orders

    def get_user_trades(self) -> tuple[ExchangeTradeFill, ...]:
        result: list[ExchangeTradeFill] = []
        query = {"limit": "100", "sort": "desc"}
        markers: set[str] = set()
        for _ in range(10):
            page = parsing.fills(self._request("GET", "user_trades", query=query))
            result.extend(page)
            if len(page) < 100:
                return tuple(result)
            marker = page[-1].trade_id
            if marker in markers:
                break
            markers.add(marker)
            query["marker"] = marker
        raise ExchangeInvariantError("trade history exceeds bounded complete scan")

    def cancel_order(self, oid: str, origin: str) -> bool:
        identifier(oid)
        origin_id(origin)
        if self._known.get(oid) != origin:
            raise BitsoValidationError("cannot cancel an unknown or foreign order")
        payload = self._request("DELETE", "orders/" + oid)
        values = parsing.array(payload)
        if any(v != oid for v in values):
            raise ExchangeInvariantError("cancellation identity mismatch")
        return oid in values

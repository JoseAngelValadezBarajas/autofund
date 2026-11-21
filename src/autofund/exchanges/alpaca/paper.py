"""Alpaca PAPER trading client: fixed paper host, narrow allowlist, idempotent orders.

The live trading host is not representable here. Only paper keys (``PK...``) can construct the
credentials this client takes, and the base URL is a constant, so a mistake in configuration cannot
turn a paper experiment into real orders.

Order submission is the one write. It is market, day, notional-sized, and carries a caller-chosen
``client_order_id``: a retry or a reconciliation after an unknown outcome looks the order up by that
id rather than submitting again.
"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from .client import AlpacaCredentials, TransportResponse
from .errors import (
    AlpacaAuthenticationError,
    AlpacaError,
    AlpacaOrderRejected,
    AlpacaParseError,
    AlpacaRateLimitError,
    AlpacaTransportError,
    AlpacaUnknownSubmissionOutcome,
    AlpacaValidationError,
)
from .models import validate_symbol

PAPER_BASE_URL = "https://paper-api.alpaca.markets"
MAX_ORDER_NOTIONAL = Decimal("100000")

_UUID = re.compile(r"[0-9a-fA-F-]{36}\Z")
_CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,48}\Z")
_ORDER_FIELDS = frozenset({"symbol", "notional", "side", "type", "time_in_force", "client_order_id"})
_ORDER_STATUSES = frozenset({"open", "closed", "all"})
TERMINAL = frozenset({"filled", "canceled", "expired", "rejected", "done_for_day", "replaced"})


@dataclass(frozen=True)
class PaperRequest:
    method: str
    path: str
    credentials: AlpacaCredentials = field(repr=False)
    body: bytes = field(default=b"", repr=False)


class PaperTransport(Protocol):
    def send(self, request: PaperRequest) -> TransportResponse: ...


def validate_paper_request(method: str, path: str, body: bytes = b"") -> None:
    parts = urlsplit(path)
    if parts.scheme or parts.netloc or parts.fragment or "%" in parts.path:
        raise AlpacaValidationError("invalid request path")
    route = parts.path
    query = dict(parse_qsl(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query)))
    if method == "GET":
        if body:
            raise AlpacaValidationError("GET cannot carry a body")
        if route in ("/v2/account", "/v2/clock", "/v2/positions") and not query:
            return
        if route == "/v2/orders":
            if set(query) - {"status", "limit", "direction"}:
                raise AlpacaValidationError("unsupported order query")
            if query.get("status", "open") not in _ORDER_STATUSES:
                raise AlpacaValidationError("invalid order status")
            if "limit" in query and not (query["limit"].isdigit() and 1 <= int(query["limit"]) <= 500):
                raise AlpacaValidationError("invalid limit")
            return
        if route == "/v2/orders:by_client_order_id":
            if set(query) != {"client_order_id"} or not _CLIENT_ID.fullmatch(query["client_order_id"]):
                raise AlpacaValidationError("invalid client order id")
            return
        if re.fullmatch(r"/v2/orders/[0-9a-fA-F-]{36}", route) and not query:
            return
    elif method == "POST" and route == "/v2/orders" and not query:
        row = json.loads(body or b"{}")
        if set(row) != _ORDER_FIELDS:
            raise AlpacaValidationError("order fields outside the allowlist")
        if row["type"] != "market" or row["time_in_force"] != "day" or row["side"] not in ("buy", "sell"):
            raise AlpacaValidationError("only market/day buy/sell orders are permitted")
        validate_symbol(row["symbol"])
        if not _CLIENT_ID.fullmatch(row["client_order_id"]):
            raise AlpacaValidationError("invalid client order id")
        try:
            notional = Decimal(row["notional"])
        except (InvalidOperation, TypeError):
            raise AlpacaValidationError("notional must be a decimal string") from None
        if not Decimal("1") <= notional <= MAX_ORDER_NOTIONAL:
            raise AlpacaValidationError("notional outside the permitted range")
        return
    elif method == "DELETE" and not query and not body:
        if route == "/v2/orders" or re.fullmatch(r"/v2/orders/[0-9a-fA-F-]{36}", route):
            return
        match = re.fullmatch(r"/v2/positions/([A-Z.]{1,7})", route)
        if match:
            validate_symbol(match.group(1))
            return
    raise AlpacaValidationError("operation outside the paper-trading allowlist")


class HttpxPaperTransport:
    def send(self, request: PaperRequest) -> TransportResponse:
        validate_paper_request(request.method, request.path, request.body)
        headers = {
            "APCA-API-KEY-ID": request.credentials.key_id,
            "APCA-API-SECRET-KEY": request.credentials.secret,
            "Accept": "application/json",
        }
        if request.body:
            headers["Content-Type"] = "application/json"
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False
            ) as client:
                response = client.request(
                    request.method, PAPER_BASE_URL + request.path, headers=headers,
                    content=request.body or None,
                )
                retry = response.headers.get("Retry-After", "60")
                return TransportResponse(
                    response.status_code, response.content, float(retry) if retry.isdigit() else 60
                )
        except (httpx.HTTPError, OSError, ValueError):
            raise AlpacaTransportError("paper transport failed") from None


# ---- models ----

def _opt(row: dict, key: str) -> Decimal | None:
    return None if row.get(key) is None else _dec(row, key)


def _dec(row: dict, key: str) -> Decimal:
    value = row.get(key)
    if value is None:
        raise AlpacaParseError(f"missing {key}")
    try:
        out = Decimal(str(value))
    except InvalidOperation:
        raise AlpacaParseError(f"malformed {key}") from None
    if not out.is_finite():
        raise AlpacaParseError(f"non-finite {key}")
    return out


@dataclass(frozen=True, slots=True)
class Account:
    status: str
    equity: Decimal
    last_equity: Decimal
    cash: Decimal
    buying_power: Decimal
    trading_blocked: bool


@dataclass(frozen=True, slots=True)
class Clock:
    is_open: bool
    timestamp: str
    next_open: str
    next_close: str


@dataclass(frozen=True, slots=True)
class Position:
    symbol: str
    qty: Decimal
    market_value: Decimal
    avg_entry_price: Decimal
    unrealized_pl: Decimal
    current_price: Decimal


@dataclass(frozen=True, slots=True)
class Order:
    id: str
    client_order_id: str
    symbol: str
    side: str
    status: str
    notional: Decimal | None
    filled_qty: Decimal
    filled_avg_price: Decimal | None
    submitted_at: str

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


def parse_account(row: dict) -> Account:
    return Account(str(row.get("status", "")), _dec(row, "equity"), _dec(row, "last_equity"),
                   _dec(row, "cash"), _dec(row, "buying_power"), bool(row.get("trading_blocked")))


def parse_clock(row: dict) -> Clock:
    return Clock(bool(row.get("is_open")), str(row.get("timestamp", "")),
                 str(row.get("next_open", "")), str(row.get("next_close", "")))


def parse_position(row: dict) -> Position:
    return Position(validate_symbol(str(row.get("symbol", ""))), _dec(row, "qty"),
                    _dec(row, "market_value"), _dec(row, "avg_entry_price"),
                    _dec(row, "unrealized_pl"), _dec(row, "current_price"))


def parse_order(row: dict) -> Order:
    return Order(str(row.get("id", "")), str(row.get("client_order_id", "")),
                 str(row.get("symbol", "")), str(row.get("side", "")), str(row.get("status", "")),
                 _opt(row, "notional"), _opt(row, "filled_qty") or Decimal(0),
                 _opt(row, "filled_avg_price"), str(row.get("submitted_at") or ""))


def _json(body: bytes) -> object:
    try:
        return json.loads(body)
    except ValueError:
        raise AlpacaParseError("response was not valid JSON") from None


class AlpacaPaperClient:
    def __init__(
        self,
        credentials: AlpacaCredentials,
        *,
        transport: PaperTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._credentials = credentials
        self._transport = transport or HttpxPaperTransport()
        self._sleep, self._monotonic = sleep, monotonic
        self._next = 0.0
        self._auth_failed = False

    def _call(self, method: str, path: str, payload: dict | None = None) -> object:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode() if payload else b""
        validate_paper_request(method, path, body)
        if self._auth_failed:
            raise AlpacaAuthenticationError("paper authentication unavailable")
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            delay = max(0.0, self._next - self._monotonic())
            if delay:
                self._sleep(delay)
            self._next = self._monotonic() + 0.35
            try:
                response = self._transport.send(PaperRequest(method, path, self._credentials, body))
                status = response.status
                if status in (401, 403):
                    self._auth_failed = True
                    raise AlpacaAuthenticationError("paper authentication failed")
                if status == 429:
                    self._next = self._monotonic() + max(60, response.retry_after)
                    raise AlpacaRateLimitError("paper rate limit")
                if status >= 500:
                    raise AlpacaTransportError("paper server unavailable")
                if status == 404:
                    raise AlpacaValidationError("paper resource not found")
                if status in (400, 422):
                    raise AlpacaOrderRejected(f"paper request rejected; http={status}")
                if not 200 <= status < 300:
                    raise AlpacaValidationError(f"paper request failed; http={status}")
                return _json(response.body) if response.body else None
            except AlpacaTransportError:
                if method != "GET":
                    raise AlpacaUnknownSubmissionOutcome(
                        "write outcome unknown; reconcile by client order id"
                    ) from None
                if attempt + 1 == attempts:
                    raise AlpacaTransportError("bounded paper read attempts exhausted") from None
                self._next = max(self._next, self._monotonic() + 2**attempt)
        raise AlpacaTransportError("paper attempts exhausted")

    @staticmethod
    def _obj(value: object) -> dict:
        if not isinstance(value, dict):
            raise AlpacaParseError("expected an object")
        return value

    @staticmethod
    def _list(value: object) -> list:
        if not isinstance(value, list):
            raise AlpacaParseError("expected a list")
        return value

    def get_account(self) -> Account:
        return parse_account(self._obj(self._call("GET", "/v2/account")))

    def get_clock(self) -> Clock:
        return parse_clock(self._obj(self._call("GET", "/v2/clock")))

    def get_positions(self) -> tuple[Position, ...]:
        return tuple(parse_position(self._obj(r)) for r in self._list(self._call("GET", "/v2/positions")))

    def list_orders(self, status: str = "open", limit: int = 100) -> tuple[Order, ...]:
        path = "/v2/orders?" + urlencode({"status": status, "limit": str(limit), "direction": "desc"})
        return tuple(parse_order(self._obj(r)) for r in self._list(self._call("GET", path)))

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        path = "/v2/orders:by_client_order_id?" + urlencode({"client_order_id": client_order_id})
        try:
            return parse_order(self._obj(self._call("GET", path)))
        except AlpacaValidationError:
            return None

    def get_order(self, order_id: str) -> Order:
        if not _UUID.fullmatch(order_id):
            raise AlpacaValidationError("invalid order id")
        return parse_order(self._obj(self._call("GET", f"/v2/orders/{order_id}")))

    def submit_market_notional(
        self, *, symbol: str, side: str, notional: Decimal, client_order_id: str
    ) -> Order:
        """Submit once. On an unknown outcome the caller must reconcile, never resubmit blindly."""
        payload = {"symbol": symbol, "notional": format(notional.quantize(Decimal("0.01")), "f"),
                   "side": side, "type": "market", "time_in_force": "day",
                   "client_order_id": client_order_id}
        return parse_order(self._obj(self._call("POST", "/v2/orders", payload)))

    def cancel_open_orders(self) -> None:
        self._call("DELETE", "/v2/orders")

    def cancel_order(self, order_id: str) -> None:
        if not _UUID.fullmatch(order_id):
            raise AlpacaValidationError("invalid order id")
        self._call("DELETE", f"/v2/orders/{order_id}")

    def close_position(self, symbol: str) -> None:
        self._call("DELETE", f"/v2/positions/{validate_symbol(symbol)}")


__all__ = [
    "PAPER_BASE_URL", "Account", "AlpacaError", "AlpacaPaperClient", "Clock", "Order", "Position",
    "validate_paper_request",
]

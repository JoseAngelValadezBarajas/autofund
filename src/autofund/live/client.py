"""Pinned Production transport. Only gated, single-use MARKET BUY may mutate."""
import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from autofund.exchanges.bitso import parsing
from autofund.exchanges.bitso.auth import BitsoCredentials, BitsoNonceProvider, sign
from autofund.exchanges.bitso.models import (
    Book,
    ExchangeBalance,
    ExchangeTradeFill,
    FeeSchedule,
    RemoteOrder,
    identifier,
)
from autofund.observer.models import OrderBookSnapshot
from autofund.observer.parsing import depth

from .models import LiveError

HOST = "https://bitso.com"
ORIGIN = re.compile(r"af-live-[a-f0-9]{32}")
PUBLIC = {"/api/v3/available_books", "/api/v3/order_book"}
GET_QUERIES = {"/api/v3/available_books": set(), "/api/v3/order_book": {"book"},
               "/api/v3/balance": set(), "/api/v3/fees": set(),
               "/api/v3/orders": {"origin_ids"}, "/api/v3/order_trades": {"origin_id"}}


def validate_request(method: str, path: str) -> None:
    parts = urlsplit(path)
    if parts.scheme or parts.netloc or parts.fragment or "%" in parts.path:
        raise LiveError("LIVE_HTTP_POLICY_VIOLATION")
    try:
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise LiveError("LIVE_HTTP_POLICY_VIOLATION") from None
    if method == "POST" and path == "/api/v3/orders":
        return
    if method != "GET" or parts.path not in GET_QUERIES or set(query) != GET_QUERIES[parts.path]:
        raise LiveError("LIVE_HTTP_POLICY_VIOLATION")
    if any(len(values) != 1 for values in query.values()):
        raise LiveError("LIVE_HTTP_POLICY_VIOLATION")
    if query.get("book", ["btc_mxn"])[0] != "btc_mxn":
        raise LiveError("ONLY_BTC_MXN_ALLOWED")
    if parts.path in {"/api/v3/orders", "/api/v3/order_trades"}:
        value = next(iter(query.values()))[0]
        if not ORIGIN.fullmatch(value):
            raise LiveError("ONLY_KNOWN_LIVE_ORIGIN_ALLOWED")


@dataclass(frozen=True, repr=False)
class LiveCredentials:
    api_key: str = field(repr=False)
    api_secret: str = field(repr=False)
    permissions_confirmed: bool = False

    def __post_init__(self) -> None:
        try:
            BitsoCredentials(self.api_key, self.api_secret)
        except Exception:
            raise LiveError("LIVE_CREDENTIALS_MISSING_OR_INVALID") from None

    def __repr__(self) -> str:
        return "LiveCredentials(key=***, secret=***)"

    @classmethod
    def from_environment(cls) -> "LiveCredentials":
        return cls(os.getenv("AUTOFUND_BITSO_LIVE_API_KEY", ""), os.getenv("AUTOFUND_BITSO_LIVE_API_SECRET", ""),
                   os.getenv("AUTOFUND_BITSO_LIVE_PERMISSIONS_CONFIRMED") == "true")


class _SubmissionPermit:
    """Created by execution only after durable SUBMITTING + operator gate."""
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.used = False

    def consume(self, body: bytes) -> None:
        if self.used or body != self.body:
            raise LiveError("LIVE_SUBMISSION_PERMIT_INVALID")
        self.used = True  # Consumed even if the network call subsequently fails.


def validate_buy(body: bytes, single_order_cap: Decimal = Decimal("11")) -> None:
    try:
        row = parsing.decode(body)
        if set(row) != {"book", "side", "type", "minor", "origin_id", "slippage_tolerance"}:
            raise ValueError
        if (row["book"], row["side"], row["type"]) != ("btc_mxn", "buy", "market"):
            raise ValueError
        if not ORIGIN.fullmatch(parsing.text(row["origin_id"])):
            raise ValueError
        if not Decimal("0") < parsing.number(row["minor"]) <= single_order_cap:
            raise ValueError
        if not Decimal("0") <= parsing.number(row["slippage_tolerance"]) <= Decimal("100"):
            raise ValueError
    except Exception:
        raise LiveError("ONLY_CAPPED_SPOT_MARKET_BUY_ALLOWED") from None


class LiveTransport(Protocol):
    def request(self, method: str, path: str, body: bytes, authorization: str,
                permit: _SubmissionPermit | None = None) -> object: ...


class BitsoProductionLiveTransport:
    def request(self, method: str, path: str, body: bytes, authorization: str,
                permit: _SubmissionPermit | None = None) -> object:
        validate_request(method, path)
        if method == "POST":
            if permit is None:
                raise LiveError("OPERATOR_SUBMISSION_PERMIT_REQUIRED")
            validate_buy(body)
            permit.consume(body)
        elif body:
            raise LiveError("GET_BODY_PROHIBITED")
        try:
            with httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False, trust_env=False) as session:
                response = session.request(method, HOST + path, content=body,
                                           headers={"Authorization": authorization, "Content-Type": "application/json"})
            row = parsing.decode(response.content)
            if response.status_code != 200 or row.get("success") is not True:
                raise LiveError("PRODUCTION_RESPONSE_REJECTED_OR_UNCERTAIN")
            return row["payload"]
        except Exception:
            # No raw remote payload, key, Authorization or HTTP exception survives.
            raise LiveError("PRODUCTION_RESPONSE_REJECTED_OR_UNCERTAIN") from None


class BitsoProductionLiveClient:
    def __init__(self, credentials: LiveCredentials, *, transport: LiveTransport | None = None,
                 single_order_cap: Decimal = Decimal("11")) -> None:
        self.credentials = credentials
        self._transport = transport or BitsoProductionLiveTransport()
        self._nonce = BitsoNonceProvider()
        self.known_origins: set[str] = set()
        self.outbound_methods: list[str] = []
        self._submitted_origins: set[str] = set()
        self.single_order_cap = single_order_cap

    def _request(self, method: str, path: str, body: bytes = b"", permit: _SubmissionPermit | None = None) -> object:
        validate_request(method, path)
        if method == "POST" and permit is None:
            raise LiveError("OPERATOR_SUBMISSION_PERMIT_REQUIRED")
        authorization = ""
        if path.split("?", 1)[0] not in PUBLIC:
            authorization = sign(BitsoCredentials(self.credentials.api_key, self.credentials.api_secret),
                                 self._nonce.next(), method, path, body).authorization
        self.outbound_methods.append(method)
        try:
            return self._transport.request(method, path, body, authorization, permit)
        except Exception:
            raise LiveError("LIVE_REQUEST_UNAVAILABLE_OR_UNCERTAIN") from None

    def balances(self) -> tuple[ExchangeBalance, ...]:
        return parsing.balances(self._request("GET", "/api/v3/balance"))

    def fees(self) -> FeeSchedule:
        matches = [x for x in parsing.fees(self._request("GET", "/api/v3/fees")) if x.book == "btc_mxn"]
        if len(matches) != 1:
            raise LiveError("CONFIRMED_ACCOUNT_FEES_UNAVAILABLE")
        return matches[0]

    def available_books(self) -> Book:
        matches = [x for x in parsing.books(self._request("GET", "/api/v3/available_books")) if x.book == "btc_mxn"]
        if len(matches) != 1:
            raise LiveError("PRODUCTION_BTC_MXN_LIMITS_UNAVAILABLE")
        return matches[0]

    def order_book(self) -> OrderBookSnapshot:
        return depth(self._request("GET", "/api/v3/order_book?book=btc_mxn"), "btc_mxn")

    def register_origin(self, origin: str) -> None:
        if not ORIGIN.fullmatch(origin):
            raise LiveError("INVALID_LIVE_ORIGIN")
        self.known_origins.add(origin)

    def lookup_order(self, origin: str) -> tuple[RemoteOrder, ...]:
        self._known(origin)
        return parsing.orders(self._request("GET", "/api/v3/orders?" + urlencode({"origin_ids": origin})))

    def order_trades(self, origin: str) -> tuple[ExchangeTradeFill, ...]:
        self._known(origin)
        return parsing.fills(self._request("GET", "/api/v3/order_trades?" + urlencode({"origin_id": origin})))

    def _known(self, origin: str) -> None:
        if origin not in self.known_origins:
            raise LiveError("UNKNOWN_AUTOFUND_ORDER")

    def place_market_order(self, payload: dict[str, str], permit: _SubmissionPermit) -> str:
        self._known(payload["origin_id"])
        body = json.dumps(payload, separators=(",", ":")).encode()
        validate_buy(body, self.single_order_cap)
        if permit.used or permit.body != body or payload["origin_id"] in self._submitted_origins:
            raise LiveError("LIVE_SUBMISSION_ALREADY_ATTEMPTED")
        self._submitted_origins.add(payload["origin_id"])
        result = parsing.obj(self._request("POST", "/api/v3/orders", body, permit))
        return identifier(parsing.text(result.get("oid")))

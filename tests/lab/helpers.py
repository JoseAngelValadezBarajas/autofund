import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

from autofund.exchanges.alpaca.client import AlpacaCredentials, TransportResponse
from autofund.exchanges.alpaca.models import AlpacaBarSeries
from autofund.exchanges.alpaca.paper import PaperRequest
from autofund.replay.data import Candle

CREDS = AlpacaCredentials("PKLABKEY0001", "LABSECRET0001")


def make_series(symbol: str, closes: list[str]) -> AlpacaBarSeries:
    start = datetime(2025, 1, 1, 4, tzinfo=UTC)
    candles = []
    for i, c in enumerate(closes):
        price = D(c)
        candles.append(Candle(start + timedelta(days=i), price, price, price, price, D(1000)))
    return AlpacaBarSeries(symbol, "all", "sip", tuple(candles))


def rising(n=300, start=100):
    return [str(D(start) + D(i) / 10) for i in range(n)]


def falling(n=300, start=200):
    return [str(D(start) - D(i) / 10) for i in range(n)]


class FakeData:
    """Stands in for AlpacaDataClient: rising SPY/QQQ, falling TLT/GLD, flat-ish quotes."""

    def __init__(self, shape=None):
        self.shape = shape or {"SPY": rising(), "QQQ": rising(), "TLT": falling(), "GLD": falling()}
        self.quote_calls = 0

    def get_daily_bars(self, symbols, start, end, *, adjustment, feed):
        return {s: make_series(s, self.shape[s]) for s in symbols}

    def get_latest_quotes(self, symbols, *, feed):
        from autofund.exchanges.alpaca.models import Quote
        self.quote_calls += 1
        at = datetime(2026, 11, 23, 16, 0, tzinfo=UTC) + timedelta(seconds=self.quote_calls)
        return {s: Quote(s, at, D("100.00"), D("100.05")) for s in symbols}


class FakeBroker:
    """A tiny paper broker behind the real AlpacaPaperClient (allowlist and parsing included)."""

    def __init__(self, equity="100000", is_open=True):
        self.equity, self.is_open = D(equity), is_open
        self.positions: dict[str, D] = {}
        self.orders: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.fail_next_post: Exception | None = None
        self.reject = False
        self.lose_order_after_unknown = True

    def send(self, request: PaperRequest) -> TransportResponse:
        self.requests.append((request.method, request.path))
        m, p = request.method, request.path.split("?")[0]
        if p == "/v2/account":
            return self._ok({"status": "ACTIVE", "equity": str(self.equity), "last_equity": str(self.equity),
                             "cash": str(self.equity), "buying_power": str(self.equity * 2),
                             "trading_blocked": False})
        if p == "/v2/clock":
            return self._ok({"is_open": self.is_open, "timestamp": "t", "next_open": "o", "next_close": "c"})
        if p == "/v2/positions" and m == "GET":
            return self._ok([{"symbol": s, "qty": "1", "market_value": str(v), "avg_entry_price": "1",
                              "unrealized_pl": "0", "current_price": "1"} for s, v in self.positions.items()])
        if p.startswith("/v2/positions/") and m == "DELETE":
            self.positions.pop(p.rsplit("/", 1)[1], None)
            return self._ok({})
        if p == "/v2/orders" and m == "GET":
            open_only = "status=open" in request.path
            rows = [o for o in self.orders.values() if not open_only or o["status"] != "filled"]
            return self._ok(rows)
        if p == "/v2/orders" and m == "DELETE":
            for o in self.orders.values():
                if o["status"] != "filled":
                    o["status"] = "canceled"
            return TransportResponse(207, b"[]")
        if p == "/v2/orders:by_client_order_id":
            cid = request.path.split("=", 1)[1]
            o = self.orders.get(cid)
            return self._ok(o) if o else TransportResponse(404, b"{}")
        if p == "/v2/orders" and m == "POST":
            if self.fail_next_post:
                exc, self.fail_next_post = self.fail_next_post, None
                if not self.lose_order_after_unknown:
                    self._create(json.loads(request.body))
                raise exc
            if self.reject:
                return TransportResponse(422, b'{"message":"rejected"}')
            return self._ok(self._create(json.loads(request.body)))
        return TransportResponse(404, b"{}")

    def _create(self, body):
        o = {"id": f"00000000-0000-4000-8000-{len(self.orders):012d}", "client_order_id": body["client_order_id"],
             "symbol": body["symbol"], "side": body["side"], "status": "filled" if self.is_open else "accepted",
             "notional": body["notional"], "filled_qty": "1" if self.is_open else "0",
             "filled_avg_price": "100" if self.is_open else None, "submitted_at": "2026-11-23T15:00:00Z"}
        self.orders[body["client_order_id"]] = o
        if self.is_open:
            side = 1 if body["side"] == "buy" else -1
            self.positions[body["symbol"]] = self.positions.get(body["symbol"], D(0)) + side * D(body["notional"])
        return o

    @staticmethod
    def _ok(payload):
        return TransportResponse(200, json.dumps(payload).encode())

import json
import socket
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

from autofund.exchanges.bitso import parsing
from autofund.exchanges.bitso.errors import BitsoUnknownSubmissionOutcome
from autofund.exchanges.bitso.execution import StageExecutionEngine
from autofund.exchanges.bitso.journal import ExecutionJournal
from autofund.exchanges.bitso.models import (
    ExchangeBalance,
    ExecutionInstruction,
    ExecutionPolicy,
    OrderState,
    OrderType,
    RemoteOrder,
)
from autofund.models import Side
from autofund.replay.strategy import OrderIntent


@pytest.fixture(autouse=True)
def offline_guard(request, monkeypatch):
    if request.node.get_closest_marker("stage"):
        return

    def forbidden(*args, **kwargs):
        raise AssertionError("F3 offline test attempted network")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.delenv("AUTOFUND_BITSO_STAGE_API_KEY", raising=False)
    monkeypatch.delenv("AUTOFUND_BITSO_STAGE_API_SECRET", raising=False)


@pytest.fixture
def fixture_data():
    return json.loads(
        (Path(__file__).parents[1] / "fixtures/bitso/scenarios.json").read_text()
    )


@pytest.fixture
def book(fixture_data):
    return parsing.books(fixture_data["books"])[0]


@pytest.fixture
def fees(fixture_data):
    return parsing.fees(fixture_data["fees"])[0]


@pytest.fixture
def instruction():
    return ExecutionInstruction(
        OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("10.2")),
        OrderType.LIMIT,
        D("1000"),
        True,
    )


class FakeExchange:
    def __init__(self):
        self.orders = {}
        self.fills = []
        self.posts = []
        self.cancels = []
        self.lookups = 0
        self.mode = "normal"
        self.delta = D("0")
        self.hidden = False

    def get_balances(self):
        balances = {"mxn": D("100000"), "btc": D("100")}
        for f in {f.trade_id: f for f in self.fills}.values():
            sign = D("1") if f.side is Side.BUY else D("-1")
            balances["mxn"] -= sign * f.minor_value
            balances["btc"] += sign * f.major_quantity
            if f.confirmed_fee is not None and f.fee_currency in balances:
                balances[f.fee_currency] -= f.confirmed_fee
        balances["mxn"] += self.delta
        return tuple(ExchangeBalance(c, v, D("0"), v) for c, v in balances.items())

    def get_open_orders(self):
        return tuple(
            o
            for o in self.orders.values()
            if o.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED)
        )

    def get_user_trades(self):
        return tuple(self.fills)

    def get_order(self, *, oid=None, origin=None):
        self.lookups += 1
        if self.hidden:
            return ()
        return tuple(
            o for o in self.orders.values() if o.origin_id == origin or o.oid == oid
        )

    def get_order_trades(self, *, oid=None, origin=None):
        return tuple(
            f for f in self.fills if f.origin_id == origin or f.exchange_order_id == oid
        )

    def place_order(self, request):
        self.posts.append(request)
        oid = "remote-" + str(len(self.posts))
        if self.mode != "timeout_absent":
            self.orders[request.origin_id] = RemoteOrder(
                oid,
                request.origin_id,
                request.book,
                request.side,
                OrderState.OPEN,
                request.major or D("0"),
                request.major or D("0"),
                request.price or D("0"),
            )
        if self.mode.startswith("timeout"):
            raise BitsoUnknownSubmissionOutcome("synthetic timeout")
        if self.mode == "crash":
            raise RuntimeError("synthetic process crash")
        return oid

    def cancel_order(self, oid, origin):
        self.cancels.append((oid, origin))
        self.orders[origin] = replace(self.orders[origin], state=OrderState.CANCELLED)
        return True

    def add_fill(self, fill):
        self.fills.append(fill)
        order = self.orders[fill.origin_id]
        quantity = sum(
            (
                f.major_quantity
                for f in {f.trade_id: f for f in self.fills}.values()
                if f.origin_id == fill.origin_id
            ),
            D("0"),
        )
        self.orders[fill.origin_id] = replace(
            order,
            unfilled_amount=order.original_amount - quantity,
            state=OrderState.COMPLETED
            if quantity == order.original_amount
            else OrderState.PARTIALLY_FILLED,
        )


@pytest.fixture
def exchange():
    return FakeExchange()


@pytest.fixture
def engine(tmp_path, exchange):
    with ExecutionJournal(tmp_path / "execution.jsonl") as journal:
        engine = StageExecutionEngine(
            exchange,
            journal,
            ExecutionPolicy(single_order_cap_mxn=D("12")),
            sleep=lambda _: None,
        )
        engine.startup()
        yield engine

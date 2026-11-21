import pytest
from fastapi.testclient import TestClient

from autofund.lab.app import create_lab_app
from autofund.lab.session import FLATTEN_PHRASE, START_PHRASE

ORIGIN = {"origin": "http://testserver"}


@pytest.fixture
def web(service):
    with TestClient(create_lab_app(service)) as client:
        token = client.get("/api/v1/control/session").json()["control_token"]
        client.headers.update({**ORIGIN, "x-autofund-control-token": token})
        yield client


def test_health_identifies_the_lab(web):
    body = web.get("/api/v1/health").json()
    assert body["product"] == "lab" and body["mode"] == "LAB_PAPER" and body["demo_mode"] is False


def test_status_is_complete_and_never_claims_certification(web):
    s = web.get("/api/v1/lab/status").json()
    assert s["certification"] == "UNCERTIFIED" and s["live_trading"] == "NOT_REPRESENTABLE"
    assert s["portfolio"]["account"]["equity"] == "100000"
    assert {x["id"] for x in s["strategies"]} == {"buy-hold", "momentum-126", "momentum-189", "momentum-252"}
    assert s["capture"]["running"] is False and s["session"]["state"] == "STOPPED"
    assert s["session"]["start_phrase"] == START_PHRASE


def test_mutations_require_origin_and_token(service):
    with TestClient(create_lab_app(service)) as bare:
        assert bare.post("/api/v1/lab/capture/start", json={"interval_seconds": 5}).status_code == 403
        assert bare.post("/api/v1/lab/session/kill", json={}, headers=ORIGIN).status_code == 403
        bad = {**ORIGIN, "x-autofund-control-token": "wrong"}
        assert bare.post("/api/v1/lab/session/kill", json={}, headers=bad).status_code == 403
        assert bare.get("/api/v1/lab/status").status_code == 200  # reads need no token


def test_foreign_host_is_rejected(service):
    with TestClient(create_lab_app(service), base_url="http://evil.example") as c:
        assert c.get("/api/v1/health").status_code == 400


def test_signals_endpoint(web):
    body = web.get("/api/v1/lab/signals/momentum-126").json()
    assert {r["symbol"]: r["action"] for r in body["rows"]} == {
        "SPY": "HOLD LONG", "QQQ": "HOLD LONG", "TLT": "CASH", "GLD": "CASH"}
    assert web.get("/api/v1/lab/signals/nope").status_code == 409


def test_capture_controls(web):
    started = web.post("/api/v1/lab/capture/start", json={"interval_seconds": 5}).json()
    assert started["running"] is True
    assert web.post("/api/v1/lab/capture/start", json={"interval_seconds": 5}).status_code == 409
    stopped = web.post("/api/v1/lab/capture/stop", json={}).json()
    assert stopped["running"] is False
    assert web.post("/api/v1/lab/capture/start", json={"interval_seconds": 1}).status_code == 422


def test_full_paper_flow_over_http(web, broker):
    plan = web.post("/api/v1/lab/session/plan",
                    json={"strategy_id": "momentum-126", "capital_fraction": "0.2"}).json()
    assert plan["executable"] and len(plan["orders"]) == 2
    # No session yet: the plan cannot be executed.
    assert web.post("/api/v1/lab/session/execute", json={"plan_id": plan["plan_id"]}).status_code == 409
    wrong = web.post("/api/v1/lab/session/start", json={
        "strategy_id": "momentum-126", "capital_fraction": "0.2", "confirmation": "yes"})
    assert wrong.status_code == 409
    ok = web.post("/api/v1/lab/session/start", json={
        "strategy_id": "momentum-126", "capital_fraction": "0.2", "confirmation": START_PHRASE})
    assert ok.json()["state"] == "ACTIVE"
    done = web.post("/api/v1/lab/session/execute", json={"plan_id": plan["plan_id"]}).json()
    assert [o["status"] for o in done["orders"]] == ["filled", "filled"]
    assert web.get("/api/v1/lab/portfolio").json()["positions"]
    assert {"PLAN", "EXECUTION"} <= {e["event"] for e in web.get("/api/v1/lab/journal").json()["items"]}
    assert web.post("/api/v1/lab/session/kill", json={"reason": "test"}).json()["state"] == "HALTED"
    assert broker.orders


def test_fraction_above_cap_and_garbage_are_rejected(web):
    assert web.post("/api/v1/lab/session/plan",
                    json={"strategy_id": "momentum-126", "capital_fraction": "0.9"}).status_code == 409
    assert web.post("/api/v1/lab/session/plan",
                    json={"strategy_id": "momentum-126", "capital_fraction": "abc"}).status_code == 422
    assert web.post("/api/v1/lab/session/plan",
                    json={"strategy_id": "momentum-126", "capital_fraction": "0.2", "x": 1}).status_code == 422


def test_flatten_confirmation(web, broker):
    from decimal import Decimal
    broker.positions = {"SPY": Decimal("100")}
    assert web.post("/api/v1/lab/session/flatten", json={"confirmation": "no"}).status_code == 409
    closed = web.post("/api/v1/lab/session/flatten", json={"confirmation": FLATTEN_PHRASE}).json()
    assert closed["closed"][0]["symbol"] == "SPY"


def test_no_route_can_reach_a_live_host_or_arbitrary_order(web):
    # The only order-creating route consumes a server-built plan id, never a symbol or size.
    routes = {r.path for r in web.app.routes if getattr(r, "methods", None) and "POST" in r.methods}
    assert not {p for p in routes if "order" in p and p != "/api/v1/lab/session/execute"}


def test_lab_cli_is_registered():
    import inspect

    import autofund.f4_cli as cli
    assert '"lab"' in inspect.getsource(cli)

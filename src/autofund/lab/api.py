"""HTTP surface of the lab. Mutations need the same-origin control token."""

import json
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from autofund.exchanges.alpaca.errors import AlpacaError
from autofund.lab.capture import CaptureError
from autofund.lab.service import LabService
from autofund.lab.session import SessionConfig, SessionError
from autofund.lab.signals import SignalError


class CaptureStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_seconds: int = Field(default=30, ge=5, le=3600)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy_id: str = Field(max_length=40)
    capital_fraction: str = Field(max_length=10)


class StartRequest(PlanRequest):
    auto_rebalance: bool = False
    confirmation: str = Field(max_length=100)


class ExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_id: str = Field(max_length=32)
    queue_if_closed: bool = False


class ReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="operator", max_length=200)


class FlattenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str = Field(max_length=100)


def _config(body: PlanRequest, auto: bool = False) -> SessionConfig:
    try:
        fraction = Decimal(body.capital_fraction)
    except InvalidOperation:
        raise HTTPException(status_code=422, detail="capital_fraction must be a decimal") from None
    return SessionConfig(body.strategy_id, fraction, auto)


def build_lab_router(service: LabService) -> APIRouter:
    router = APIRouter()

    def guarded(fn: Any) -> Any:
        try:
            return fn()
        except (SessionError, CaptureError, SignalError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except AlpacaError as exc:
            raise HTTPException(status_code=502, detail=f"{type(exc).__name__}: {exc}") from None

    @router.get("/api/v1/lab/status")
    def status() -> dict[str, Any]:
        return guarded(service.status)

    @router.get("/api/v1/lab/portfolio")
    def portfolio() -> dict[str, Any]:
        return guarded(lambda: service.portfolio(fresh=True))

    @router.get("/api/v1/lab/signals/{strategy_id}")
    def signals(strategy_id: str) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            s = service.signals.signals(strategy_id)
            return {"strategy_id": strategy_id, "as_of": s.as_of, "rows": [r.public() for r in s.rows]}
        return guarded(run)

    @router.get("/api/v1/lab/journal")
    def journal(limit: int = 100) -> dict[str, Any]:
        return {"items": service.session.journal.tail(max(1, min(limit, 500)))}

    @router.get("/api/v1/lab/equity")
    def equity() -> dict[str, Any]:
        return {"items": service.session.equity_history()}

    @router.post("/api/v1/lab/capture/start")
    def capture_start(body: CaptureStartRequest) -> dict[str, Any]:
        return guarded(lambda: service.capture.start(body.interval_seconds))

    @router.post("/api/v1/lab/capture/stop")
    def capture_stop(body: ReasonRequest) -> dict[str, Any]:
        return service.capture.stop(body.reason)

    @router.post("/api/v1/lab/session/plan")
    def plan(body: PlanRequest) -> dict[str, Any]:
        return guarded(lambda: service.session.build_plan(_config(body)).public())

    @router.post("/api/v1/lab/session/start")
    def start(body: StartRequest) -> dict[str, Any]:
        guarded(lambda: service.session.start(_config(body, body.auto_rebalance), body.confirmation))
        return service.session.status()

    @router.post("/api/v1/lab/session/execute")
    def execute(body: ExecuteRequest) -> dict[str, Any]:
        return guarded(lambda: service.session.execute(body.plan_id, queue_if_closed=body.queue_if_closed))

    @router.post("/api/v1/lab/session/stop")
    def stop(body: ReasonRequest) -> dict[str, Any]:
        guarded(lambda: service.session.stop(body.reason))
        return service.session.status()

    @router.post("/api/v1/lab/session/kill")
    def kill(body: ReasonRequest) -> dict[str, Any]:
        guarded(lambda: service.session.kill(body.reason))
        return service.session.status()

    @router.post("/api/v1/lab/session/flatten")
    def flatten(body: FlattenRequest) -> dict[str, Any]:
        return {"closed": guarded(lambda: service.session.flatten(body.confirmation))}

    @router.get("/api/v1/lab/export/journal")
    def export_journal() -> dict[str, Any]:
        return json.loads(json.dumps({"items": service.session.journal.tail(500)}, default=str))

    return router

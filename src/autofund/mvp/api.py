"""Same-origin control plane. Browser mutations control sessions, never orders."""

import io
import json
import secrets
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from autofund.live.models import LiveError

from .orchestrator import AutoFundOrchestrator, SessionConfig, SessionStartBlocked


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: str
    max_session_loss_mxn: str
    max_session_duration_seconds: int = Field(ge=60, le=86_400)
    max_orders_per_session: int = Field(ge=1, le=100)

    @field_validator("max_session_loss_mxn")
    @classmethod
    def loss(cls, value: str) -> str:
        try:
            amount = Decimal(value)
        except Exception:
            raise ValueError("invalid loss limit") from None
        if not Decimal("0") < amount <= Decimal("50"):
            raise ValueError("loss limit outside product bound")
        return value


class ActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="operator", max_length=200)


def create_mvp_app(orchestrator: AutoFundOrchestrator, dist: Path | None = None,
                   *, host: str = "127.0.0.1", port: int = 8000) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            # Release the backend publication thread; never touches financial state.
            orchestrator.shutdown()

    app = FastAPI(title="AutoFund MVP 0.2", version="0.2.0", docs_url=None, redoc_url=None, lifespan=lifespan)
    control_token = secrets.token_urlsafe(32)
    allowed_hosts = {host, f"{host}:{port}", "localhost", f"localhost:{port}", "testserver"}

    @app.middleware("http")
    async def security(request: Request, call_next: Any) -> Response:
        request_host = request.headers.get("host", "")
        if request_host not in allowed_hosts:
            return JSONResponse(status_code=400, content={"code": "INVALID_HOST"})
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            origin = request.headers.get("origin")
            expected = {f"http://{host}:{port}", f"http://localhost:{port}", "http://testserver"}
            if origin not in expected or request.headers.get("x-autofund-control-token") != control_token:
                return JSONResponse(status_code=403, content={"code": "CONTROL_AUTH_REQUIRED"})
        response = cast(Response, await call_next(request))
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin",
                                 "Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'"})
        return response

    @app.get("/api/v1/control/session")
    def control_session() -> dict[str, object]:
        return {"control_token": control_token, "confirmation_phrase": "START AUTOFUND REAL 50",
                "hard_bounds": {"authorized_capital_mxn": "50", "max_deployment_mxn": "25", "single_order_cap_mxn": "11"}}

    @app.get("/api/v1/health")
    def health() -> dict[str, object]:
        return {"application": "AutoFund", "product_version": "AutoFund MVP 0.1", "ready": True,
                "app_state": orchestrator.state, "demo_mode": orchestrator.demo}

    @app.get("/api/v1/mvp")
    def state() -> dict[str, Any]: return orchestrator.snapshot()

    @app.get("/api/v1/mvp/wallet")
    def wallet() -> dict[str, Any]:
        """Read-only account evidence, explicitly separate from AutoFund ownership."""
        snapshot = orchestrator.snapshot()
        return {"bitso_wallet": snapshot.get("wallet", {"status": "UNAVAILABLE", "balances": []}),
                "autofund_portfolio": {"cash_mxn": snapshot.get("cash_mxn"),
                                       "equity_mxn": snapshot.get("equity_mxn"),
                                       "position": snapshot.get("position")}}

    @app.get("/api/v1/mvp/scanner")
    def scanner() -> dict[str, Any]: return orchestrator.scanner_evidence()

    @app.get("/api/v1/mvp/learning")
    def learning() -> dict[str, Any]: return orchestrator.learning_view()

    @app.post("/api/v1/control/start")
    def start(body: StartRequest, _: str | None = Header(default=None)) -> dict[str, Any]:
        try:
            orchestrator.start(SessionConfig(Decimal(body.max_session_loss_mxn), body.max_session_duration_seconds,
                                             body.max_orders_per_session), body.confirmation)
        except SessionStartBlocked as exc:
            # Recoverable readiness blocker: exact names, 409, application stays STOPPED.
            raise HTTPException(status_code=409, detail={"code": "PRODUCTION_PREFLIGHT_BLOCKED",
                                                         "blockers": list(exc.blockers),
                                                         "message": str(exc)}) from None
        except (LiveError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return orchestrator.snapshot()

    @app.post("/api/v1/control/stop")
    def stop(body: ActionRequest) -> dict[str, Any]:
        try:
            orchestrator.stop(body.reason)
        except LiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return orchestrator.snapshot()

    @app.post("/api/v1/control/kill")
    def kill(body: ActionRequest) -> dict[str, Any]:
        try:
            orchestrator.kill(body.reason)
        except LiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        return orchestrator.snapshot()

    @app.get("/api/v1/telemetry")
    def telemetry(component: str | None = None, level: str | None = None,
                  checkpoint_type: str | None = None) -> dict[str, object]:
        rows = orchestrator.snapshot()["telemetry"]
        assert isinstance(rows, list)
        selected = [row for row in rows if (component is None or row.get("component") == component)
                    and (level is None or row.get("level") == level)
                    and (checkpoint_type is None or row.get("event") == checkpoint_type)]
        return {"items": selected, "total": len(selected)}

    @app.get("/api/v1/sessions/{session_id}/diagnostics")
    def diagnostics(session_id: str) -> Response:
        if not session_id.startswith("mvp-") or "/" in session_id or "\\" in session_id:
            raise HTTPException(status_code=404)
        # Refresh the read-only wallet view so the export is current, never a
        # startup snapshot presented as live.
        orchestrator.refresh_wallet()
        root = orchestrator.artifacts / session_id
        allowed = ("report.json", "handoff.json", "checkpoint_summary.json", "telemetry.jsonl")
        if not root.is_dir():
            raise HTTPException(status_code=404)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in allowed:
                path = root / name
                if path.is_file():
                    archive.writestr(name, path.read_bytes())
            archive.writestr("config-fingerprint.json", json.dumps({"product": "AutoFund MVP 0.1", "market": "btc_mxn"}))
        return Response(output.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{session_id}-diagnostics.zip"'})

    @app.get("/api/v1/mvp/stream")
    async def stream(request: Request) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            import asyncio
            while not await request.is_disconnected():
                yield "event: snapshot\ndata: " + json.dumps(orchestrator.snapshot(), default=str, separators=(",", ":")) + "\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    if dist and dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="mvp")
    else:
        @app.get("/")
        def missing() -> Response: return Response("AutoFund frontend build unavailable", status_code=503)
    return app

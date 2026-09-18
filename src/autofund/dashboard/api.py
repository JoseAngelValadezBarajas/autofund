"""GET-only FastAPI surface for shadow artifact projections."""
import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import models
from .service import DashboardDataProvider

MAX_PAGE = 200


def create_app(provider: DashboardDataProvider, dist: Path | None = None) -> FastAPI:
    app = FastAPI(title="AutoFund Monitoring Dashboard", version="0.6.0", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "same-origin", "Cache-Control": "no-store"})
        return response

    @app.exception_handler(FileNotFoundError)
    async def missing(_: Request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content=models.ApiError(code="SESSION_NOT_FOUND", message=str(exc)).model_dump())

    @app.exception_handler(RequestValidationError)
    async def invalid(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content=models.ApiError(code="INVALID_REQUEST", message="invalid pagination or range").model_dump())

    def source() -> DashboardDataProvider: return provider
    Provider = Annotated[DashboardDataProvider, Depends(source)]
    def page(items: Sequence[models.DashboardModel], limit: int, offset: int) -> dict[str, object]: return {"items": [x.model_dump(mode="json") for x in items], "limit": limit, "offset": offset, "total": len(items)}

    @app.get("/api/v1/health", response_model=models.Health)
    def health(data: Provider) -> models.Health: return data.health()
    @app.get("/api/v1/overview", response_model=models.Overview)
    def overview(data: Provider) -> models.Overview: return data.overview()
    @app.get("/api/v1/portfolio", response_model=models.Portfolio)
    def portfolio(data: Provider) -> models.Portfolio: return data.portfolio()
    @app.get("/api/v1/positions", response_model=list[models.Position])
    def positions(data: Provider) -> list[models.Position]: return data.portfolio().positions
    @app.get("/api/v1/market", response_model=models.Market)
    def market(data: Provider) -> models.Market: return data.market()
    @app.get("/api/v1/candles", response_model=list[models.Candle])
    def candles(data: Provider, limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 50) -> list[models.Candle]: return data.candles()[-limit:]
    @app.get("/api/v1/order-book", response_model=models.OrderBook)
    def order_book(data: Provider) -> models.OrderBook: return data.order_book()
    @app.get("/api/v1/risk", response_model=models.Risk)
    def risk(data: Provider) -> models.Risk: return data.risk()
    @app.get("/api/v1/quality", response_model=models.Quality)
    def quality(data: Provider) -> models.Quality: return data.quality()

    @app.get("/api/v1/signals")
    def signals(data: Provider, limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100, offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, object]: return page(data.signals()[offset:offset + limit], limit, offset)
    @app.get("/api/v1/shadow-fills")
    def fills(data: Provider, limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100, offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, object]: return page(data.fills()[offset:offset + limit], limit, offset)
    @app.get("/api/v1/ledger")
    def ledger(data: Provider, limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100, offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, object]: return page(data.ledger()[offset:offset + limit], limit, offset)
    @app.get("/api/v1/activity")
    def activity(data: Provider, limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 100, offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, object]: return page(data.activity()[offset:offset + limit], limit, offset)
    @app.get("/api/v1/sessions")
    def sessions(data: Provider) -> dict[str, object]: return page([data.session()], 1, 0)
    @app.get("/api/v1/sessions/{session_id}", response_model=models.SessionDetail)
    def session(session_id: str, data: Provider) -> models.SessionDetail:
        current = data.session()
        if session_id != current.session_id:
            raise FileNotFoundError("requested session is not available")
        return current
    @app.get("/api/v1/reports/current", response_model=models.Report)
    def current_report(data: Provider) -> models.Report: return data.report()
    @app.get("/api/v1/reports/weekly", response_model=models.Report)
    def weekly_report(data: Provider) -> models.Report: return data.report()

    @app.get("/api/v1/stream")
    async def stream(request: Request, data: Provider) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            while not await request.is_disconnected():
                payload = {"overview": data.overview().model_dump(mode="json"), "quality": data.quality().model_dump(mode="json")}
                yield f"event: snapshot\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
                await asyncio.sleep(3)
        return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    if dist and dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="dashboard")
    else:
        @app.get("/")
        def unavailable() -> Response: return Response("Dashboard build unavailable. Run npm run build in frontend/.", media_type="text/plain")
    return app

"""``autofund lab``: the paper-trading laboratory, served at 127.0.0.1.

A separate entry point from ``autofund demo`` on purpose. Demo mode strips exchange credentials and
asserts it holds no live capability; the lab *needs* paper credentials. Keeping them apart means
neither guarantee has to be weakened for the other.
"""

import argparse
import secrets
import threading
import webbrowser
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from autofund.lab.api import build_lab_router
from autofund.lab.service import LabService
from autofund.lab.session import FLATTEN_PHRASE, START_PHRASE
from autofund.version import PRODUCT_VERSION, VERSION
from autofund.web_security import (
    SECURITY_HEADERS,
    LaunchGate,
    host_allowed,
    install_launch_gate,
    token_matches,
)


def create_lab_app(service: LabService, dist: Path | None = None, *, host: str = "127.0.0.1",
                   port: int = 8000, gate: LaunchGate | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            service.shutdown()  # nothing the lab started outlives the application

    app = FastAPI(title=PRODUCT_VERSION + " lab", version=VERSION, docs_url=None, redoc_url=None,
                  lifespan=lifespan)
    token = secrets.token_urlsafe(32)
    # Registered first so it runs *inside* the Host/Origin check below (the last one added is the
    # outermost): a rebinding request is refused before anything else looks at it.
    install_launch_gate(app, gate)
    allowed_hosts = {host, f"{host}:{port}", "localhost", f"localhost:{port}", "testserver"}

    @app.middleware("http")
    async def security(request: Request, call_next: Any) -> Response:
        if request.headers.get("host", "") not in allowed_hosts or not host_allowed(request.headers.get("host", "")):
            return JSONResponse(status_code=400, content={"code": "INVALID_HOST"})
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected = {f"http://{host}:{port}", f"http://localhost:{port}", "http://testserver"}
            if request.headers.get("origin") not in expected or \
                    not token_matches(request.headers.get("x-autofund-control-token"), token):
                return JSONResponse(status_code=403, content={"code": "CONTROL_AUTH_REQUIRED"})
        response = cast(Response, await call_next(request))
        response.headers.update(SECURITY_HEADERS)
        return response

    @app.get("/api/v1/health")
    def health() -> dict[str, object]:
        return {"application": "AutoFund", "product": "lab", "product_version": PRODUCT_VERSION,
                "ready": True, "mode": "LAB_PAPER", "demo_mode": False}

    @app.get("/api/v1/control/session")
    def control_session() -> dict[str, object]:
        return {"control_token": token, "start_phrase": START_PHRASE, "flatten_phrase": FLATTEN_PHRASE}

    app.include_router(build_lab_router(service))
    if dist and dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="lab")
    else:
        @app.get("/")
        def missing() -> Response:
            return Response("AutoFund frontend build unavailable", status_code=503)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autofund lab")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-open-browser", action="store_true")
    args = parser.parse_args(argv)
    service = LabService()
    dist = Path(__file__).parents[3] / "frontend" / "dist"
    gate = LaunchGate()
    app = create_lab_app(service, dist, host=args.host, port=args.port, gate=gate)
    url = f"http://{args.host}:{args.port}"
    launch = gate.launch_url(url)
    print(f"AutoFund Lab {VERSION} — PAPER ONLY. Live trading is not representable in this build.")
    print("Capture and sessions run only while this window is open; closing it stops everything.")
    print("Personal data is written to research/ (git-ignored).")
    print("Open this link (it carries a key; other programs on this machine cannot use the page without it):")
    print("  " + launch)
    if not args.no_open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(launch)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0

"""Shared browser-facing security rules for every AutoFund web surface.

AutoFund serves its dashboards on the loopback interface only. Three attacks still matter for a page
that shows financial state and can start a session:

* **DNS rebinding.** A hostile site points its own name at 127.0.0.1, so the browser sends requests
  to this server with ``Host: evil.example``. Refusing any Host that is not loopback stops the
  attacker's page from reading responses.
* **Clickjacking.** A hostile site frames the dashboard and tricks a click. ``frame-ancestors 'none'``
  (and the older ``X-Frame-Options``) forbid being framed at all.
* **Token guessing by timing.** The control token is compared in constant time.
* **Another local process.** Binding to loopback does not stop *other programs on the same machine*
  from asking the server for its control token or reading balances. A :class:`LaunchGate` closes
  that: the terminal prints a one-time-per-run link carrying a random key, the browser trades it for
  an ``HttpOnly`` ``SameSite=Strict`` cookie, and every request without that cookie is refused.
"""

import hmac
import logging
import re
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "testserver"})

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)

SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def host_allowed(host_header: str) -> bool:
    """True only for a loopback host name, with or without a port."""
    hostname = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
    return hostname in LOOPBACK_HOSTNAMES


def token_matches(supplied: str | None, expected: str) -> bool:
    """Constant-time comparison; a missing token never matches."""
    return supplied is not None and hmac.compare_digest(supplied.encode(), expected.encode())


COOKIE_NAME = "af_session"
_LOCKED_PAGE = (
    "<!doctype html><meta charset=utf-8><title>AutoFund</title>"
    "<body style='font:16px system-ui;background:#09111d;color:#e6edf3;padding:40px'>"
    "<h1>AutoFund is locked</h1><p>Open the link printed in the terminal where you started "
    "AutoFund. It contains a key that only that terminal knows.</p>"
)


class LaunchGate:
    """Requires a session cookie that is only issued to whoever holds the launch link."""

    #: The link must be used within this many seconds of the app starting.
    LINK_LIFETIME_SECONDS = 900.0

    def __init__(self) -> None:
        self.launch_key = secrets.token_urlsafe(24)
        self._cookie = secrets.token_urlsafe(32)
        self._created = time.monotonic()
        self._redeemed = False
        self._lock = threading.Lock()

    def launch_url(self, base_url: str) -> str:
        return f"{base_url}/?k={self.launch_key}"

    def has_session(self, request: Request) -> bool:
        return token_matches(request.cookies.get(COOKIE_NAME), self._cookie)

    def redeem(self, request: Request) -> Response | None:
        """A matching ``?k=`` becomes the session cookie and a redirect to a clean URL.

        The key works **once** and only for a short time. A link that stayed valid until the app
        exited would be a bearer credential sitting in browser history, terminal scrollback and any
        log that recorded the URL; after the first redemption it is just a dead string.
        """
        key = request.query_params.get("k")
        if key is None or not token_matches(key, self.launch_key):
            return None
        with self._lock:
            expired = time.monotonic() - self._created > self.LINK_LIFETIME_SECONDS
            if self._redeemed or expired:
                return None
            self._redeemed = True
        response = RedirectResponse(url="/", status_code=303)  # always "/": never an open redirect
        response.set_cookie(COOKIE_NAME, self._cookie, httponly=True, samesite="strict", path="/")
        return response


def install_launch_gate(app: FastAPI, gate: LaunchGate | None) -> None:
    """Register the gate. Call it *before* the Host/Origin middleware so that one runs first."""
    if gate is None:
        return

    @app.middleware("http")
    async def launch_gate(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Any:
        redeemed = gate.redeem(request)
        if redeemed is not None:
            return redeemed
        if gate.has_session(request):
            return await call_next(request)
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=401, content={"code": "LAUNCH_AUTH_REQUIRED"})
        return HTMLResponse(_LOCKED_PAGE, status_code=401)


_KEY_IN_URL = re.compile(r"([?&])k=[^&\s\"]+")


class _RedactLaunchKey(logging.Filter):
    """Removes the launch key from any access-log line that carries it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_KEY_IN_URL.sub(r"\1k=REDACTED", a) if isinstance(a, str) else a
                                for a in record.args)
        if isinstance(record.msg, str):
            record.msg = _KEY_IN_URL.sub(r"\1k=REDACTED", record.msg)
        return True


def redact_launch_key_in_logs() -> None:
    """Install once; uvicorn's access log would otherwise print ``GET /?k=<key>`` to the terminal."""
    for name in ("uvicorn.access", "uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _RedactLaunchKey) for f in logger.filters):
            logger.addFilter(_RedactLaunchKey())

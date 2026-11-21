"""Shared browser-facing security rules for every AutoFund web surface.

AutoFund serves its dashboards on the loopback interface only. Three attacks still matter for a page
that shows financial state and can start a session:

* **DNS rebinding.** A hostile site points its own name at 127.0.0.1, so the browser sends requests
  to this server with ``Host: evil.example``. Refusing any Host that is not loopback stops the
  attacker's page from reading responses.
* **Clickjacking.** A hostile site frames the dashboard and tricks a click. ``frame-ancestors 'none'``
  (and the older ``X-Frame-Options``) forbid being framed at all.
* **Token guessing by timing.** The control token is compared in constant time.
"""

import hmac

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

"""GET-only scanner data source.

Deliberately built on the F4 strict read-only observer rather than the live
trading client. The live client is intentionally locked to `btc_mxn`; widening it
to reach other markets would erode the Production trading boundary. The observer
is already a GET-only surface, so the scanner uses it and the live path keeps its
single-market allowlist untouched.

No method here can produce an exchange write: the observer transport accepts GET
and rejects everything else before any request leaves the process.
"""

from decimal import Decimal
from typing import Any

from autofund.observer.client import (
    BitsoProductionReadOnlyClient,
    ProductionCredentials,
)

from .scanner import ScannerSource


class ReadOnlyScannerSource(ScannerSource):
    """Adapter from the F4 read-only client to the scanner's source protocol."""

    def __init__(self, client: BitsoProductionReadOnlyClient | None = None) -> None:
        self._client = client if client is not None else _default_client()
        self.request_count = 0

    def available_books(self) -> tuple[Any, ...]:
        self.request_count += 1
        return self._client.available_books()

    def ticker(self, book: str) -> Any:
        self.request_count += 1
        return self._client.ticker(book)

    def order_book(self, book: str) -> Any:
        self.request_count += 1
        return self._client.order_book(book)

    def fee_schedule(self, book: str) -> Any:
        self.request_count += 1
        return self._client.fee_schedule(book)


def _default_client() -> BitsoProductionReadOnlyClient:
    """Public market data needs no credentials; fees do.

    Confirmed Production read-only credentials are used when the operator has
    provided them, and the client degrades to public-only data otherwise. Absent
    credentials never grant any additional capability.
    """
    credentials = None
    try:
        credentials = ProductionCredentials.from_environment()
    except Exception:
        credentials = None
    return BitsoProductionReadOnlyClient(credentials)


def probe() -> dict[str, Any]:
    """Credential-free readiness probe used by tests and diagnostics."""
    return {"source": "F4_STRICT_READ_ONLY", "capability": "GET_ONLY",
            "produces_order_intent": False, "writes_allowed": 0,
            "cap_mxn": str(Decimal("11"))}

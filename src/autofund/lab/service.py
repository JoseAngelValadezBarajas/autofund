"""The lab's single facade: credentials, clients, capture, session and the research catalog."""

import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autofund.exchanges.alpaca.client import AlpacaCredentials, AlpacaDataClient
from autofund.exchanges.alpaca.env import load_credentials
from autofund.exchanges.alpaca.errors import AlpacaAuthenticationError, AlpacaError
from autofund.exchanges.alpaca.paper import AlpacaPaperClient

from .capture import CaptureController
from .paths import env_file, research_root
from .session import PaperSession
from .signals import (
    DEFAULT_UNIVERSE,
    STRATEGIES,
    SignalProvider,
    certification_status,
    research_catalog,
)

PORTFOLIO_TTL_SECONDS = 5.0


def default_credentials() -> AlpacaCredentials:
    path = env_file()
    return load_credentials(path) if path.exists() else AlpacaCredentials.from_env()


class LabService:
    def __init__(
        self,
        *,
        root: Path | None = None,
        credentials: Callable[[], AlpacaCredentials] = default_credentials,
        data_client: Callable[[], AlpacaDataClient] | None = None,
        paper_client: Callable[[], AlpacaPaperClient] | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        auto_interval_seconds: float = 60,
    ) -> None:
        self.root = root or research_root()
        self._credentials = credentials
        self._data_override, self._paper_override = data_client, paper_client
        self._lock = threading.Lock()
        self._data: AlpacaDataClient | None = None
        self._paper: AlpacaPaperClient | None = None
        self._portfolio: tuple[float, dict[str, Any]] | None = None
        self._now = now
        self.capture = CaptureController(root=self.root, data_client=self.data, symbols=DEFAULT_UNIVERSE,
                                         now=now)
        self.signals = SignalProvider(self.data, today=lambda: now().date())
        self.session = PaperSession(root=self.root, paper=self.paper, signals=self.signals, now=now,
                                    auto_interval_seconds=auto_interval_seconds)

    # ---- lazily built clients (one instance each, so rate limiting is shared) ----

    def data(self) -> AlpacaDataClient:
        if self._data_override:
            return self._data_override()
        with self._lock:
            if self._data is None:
                self._data = AlpacaDataClient(self._credentials())
            return self._data

    def paper(self) -> AlpacaPaperClient:
        if self._paper_override:
            return self._paper_override()
        with self._lock:
            if self._paper is None:
                self._paper = AlpacaPaperClient(self._credentials())
            return self._paper

    def credential_status(self) -> dict[str, Any]:
        try:
            creds = self._credentials()
            return {"configured": True, "paper_key": creds.key_id.startswith("PK"), "error": None}
        except AlpacaAuthenticationError as exc:
            return {"configured": False, "paper_key": False, "error": str(exc)}

    # ---- observation ----

    def portfolio(self, *, fresh: bool = False) -> dict[str, Any]:
        import time as _t
        cached = self._portfolio
        if not fresh and cached and _t.monotonic() - cached[0] < PORTFOLIO_TTL_SECONDS:
            return cached[1]
        client = self.paper()
        account, clock = client.get_account(), client.get_clock()
        positions, orders = client.get_positions(), client.list_orders("all", 25)
        out = {
            "account": {"status": account.status, "equity": str(account.equity),
                        "last_equity": str(account.last_equity), "cash": str(account.cash),
                        "buying_power": str(account.buying_power),
                        "day_change": str(account.equity - account.last_equity),
                        "trading_blocked": account.trading_blocked},
            "clock": asdict(clock),
            "positions": [{"symbol": p.symbol, "qty": str(p.qty), "market_value": str(p.market_value),
                           "avg_entry_price": str(p.avg_entry_price), "current_price": str(p.current_price),
                           "unrealized_pl": str(p.unrealized_pl)} for p in positions],
            "orders": [{"id": o.id, "client_order_id": o.client_order_id, "symbol": o.symbol,
                        "side": o.side, "status": o.status,
                        "notional": None if o.notional is None else str(o.notional),
                        "filled_qty": str(o.filled_qty),
                        "filled_avg_price": None if o.filled_avg_price is None else str(o.filled_avg_price),
                        "submitted_at": o.submitted_at} for o in orders],
        }
        self._portfolio = (_t.monotonic(), out)
        return out

    def status(self) -> dict[str, Any]:
        catalog = research_catalog(self.root)
        creds = self.credential_status()
        warnings: list[str] = []
        portfolio: dict[str, Any] | None = None
        if creds["configured"]:
            try:
                portfolio = self.portfolio()
                if self.session.state == "ACTIVE":
                    self.session.snapshot_equity()
            except AlpacaError as exc:
                warnings.append(f"Alpaca paper account unavailable: {exc}")
        else:
            warnings.append("Alpaca paper keys are not configured. Put AUTOFUND_ALPACA_DATA_API_KEY and "
                            "AUTOFUND_ALPACA_DATA_API_SECRET (paper keys, PK...) in .env and restart.")
        return {
            "product": "lab", "mode": "LAB_PAPER", "live_trading": "NOT_REPRESENTABLE",
            "checked_at": self._now().isoformat(),
            "credentials": creds, "portfolio": portfolio, "warnings": warnings,
            "capture": self.capture.status(), "session": self.session.status(),
            "certification": certification_status(catalog), "research": catalog,
            "strategies": [{"id": s.id, "label": s.label, "lookback": s.lookback,
                            "universe": list(s.universe), "description": s.description}
                           for s in STRATEGIES.values()],
        }

    def shutdown(self) -> None:
        """Application exit: stop capture and any session automation. Nothing outlives the app."""
        try:
            self.capture.stop("application exit")
        finally:
            self.session.shutdown()


__all__ = ["LabService", "default_credentials", "time"]

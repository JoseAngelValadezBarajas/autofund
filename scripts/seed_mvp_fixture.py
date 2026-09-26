"""Serve a deterministic MVP app pre-seeded with 0.1.1 postmortem evidence.

Used only by Playwright. It synthesizes no trading behavior: it completes one
DEMO session (which never contacts Bitso), injects the first-Production-session
evidence shape, and serves it on a dedicated port so the post-session, Learning
and Scanner pages can be visually verified without any Production traffic.
"""

import argparse
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import uvicorn

from autofund.mvp import scanner as sc
from autofund.mvp.api import create_mvp_app
from autofund.mvp.champion import ENTRY_CONDITION_NOT_MET
from autofund.mvp.orchestrator import (
    STOP_MAX_DURATION,
    AutoFundOrchestrator,
    DemoAutonomousRunner,
    SessionConfig,
)


class _Limits:
    def __init__(self, book, minimum_value, amount="0.000001"):
        self.book, self.minimum_value, self.minimum_amount = book, Decimal(minimum_value), Decimal(amount)


class _Level:
    def __init__(self, price, amount):
        self.price, self.amount = Decimal(price), Decimal(amount)


class _Ticker:
    def __init__(self, book, bid, ask, high, low):
        self.book, self.bid, self.ask = book, Decimal(bid), Decimal(ask)
        self.high, self.low, self.volume, self.vwap = Decimal(high), Decimal(low), Decimal("100"), Decimal("100")


class _Depth:
    def __init__(self, book, bid, ask, amount, sequence, at):
        self.book, self.timestamp, self.sequence = book, at, sequence
        self.bids, self.asks = (_Level(bid, amount),), (_Level(ask, amount),)

    @property
    def best_bid(self):
        return self.bids[0].price

    @property
    def best_ask(self):
        return self.asks[0].price

    @property
    def spread_bps(self):
        mid = (self.best_ask + self.best_bid) / Decimal("2")
        return (self.best_ask - self.best_bid) / mid * Decimal("10000")


class _Fee:
    def __init__(self, book):
        self.book = book
        self.maker_fee_decimal = Decimal("0.0065")
        self.taker_fee_decimal = Decimal("0.0078")


class FixtureScannerSource:
    """Deterministic GET-only market data with a spread of eligibility outcomes."""

    def __init__(self, now):
        self.now, self.calls = now, []

    def available_books(self):
        self.calls.append("GET")
        return (_Limits("btc_mxn", "10"), _Limits("usd_mxn", "10"),
                _Limits("eth_mxn", "10"), _Limits("sol_mxn", "60"), _Limits("btc_usd", "10"))

    def ticker(self, book):
        self.calls.append("GET")
        rows = {"btc_mxn": ("1000000", "1000010", "1010000", "990000"),
                "usd_mxn": ("18", "18.9", "18.9", "17.95"),
                "eth_mxn": ("50000", "50300", "54000", "46000"),
                "sol_mxn": ("3000", "3001", "3300", "2700")}
        return _Ticker(book, *rows[book])

    def order_book(self, book):
        self.calls.append("GET")
        rows = {"btc_mxn": ("1000000", "1000010", "0.0002"),
                "usd_mxn": ("18", "18.9", "100"),
                "eth_mxn": ("50000", "50300", "0.001"),
                "sol_mxn": ("3000", "3001", "0.01")}
        return _Depth(book, *rows[book], 1, self.now)

    def fee_schedules(self):
        self.calls.append("GET")
        return tuple(_Fee(book) for book in ("btc_mxn", "usd_mxn", "eth_mxn", "sol_mxn"))


def seed(orchestrator: AutoFundOrchestrator) -> None:
    """Complete one demo session and inject the first-session evidence shape.

    Mirrors the real runner's emission order: one NO_SIGNAL per closed candle for
    the insufficient-history preconditions, then an eligible STRATEGY_EVALUATED
    plus its NO_SIGNAL for every subsequent candle.
    """
    orchestrator.start(SessionConfig(max_session_duration_seconds=3600), "START AUTOFUND REAL 50")
    orchestrator.snapshot()
    now = datetime.now(UTC)
    for index in range(2):
        orchestrator._checkpoint("NO_SIGNAL", component="strategy",
                                 message="Insufficient closed-candle evidence",
                                 reason_code="INSUFFICIENT_HISTORY", eligible=False)
    for index in range(58):
        distance = Decimal("0.0031") + Decimal(index) * Decimal("0.0003")
        orchestrator._checkpoint("CANDLE_CLOSED", component="market", message="BTC/MXN closed candle accepted")
        orchestrator._checkpoint("STRATEGY_EVALUATED", component="strategy", message="Champion chose no trade",
                                 reason_code=ENTRY_CONDITION_NOT_MET, eligible=True, profile_id="mean-reversion-safe",
                                 strategy_fingerprint="fp", market_regime="NORMAL",
                                 distance_to_signal=str(distance), near_signal=False,
                                 timestamp_utc=(now + timedelta(seconds=index)).isoformat())
        orchestrator._checkpoint("NO_SIGNAL", component="strategy", message="Champion chose no trade",
                                 reason_code=ENTRY_CONDITION_NOT_MET, eligible=True,
                                 distance_to_signal=str(distance))
    orchestrator._requested_stop = STOP_MAX_DURATION
    orchestrator.stop(STOP_MAX_DURATION)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seed-mvp-fixture")
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument("--artifacts", type=Path, default=Path("artifacts/playwright-mvp-fixture"))
    args = parser.parse_args(argv)
    artifacts = args.artifacts
    artifacts.mkdir(parents=True, exist_ok=True)
    # The Control Center reads its research registry from this directory, so the synthetic dataset
    # must be generated here or the dashboard reports no campaigns - which makes the engineering
    # verdict DEGRADED, because collectors then read UNKNOWN, and the end-to-end assertion fails for
    # a reason that has nothing to do with the UI under test.
    #
    # This was masked locally because `artifacts/demo` already existed from an earlier run, so the
    # registry found campaigns that CI never created. A fixture that only works on the machine it
    # was written on is not a fixture.
    from autofund.demo import generate

    generate(artifacts_root=artifacts)
    orchestrator = AutoFundOrchestrator(artifacts, DemoAutonomousRunner(), demo=True)
    orchestrator.startup()
    seed(orchestrator)
    orchestrator.start_scanner(sc.MarketScanner(FixtureScannerSource(datetime.now(UTC))), interval_seconds=3600)
    orchestrator.scan_markets(now=datetime.now(UTC))
    dist = Path(__file__).parents[1] / "frontend" / "dist"
    # The artifacts root is passed through so the research read model is built over the same
    # directory this fixture just seeded, rather than over the developer's own artifacts.
    app = create_mvp_app(orchestrator, dist, host="127.0.0.1", port=args.port,
                         artifacts_root=artifacts)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())

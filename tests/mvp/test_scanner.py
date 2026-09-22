"""Deterministic market scanner tests, including explicit safety proofs.

All market data is synthetic and injected. Nothing here contacts an exchange and
no test may produce an order POST.
"""

from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from autofund.mvp import scanner as sc
from autofund.mvp.scanner import ELIGIBLE, INELIGIBLE_CAP, INELIGIBLE_DEPTH
from autofund.mvp.scoring import (
    FRICTION_VERSION,
    SCORE_VERSION,
    WEIGHTS,
    estimate_friction,
    score_market,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


class Limits:
    def __init__(self, book: str, minimum_value: str, minimum_amount: str = "0.000001") -> None:
        self.book, self.minimum_value, self.minimum_amount = book, D(minimum_value), D(minimum_amount)


class Level:
    def __init__(self, price: str, amount: str) -> None:
        self.price, self.amount = D(price), D(amount)


class Ticker:
    def __init__(self, book: str, bid: str, ask: str, high: str, low: str, volume: str = "100") -> None:
        self.book, self.bid, self.ask = book, D(bid), D(ask)
        self.high, self.low, self.volume, self.vwap = D(high), D(low), D(volume), D("100")


class Depth:
    def __init__(self, book: str, bid: str, ask: str, amount: str, sequence: int = 1,
                 at: datetime | None = None) -> None:
        self.book, self.timestamp, self.sequence = book, at or NOW, sequence
        self.bids, self.asks = (Level(bid, amount),), (Level(ask, amount),)

    @property
    def best_bid(self) -> D:
        return self.bids[0].price

    @property
    def best_ask(self) -> D:
        return self.asks[0].price

    @property
    def spread_bps(self) -> D:
        mid = (self.best_ask + self.best_bid) / D("2")
        return (self.best_ask - self.best_bid) / mid * D("10000")


class Fee:
    def __init__(self, book: str, rate: str = "0.0078") -> None:
        self.book = book
        self.maker_fee_decimal = D("0.0065")
        self.taker_fee_decimal = D(rate)


class Source:
    """GET-only fake exchange. Records every call for safety assertions."""

    def __init__(self, books, ticks, depths, *, fees=True) -> None:
        self.books, self.ticks, self.depths, self.fees = books, ticks, depths, fees
        self.calls: list[str] = []
        self.order_posts = 0

    def available_books(self):
        self.calls.append("GET /available_books")
        return self.books

    def ticker(self, book):
        self.calls.append(f"GET /ticker {book}")
        return self.ticks[book]

    def order_book(self, book):
        self.calls.append(f"GET /order_book {book}")
        return self.depths[book]

    def fee_schedules(self):
        self.calls.append("GET /fees")
        if not self.fees:
            raise RuntimeError("fee data unavailable")
        return tuple(Fee(book.book) for book in self.books)


def source(*, usd_amount: str = "100", usd_bid: str = "18", usd_ask: str = "18.001",
           usd_high: str = "18.1", usd_low: str = "17.9",
           btc_amount: str = "0.0002", btc_high: str = "1010000", btc_low: str = "990000",
           sol_min: str = "10", staleness: int = 0, fees: bool = True,
           include_usd: bool = True) -> Source:
    books = [Limits("btc_mxn", "10"), Limits("sol_mxn", sol_min), Limits("btc_usd", "10"),
             Limits("eth_btc", "0.0001")]
    if include_usd:
        books.append(Limits("usd_mxn", "10"))
    ticks = {"btc_mxn": Ticker("btc_mxn", "1000000", "1000010", btc_high, btc_low),
             "sol_mxn": Ticker("sol_mxn", "3000", "3001", "3300", "2700"),
             "usd_mxn": Ticker("usd_mxn", usd_bid, usd_ask, usd_high, usd_low)}
    depths = {"btc_mxn": Depth("btc_mxn", "1000000", "1000010", btc_amount, 1,
                               NOW.replace(minute=max(0, NOW.minute - 0)) if not staleness
                               else NOW.replace(second=0) - __import__("datetime").timedelta(seconds=staleness)),
              "sol_mxn": Depth("sol_mxn", "3000", "3001", "0.01", 2),
              "usd_mxn": Depth("usd_mxn", usd_bid, usd_ask, usd_amount, 3)}
    return Source(books, ticks, depths, fees=fees)


def scan(source_obj: Source, **kwargs) -> dict:
    return sc.MarketScanner(source_obj, **kwargs).scan(now=NOW)


def by_book(evidence: dict, book: str) -> dict:
    return next(item for item in evidence["candidates"] if item["book"] == book)


def test_dynamic_mxn_discovery_excludes_non_mxn_books():
    evidence = scan(source())
    universe = {item["book"] for item in evidence["candidates"]}
    # Discovery is driven by the exchange response, filtered to *_mxn only.
    assert universe == {"btc_mxn", "sol_mxn", "usd_mxn"}
    assert "btc_usd" not in universe and "eth_btc" not in universe


def test_moderate_movement_with_good_spread_and_depth_is_eligible():
    evidence = scan(source())
    btc = by_book(evidence, "btc_mxn")
    assert btc["status"] == ELIGIBLE
    assert btc["cap_executable"] is True
    assert btc["rank"] >= 1
    assert D(btc["score"]) > D("0")


def test_high_movement_but_terrible_spread_is_not_eligible():
    wide = source(usd_bid="18", usd_ask="25")
    evidence = scan(wide)
    usd = by_book(evidence, "usd_mxn")
    assert usd["status"] == sc.INELIGIBLE_SPREAD
    assert D(usd["spread_bps"]) > D("100")
    # Still reported, and demonstrably penalised on friction components.
    assert D(usd["components"]["spread_cost_score"]) < D("1")


def test_high_movement_with_insufficient_depth_is_not_eligible():
    evidence = scan(source(btc_amount="0.0000001"))
    btc = by_book(evidence, "btc_mxn")
    assert btc["status"] == INELIGIBLE_DEPTH
    assert D(btc["depth_mxn"]) < D("11")


def test_minimum_above_live_cap_is_rejected_and_cap_is_never_raised():
    evidence = scan(source(sol_min="60"))
    sol = by_book(evidence, "sol_mxn")
    assert sol["status"] == INELIGIBLE_CAP
    assert sol["cap_executable"] is False
    # The 11 MXN single-order hard cap is unchanged by the scanner.
    assert sc.MAX_SINGLE_ORDER_CAP_MXN == D("11")


def test_stale_market_is_invalid_data():
    evidence = scan(source(staleness=600))
    btc = by_book(evidence, "btc_mxn")
    assert btc["status"] == sc.STALE_DATA
    assert btc["data_quality"] == "INVALID"


def test_missing_fee_data_is_a_safe_rejection():
    evidence = scan(source(fees=False))
    btc = by_book(evidence, "btc_mxn")
    assert btc["status"] == sc.MISSING_FEE_DATA
    assert btc["cap_executable"] is False
    assert btc["best_bid_mxn"] == "1000000"
    assert btc["best_ask_mxn"] == "1000010"
    assert btc["movement_bps"] is not None and btc["depth_mxn"] is not None
    assert btc["maker_fee"] is None and btc["taker_fee"] is None
    assert evidence["fee_source"] == "UNAVAILABLE"
    assert evidence["status"] == "DEGRADED"
    assert evidence["error"] == "ACCOUNT_FEE_SOURCE_UNAVAILABLE"


def test_missing_one_book_fee_rejects_only_that_market():
    src = source()
    src.fee_schedules = lambda: (Fee("btc_mxn", "0.0078"), Fee("usd_mxn", "0.002"))
    evidence = scan(src)
    assert by_book(evidence, "btc_mxn")["status"] == ELIGIBLE
    assert by_book(evidence, "usd_mxn")["taker_fee"] == "0.002"
    sol = by_book(evidence, "sol_mxn")
    assert sol["status"] == sc.MISSING_FEE_DATA and sol["best_bid_mxn"] == "3000"
    assert evidence["books_with_account_fee"] == 2
    assert evidence["books_with_market_data"] == 3
    assert evidence["status"] == "DEGRADED"
    assert evidence["error"] == "ACCOUNT_FEE_DATA_INCOMPLETE"


def test_account_fee_snapshot_is_cached_once_for_all_books():
    src = source()
    scanner = sc.MarketScanner(src, interval_seconds=300)
    first = scanner.scan(now=NOW)
    second = scanner.scan(now=NOW)
    assert src.calls.count("GET /fees") == 1
    assert [row["data_fingerprint"] for row in first["candidates"]] == \
        [row["data_fingerprint"] for row in second["candidates"]]


def test_scanner_uses_only_get_and_never_posts():
    src = source()
    scan(src)
    assert src.calls, "scanner must read market data"
    assert all(call.startswith("GET ") for call in src.calls)
    assert src.order_posts == 0
    assert not any("POST" in call or "orders" in call for call in src.calls)


def test_ranking_is_deterministic_for_identical_inputs():
    first = scan(source())
    second = scan(source())
    assert [item["book"] for item in first["candidates"]] == [item["book"] for item in second["candidates"]]
    assert [item["score"] for item in first["candidates"]] == [item["score"] for item in second["candidates"]]
    assert [item["rank"] for item in first["candidates"]] == [item["rank"] for item in second["candidates"]]
    assert [item["data_fingerprint"] for item in first["candidates"]] == \
        [item["data_fingerprint"] for item in second["candidates"]]


def test_cheaper_market_outranks_expensive_but_volatile_market():
    # usd_mxn swings 5% while BTC/MXN swings 2%, but usd_mxn pays a wide spread.
    evidence = scan(source(usd_bid="18", usd_ask="18.9", usd_high="18.9", usd_low="17.95",
                           btc_high="1010000", btc_low="990000"))
    usd, btc = by_book(evidence, "usd_mxn"), by_book(evidence, "btc_mxn")
    assert D(usd["movement_bps"]) > D(btc["movement_bps"])
    # Higher movement still ranks below once trading friction is accounted for.
    assert D(usd["score"]) < D(btc["score"])


def test_scanner_failure_degrades_only_the_scanner():
    class Broken:
        def available_books(self):
            raise RuntimeError("MARKET_SCANNER_UNAVAILABLE")

    scanner = sc.MarketScanner(Broken())
    evidence = scanner.scan(now=NOW)
    assert evidence["degraded"] is True
    assert evidence["candidates"] == []
    assert evidence["live_market"] == "btc_mxn"
    assert evidence["execution_path_to_production"] == "NOT_PRESENT"
    # A scanner fault cannot alter the live market or the production path.
    assert evidence["production_market_rotation"] == "DISABLED"
    assert evidence["market_promotion"] == "DISABLED"


def test_strategy_compatibility_is_explicit_for_other_markets():
    evidence = scan(source())
    assert by_book(evidence, "btc_mxn")["strategy_compatibility"] == sc.STRATEGY_COMPATIBLE
    assert by_book(evidence, "usd_mxn")["strategy_compatibility"] == sc.STRATEGY_RESEARCH_ONLY
    assert all(item["lifecycle"] in {"SHADOW_CANDIDATE", "REJECTED"} for item in evidence["candidates"])


def test_scanner_exposes_no_production_execution_path():
    evidence = scan(source())
    assert evidence["live_market"] == "btc_mxn"
    assert evidence["production_market_rotation"] == "DISABLED"
    assert evidence["market_promotion"] == "DISABLED"
    assert evidence["execution_path_to_production"] == "NOT_PRESENT"
    assert evidence["read_only"] is True


def test_live_production_market_cannot_be_changed_by_the_scanner():
    src = source()
    scanner = sc.MarketScanner(src)
    scanner.scan(now=NOW)
    # The live market is a module constant, not scanner state.
    assert sc.LIVE_MARKET == "btc_mxn"
    assert scanner.evidence()["live_market"] == "btc_mxn"
    assert not hasattr(scanner, "set_live_market")


def test_shadow_candidates_never_create_order_intents():
    src = source()
    scanner = sc.MarketScanner(src)
    scanner.scan(now=NOW)
    for row in scanner.evidence()["shadow"]:
        assert row["lifecycle"] == "RESEARCH_ONLY"
        assert "order" not in row
        assert "intent" not in row
    assert src.order_posts == 0


# --------------------------------------------------------------- scoring
def test_score_components_are_bounded_and_weights_are_exposed():
    score = score_market(movement_bps=D("100"), volatility_bps=D("50"), spread_bps=D("10"),
                         depth_mxn=D("250"), volume_mxn=D("1000"), taker_rate=D("0.005"),
                         quality="VALID", staleness_seconds=D("1"))
    assert score.version == SCORE_VERSION
    assert D("0") <= score.score <= D("100")
    for value in score.components.values():
        assert D("0") <= D(value) <= D("1")
    assert score.weights == {key: str(value) for key, value in WEIGHTS.items()}
    assert "movement_score" in score.components


def test_score_is_deterministic_and_monotonic_in_cost():
    cheap = score_market(movement_bps=D("100"), volatility_bps=D("50"), spread_bps=D("2"),
                         depth_mxn=D("500"), volume_mxn=D("1000"), taker_rate=D("0.001"),
                         quality="VALID", staleness_seconds=D("0"))
    dear = score_market(movement_bps=D("100"), volatility_bps=D("50"), spread_bps=D("80"),
                        depth_mxn=D("500"), volume_mxn=D("1000"), taker_rate=D("0.01"),
                        quality="VALID", staleness_seconds=D("0"))
    assert cheap.score > dear.score
    again = score_market(movement_bps=D("100"), volatility_bps=D("50"), spread_bps=D("2"),
                         depth_mxn=D("500"), volume_mxn=D("1000"), taker_rate=D("0.001"),
                         quality="VALID", staleness_seconds=D("0"))
    assert again.score == cheap.score


def test_degraded_data_lowers_the_score():
    good = score_market(movement_bps=D("100"), volatility_bps=D("50"), spread_bps=D("5"),
                        depth_mxn=D("500"), volume_mxn=D("1000"), taker_rate=D("0.005"),
                        quality="VALID", staleness_seconds=D("0"))
    bad = score_market(movement_bps=D("100"), volatility_bps=D("50"), spread_bps=D("5"),
                       depth_mxn=D("500"), volume_mxn=D("1000"), taker_rate=D("0.005"),
                       quality="INVALID", staleness_seconds=D("0"))
    assert bad.score < good.score


def test_friction_exposes_its_assumptions_and_scales_with_cost():
    friction = estimate_friction(notional_mxn=D("11"), spread_bps=D("20"), taker_rate=D("0.0078"),
                                 depth_mxn=D("100"))
    assert friction.round_trip_mxn > D("0")
    assert friction.assumptions
    assert friction.round_trip_bps > D("0")
    cheap = estimate_friction(notional_mxn=D("11"), spread_bps=D("1"), taker_rate=D("0.001"),
                              depth_mxn=D("100"))
    assert cheap.round_trip_mxn < friction.round_trip_mxn
    assert FRICTION_VERSION in friction.telemetry()["version"]


def test_movement_smaller_than_friction_is_reported_by_coverage():
    score = score_market(movement_bps=D("1"), volatility_bps=D("1"), spread_bps=D("50"),
                         depth_mxn=D("20"), volume_mxn=D("50"), taker_rate=D("0.01"),
                         quality="VALID", staleness_seconds=D("0"))
    # Coverage below 1 means observed movement cannot pay for a round trip.
    assert D(score.friction_coverage) < D("1")
    thin = score_market(movement_bps=D("500"), volatility_bps=D("300"), spread_bps=D("1"),
                        depth_mxn=D("900"), volume_mxn=D("9000"), taker_rate=D("0.001"),
                        quality="VALID", staleness_seconds=D("0"))
    assert D(thin.friction_coverage) > D("1")


@pytest.mark.parametrize("quality", ["VALID", "DEGRADED", "INVALID"])
def test_quality_inputs_never_raise(quality):
    score = score_market(movement_bps=None, volatility_bps=None, spread_bps=None, depth_mxn=None,
                         volume_mxn=None, taker_rate=None, quality=quality, staleness_seconds=None)
    assert D("0") <= score.score <= D("100")
    assert score.friction_coverage == "NOT_APPLICABLE"

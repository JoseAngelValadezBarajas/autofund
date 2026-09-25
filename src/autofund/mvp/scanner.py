"""Read-only Market Opportunity Scanner.

Research functionality that sits *beside* Production, never inside it:

- discovery, ranking and shadow evaluation are GET-only;
- no scanner result can create a Production order intent, reach the live
  ExecutionEngine, change the live market or alter the Champion;
- a scanner failure degrades the scanner only. It cannot touch the ledger and
  cannot halt a valid BTC/MXN session.

Discovery is dynamic: the MXN universe comes from the exchange's own
available-books response, filtered to `*_mxn` because AutoFund's financial
envelope and ledger are MXN-denominated. Nothing is hardcoded and no
cross-currency accounting is introduced.
"""

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from autofund.decimal_utils import ZERO, financial
from autofund.replay.serialization import fingerprint

from .scoring import (
    MAX_DATA_AGE_SECONDS,
    MAX_ELIGIBLE_SPREAD_BPS,
    MAX_SINGLE_ORDER_CAP_MXN,
    MIN_REQUIRED_DEPTH_MXN,
    SCORE_VERSION,
    score_market,
)

# Eligibility outcomes. ELIGIBLE means "research candidate", never "buy".
ELIGIBLE = "ELIGIBLE"
INVALID_DATA = "INVALID_DATA"
STALE_DATA = "STALE_DATA"
INELIGIBLE_MINIMUM = "INELIGIBLE_MINIMUM"
INELIGIBLE_CAP = "INELIGIBLE_CAP"
INELIGIBLE_SPREAD = "INELIGIBLE_SPREAD"
INELIGIBLE_DEPTH = "INELIGIBLE_DEPTH"
MISSING_FEE_DATA = "MISSING_FEE_DATA"
UNSUPPORTED_ACCOUNTING = "UNSUPPORTED_ACCOUNTING"
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"

# The live Production market is immutable in this milestone.
LIVE_MARKET = "btc_mxn"
PRODUCTION_MARKET_ROTATION = "DISABLED"
MARKET_PROMOTION = "DISABLED"

# Strategies are only certified for the markets they were certified on. Applying
# an uncertified strategy elsewhere is research, never comparable live evidence.
STRATEGY_COMPATIBLE = "CERTIFIED_FOR_MARKET"
STRATEGY_RESEARCH_ONLY = "RESEARCH_ONLY"
CHAMPION_SUPPORTED_MARKETS = frozenset({"btc_mxn"})

DEFAULT_SCAN_INTERVAL_SECONDS = 300


class ScannerSource(Protocol):
    """GET-only market data provider. Implemented by the Production read client."""

    def available_books(self) -> tuple[Any, ...]: ...
    def ticker(self, book: str) -> Any: ...
    def order_book(self, book: str) -> Any: ...
    def fee_schedules(self) -> tuple[Any, ...]: ...


@dataclass(frozen=True, slots=True)
class MarketCandidate:
    book: str
    rank: int
    status: str
    reason: str
    score: str
    score_version: str
    components: dict[str, str]
    movement_bps: str | None
    volatility_bps: str | None
    high_low_range_bps: str | None
    best_bid_mxn: str | None
    best_ask_mxn: str | None
    spread_bps: str | None
    depth_mxn: str | None
    volume_mxn: str | None
    minimum_order_mxn: str | None
    maker_fee: str | None
    taker_fee: str | None
    estimated_round_trip_friction_mxn: str | None
    estimated_round_trip_friction_bps: str | None
    data_quality: str
    cap_executable: bool
    strategy_compatibility: str
    lifecycle: str
    shadow_evaluations: int
    shadow_signals: int
    shadow_net_pnl_mxn: str | None
    evidence_count: int
    data_fingerprint: str

    def public(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def strategy_compatibility(book: str) -> str:
    return STRATEGY_COMPATIBLE if book in CHAMPION_SUPPORTED_MARKETS else STRATEGY_RESEARCH_ONLY


@financial
def movement_bps(high: Decimal, low: Decimal) -> Decimal | None:
    if high <= ZERO or low <= ZERO or high < low:
        return None
    mid = (high + low) / Decimal("2")
    return ((high - low) / mid) * Decimal("10000")


class MarketScanner:
    """Bounded, deterministic scanner over the exchange's MXN universe."""

    def __init__(self, source: ScannerSource | None = None, *,
                 interval_seconds: int = DEFAULT_SCAN_INTERVAL_SECONDS,
                 max_shadow_candidates: int = 3,
                 cap_mxn: Decimal = MAX_SINGLE_ORDER_CAP_MXN) -> None:
        self._source = source
        self.interval_seconds = interval_seconds
        self.max_shadow_candidates = max_shadow_candidates
        self.cap_mxn = cap_mxn
        self.candidates: list[MarketCandidate] = []
        self.universe_size = 0
        self.scanned_at: datetime | None = None
        self.degraded = False
        self.last_error: str | None = None
        self.fee_source = "UNAVAILABLE"
        self.last_fee_refresh_at: datetime | None = None
        self._fee_cache: dict[str, Any] = {}
        self._fee_cache_at: datetime | None = None
        self.shadow: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------- discovery
    def discover(self) -> tuple[str, ...]:
        """MXN books from the exchange response. Never a hardcoded list."""
        assert self._source is not None
        return tuple(sorted(book.book for book in self._source.available_books()
                            if str(book.book).endswith("_mxn")))

    @financial
    def _evaluate_market(self, book: str, limits: Any, now: datetime,
                         fee_map: dict[str, Any]) -> MarketCandidate:
        """Collect GET-only observations and apply the hard eligibility filters."""
        try:
            ticker = self._source.ticker(book)          # type: ignore[union-attr]
            depth = self._source.order_book(book)       # type: ignore[union-attr]
        except Exception:
            return self._rejected(book, INVALID_DATA, "Market read unavailable", limits)

        age = Decimal(str(max(ZERO, Decimal(str((now - depth.timestamp).total_seconds())))))
        if age > MAX_DATA_AGE_SECONDS:
            return self._rejected(book, STALE_DATA, f"Order book age {age}s exceeds limit", limits)
        if ticker.bid <= ZERO or ticker.ask <= ticker.bid:
            return self._rejected(book, INVALID_DATA, "Crossed or nonpositive book", limits)

        total_depth = sum((level.price * level.amount for level in depth.bids[:5]), ZERO)
        volume_mxn = ticker.volume * ticker.vwap
        movement = movement_bps(ticker.high, ticker.low)
        spread_bps = depth.spread_bps
        minimum_value = limits.minimum_value
        fee = fee_map.get(book)
        maker = fee.maker_fee_decimal if fee is not None else None
        taker = fee.taker_fee_decimal if fee is not None else None
        score = score_market(movement_bps=movement, volatility_bps=movement,
                             spread_bps=spread_bps, depth_mxn=total_depth, volume_mxn=volume_mxn,
                             taker_rate=taker, quality="VALID", staleness_seconds=age,
                             notional_mxn=self.cap_mxn)

        if fee is None:
            return self._candidate(book, MISSING_FEE_DATA, "Account fee data unavailable",
                                   limits, ticker, depth, maker, taker, total_depth, volume_mxn,
                                   movement, spread_bps, score, age)

        # Hard filters run before ranking. Order is deliberate and documented.
        if minimum_value > self.cap_mxn:
            return self._candidate(book, INELIGIBLE_CAP,
                                   f"Minimum order {minimum_value} exceeds the {self.cap_mxn} MXN single-order cap",
                                   limits, ticker, depth, maker, taker, total_depth, volume_mxn, movement,
                                   spread_bps, score, age)
        if minimum_value <= ZERO:
            return self._rejected(book, INELIGIBLE_MINIMUM, "Invalid exchange minimum", limits)
        if spread_bps > MAX_ELIGIBLE_SPREAD_BPS:
            return self._candidate(book, INELIGIBLE_SPREAD,
                                   f"Spread {spread_bps} bps exceeds the {MAX_ELIGIBLE_SPREAD_BPS} bps policy",
                                   limits, ticker, depth, maker, taker, total_depth, volume_mxn, movement,
                                   spread_bps, score, age)
        if total_depth < MIN_REQUIRED_DEPTH_MXN:
            return self._candidate(book, INELIGIBLE_DEPTH,
                                   f"Top-of-book depth {total_depth} MXN below the {MIN_REQUIRED_DEPTH_MXN} MXN floor",
                                   limits, ticker, depth, maker, taker, total_depth, volume_mxn, movement,
                                   spread_bps, score, age)
        return self._candidate(book, ELIGIBLE, "Tradable within the current 50/25/11 MXN envelope",
                               limits, ticker, depth, maker, taker, total_depth, volume_mxn, movement,
                               spread_bps, score, age)

    def _base(self, book: str) -> dict[str, Any]:
        return {"book": book, "rank": 0, "movement_bps": None, "volatility_bps": None,
                "high_low_range_bps": None, "best_bid_mxn": None, "best_ask_mxn": None,
                "spread_bps": None, "depth_mxn": None, "volume_mxn": None,
                "minimum_order_mxn": None, "maker_fee": None, "taker_fee": None,
                "estimated_round_trip_friction_mxn": None, "estimated_round_trip_friction_bps": None,
                "data_quality": "INVALID", "cap_executable": False,
                "score": "0", "score_version": SCORE_VERSION, "components": {},
                "strategy_compatibility": strategy_compatibility(book), "lifecycle": "RESEARCH_ONLY",
                "shadow_evaluations": 0, "shadow_signals": 0, "shadow_net_pnl_mxn": None,
                "evidence_count": 0}

    def _rejected(self, book: str, status: str, reason: str, limits: Any) -> MarketCandidate:
        base = self._base(book)
        base["minimum_order_mxn"] = str(getattr(limits, "minimum_value", "")) or None
        base["data_fingerprint"] = fingerprint({"book": book, "status": status, "reason": reason})
        return MarketCandidate(status=status, reason=reason, **base)

    def _candidate(self, book: str, status: str, reason: str, limits: Any, ticker: Any, depth: Any,
                   maker: Decimal | None, taker: Decimal | None, total_depth: Decimal, volume_mxn: Decimal,
                   movement: Decimal | None, spread_bps: Decimal, score: Any, age: Decimal) -> MarketCandidate:
        base = self._base(book)
        base.update({
            "movement_bps": None if movement is None else str(movement),
            "volatility_bps": None if movement is None else str(movement),
            "high_low_range_bps": None if movement is None else str(movement),
            "best_bid_mxn": str(ticker.bid), "best_ask_mxn": str(ticker.ask),
            "spread_bps": str(spread_bps), "depth_mxn": str(total_depth),
            "volume_mxn": str(volume_mxn), "minimum_order_mxn": str(limits.minimum_value),
            "maker_fee": None if maker is None else str(maker),
            "taker_fee": None if taker is None else str(taker),
            "estimated_round_trip_friction_mxn": (None if taker is None else str(score.friction.round_trip_mxn)),
            "estimated_round_trip_friction_bps": (None if taker is None else str(score.friction.round_trip_bps)),
            "data_quality": "VALID",
            "cap_executable": taker is not None and limits.minimum_value <= self.cap_mxn,
            "score": str(score.score), "components": dict(score.components),
        })
        base["lifecycle"] = "SHADOW_CANDIDATE" if status == ELIGIBLE else "REJECTED"
        base["data_fingerprint"] = fingerprint({
            "book": book, "bid": str(ticker.bid), "ask": str(ticker.ask),
            "sequence": depth.sequence, "score_version": SCORE_VERSION, "score": str(score.score),
            "minimum_value": str(limits.minimum_value), "maker": None if maker is None else str(maker),
            "taker": None if taker is None else str(taker)})
        return MarketCandidate(status=status, reason=reason, **base)

    def _account_fees(self, now: datetime) -> dict[str, Any]:
        """Refresh one account fee snapshot per scanner cadence, never per book."""
        if (self._fee_cache_at is not None
                and (now - self._fee_cache_at).total_seconds() < self.interval_seconds):
            return dict(self._fee_cache)
        assert self._source is not None
        schedules = self._source.fee_schedules()
        mapped = {str(row.book): row for row in schedules}
        self._fee_cache, self._fee_cache_at = mapped, now
        self.last_fee_refresh_at = now
        return dict(mapped)

    # ------------------------------------------------------------------ scan
    def scan(self, *, now: datetime | None = None, telemetry: Any = None) -> dict[str, Any]:
        """Run one bounded, deterministic scan. Failures degrade the scanner only."""
        now = now or datetime.now(UTC)
        with self._lock:
            emit = (lambda *args, **kwargs: None) if telemetry is None else telemetry
            emit("MARKET_SCAN_STARTED", component="scanner", message="MXN universe discovery started")
            try:
                available = self._source.available_books()  # type: ignore[union-attr]
                books = tuple(sorted(str(row.book) for row in available if str(row.book).endswith("_mxn")))
                limits = {str(book.book): book for book in available}
                self.universe_size = len(books)
                fee_error = False
                try:
                    fee_map = self._account_fees(now)
                except Exception:
                    fee_map, fee_error = {}, True
                candidates = [self._evaluate_market(book, limits[book], now, fee_map) for book in books]
            except Exception as exc:
                # Scanner failure is contained: research never halts Production.
                self.degraded = True
                self.last_error = str(exc.args[0]) if exc.args else "MARKET_SCANNER_UNAVAILABLE"
                emit("MARKET_SCANNER_DEGRADED", component="scanner", level="WARNING",
                     message=self.last_error)
                return self.evidence()
            missing_fees = any(book not in fee_map for book in books)
            self.degraded = fee_error or missing_fees
            self.last_error = ("ACCOUNT_FEE_SOURCE_UNAVAILABLE" if fee_error else
                               "ACCOUNT_FEE_DATA_INCOMPLETE" if missing_fees else None)
            self.fee_source = "UNAVAILABLE" if fee_error else "ACCOUNT CONFIRMED"
            # Deterministic ranking: score desc, then book name asc for stability.
            ranked = sorted(candidates, key=lambda item: (-Decimal(item.score), item.book))
            ordered: list[MarketCandidate] = []
            rank = 0
            for candidate in ranked:
                if candidate.status == ELIGIBLE:
                    rank += 1
                    candidate = MarketCandidate(**{**candidate.public(), "rank": rank})
                    emit("MARKET_CANDIDATE_ELIGIBLE", component="scanner", message=candidate.book,
                         market=candidate.book, score=candidate.score,
                         score_version=candidate.score_version, component_scores=candidate.components,
                         eligibility=candidate.status, reason=candidate.reason,
                         data_fingerprint=candidate.data_fingerprint)
                elif candidate.lifecycle == "REJECTED":
                    emit("MARKET_CANDIDATE_REJECTED", component="scanner", level="INFO",
                         message=candidate.reason, market=candidate.book, score=candidate.score,
                         score_version=candidate.score_version, component_scores=candidate.components,
                         eligibility=candidate.status, reason=candidate.status,
                         data_fingerprint=candidate.data_fingerprint)
                ordered.append(candidate)
            self.candidates, self.scanned_at = ordered, now
            self._run_shadow(emit)
            emit("MARKET_SCAN_COMPLETED", component="scanner", message="Scan completed",
                 universe=self.universe_size,
                 eligible=sum(1 for item in ordered if item.status == ELIGIBLE),
                 rejected=sum(1 for item in ordered if item.status != ELIGIBLE),
                 score_version=SCORE_VERSION)
            return self.evidence()

    def _run_shadow(self, emit: Any) -> None:
        """Record shadow-evaluation intent for top eligible candidates.

        No candidate market can reach the Production ExecutionEngine: this records
        research evidence only and never creates an order intent.

        Intent alone is not evidence, so each candidate is seeded with the real
        series availability for its market. A market with no captured candles is
        recorded as awaiting data rather than appearing as an evaluated zero, and a
        market whose series was supplied is evaluated by `shadow_research` through
        `record_shadow_evaluation`.
        """
        top = [item for item in self.candidates if item.status == ELIGIBLE][:self.max_shadow_candidates]
        for candidate in top:
            emit("MARKET_SHADOW_STARTED", component="scanner", message=candidate.book,
                 market=candidate.book, score=candidate.score,
                 strategy_compatibility=candidate.strategy_compatibility)
        self.shadow = {
            item.book: {"market": item.book, "candles": 0, "evaluations": 0, "signals": 0,
                        "entries": 0, "exits": 0, "shadow_trades": 0, "gross_pnl_mxn": "0",
                        "net_pnl_mxn": "0", "fees_mxn": "0", "slippage_mxn": "0",
                        "max_drawdown_mxn": "0", "estimated_fees_mxn": "0",
                        "estimated_slippage_mxn": "0", "signal_rate": "0",
                        "economic_reject_rate": "0", "data_source": "AWAITING_CANDLE_DATA",
                        "evaluated": False,
                        "strategy_compatibility": item.strategy_compatibility,
                        "lifecycle": "RESEARCH_ONLY",
                        "evidence_count": 0} for item in top}

    def record_shadow_evaluation(self, market: str, *, candles: int, evaluations: int,
                                 signals: int, shadow_trades: int = 0, gross_pnl_mxn: str = "0",
                                 fees_mxn: str = "0", slippage_mxn: str = "0",
                                 net_pnl_mxn: str = "0", max_drawdown_mxn: str = "0",
                                 economic_reject_rate: str = "0", data_source: str = "CAPTURED",
                                 profiles: list[dict[str, Any]] | None = None,
                                 telemetry: Any = None) -> dict[str, Any]:
        """Record a real shadow evaluation result for one research market.

        This is how `shadow_evaluations` becomes a measurement instead of a zero.
        Only genuine evaluations are recorded: the caller supplies counts produced
        by actually replaying a candle series, so a market with no data is never
        counted here.
        """
        with self._lock:
            row = self.shadow.setdefault(market, {
                "market": market, "strategy_compatibility": strategy_compatibility(market),
                "lifecycle": "RESEARCH_ONLY", "evidence_count": 0})
            row.update({
                "candles": candles, "evaluations": evaluations, "signals": signals,
                "entries": shadow_trades, "exits": shadow_trades,
                "shadow_trades": shadow_trades, "gross_pnl_mxn": gross_pnl_mxn,
                "fees_mxn": fees_mxn, "slippage_mxn": slippage_mxn,
                "estimated_fees_mxn": fees_mxn, "estimated_slippage_mxn": slippage_mxn,
                "net_pnl_mxn": net_pnl_mxn, "max_drawdown_mxn": max_drawdown_mxn,
                "economic_reject_rate": economic_reject_rate,
                "signal_rate": (str(Decimal(signals) / Decimal(evaluations))
                                if evaluations else "0"),
                "data_source": data_source, "evaluated": True,
                "profiles": list(profiles or []),
                "evidence_count": int(row.get("evidence_count", 0)) + 1})
            if telemetry is not None:
                telemetry("MARKET_SHADOW_EVALUATED", component="scanner", message=market,
                          market=market, candles=candles, evaluations=evaluations,
                          signals=signals, shadow_trades=shadow_trades,
                          gross_pnl_mxn=gross_pnl_mxn, net_pnl_mxn=net_pnl_mxn,
                          economic_reject_rate=economic_reject_rate,
                          data_source=data_source,
                          strategy_compatibility=row["strategy_compatibility"],
                          profiles=list(profiles or []))
            return dict(row)

    def observe_shadow(self, book: str, *, candles: int, evaluations: int, signals: int,
                       net_pnl_mxn: str = "0", telemetry: Any = None) -> dict[str, Any]:
        """Record shadow evaluation progress for one research candidate.

        Retained for existing callers; delegates to `record_shadow_evaluation` so
        there is one code path that can mark a market as genuinely evaluated.
        """
        return self.record_shadow_evaluation(
            book, candles=candles, evaluations=evaluations, signals=signals,
            net_pnl_mxn=net_pnl_mxn, telemetry=telemetry)


    # ---------------------------------------------------------------- output
    def evidence(self) -> dict[str, Any]:
        with self._lock:
            return {"scanner_ran": self.scanned_at is not None, "degraded": self.degraded,
                    "error": self.last_error, "universe_size": self.universe_size,
                    "status": "DEGRADED" if self.degraded else "HEALTHY",
                    "fee_source": self.fee_source,
                    "last_fee_refresh_at": (self.last_fee_refresh_at.isoformat().replace("+00:00", "Z")
                                            if self.last_fee_refresh_at else None),
                    "books_with_account_fee": sum(1 for item in self.candidates if item.taker_fee is not None),
                    "books_with_market_data": sum(1 for item in self.candidates if item.best_bid_mxn is not None),
                    "scanned_at": self.scanned_at.isoformat().replace("+00:00", "Z") if self.scanned_at else None,
                    "score_version": SCORE_VERSION,
                    "eligible": [item.public() for item in self.candidates if item.status == ELIGIBLE],
                    "rejected": [item.public() for item in self.candidates if item.status != ELIGIBLE],
                    "candidates": [item.public() for item in self.candidates],
                    "shadow": [dict(value) for value in self.shadow.values()],
                    "live_market": LIVE_MARKET, "production_market_rotation": PRODUCTION_MARKET_ROTATION,
                    "market_promotion": MARKET_PROMOTION, "read_only": True,
                    "interval_seconds": self.interval_seconds,
                    "execution_path_to_production": "NOT_PRESENT"}

    def eligible_count(self) -> int:
        with self._lock:
            return sum(1 for item in self.candidates if item.status == ELIGIBLE)

    def scanner_evidence(self) -> dict[str, Any]:
        """Compact form embedded in session reports."""
        with self._lock:
            return {"scanner_ran": self.scanned_at is not None, "degraded": self.degraded,
                    "universe_size": self.universe_size, "score_version": SCORE_VERSION,
                    "eligible_count": self.eligible_count(),
                    "rejected_count": sum(1 for item in self.candidates if item.status != ELIGIBLE),
                    "shadow_evaluations": sum(1 for item in self.shadow.values()
                                              if item.get("evidence_count", 0) > 0),
                    "candidates": [{"market": item.book, "score": item.score, "status": item.status,
                                    "reason": item.reason,
                                    "strategy_compatibility": item.strategy_compatibility}
                                   for item in self.candidates]}

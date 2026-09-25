"""Shadow research pipeline: discovered markets -> real deterministic evaluation.

This closes the MVP 0.1.2 gap directly. Previously the scanner emitted
``MARKET_SHADOW_STARTED`` for its top candidates and then reported
``shadow_evaluations = 0``: shadow research was *announced* but nothing was ever
evaluated. Here the pipeline is connected end to end:

    market discovered (eligible)
      -> available candles for that market
      -> compatible research profile
      -> strategy decisions
      -> EconomicEdgeGuard assessment
      -> simulated BUY / position / SELL in a private shadow wallet
      -> actual account fee schedule + observed spread + depth-walked slippage
      -> net P&L
      -> SHADOW_EVALUATION

Safety properties, all structural rather than by convention:

- **No POST.** This module never holds an exchange write transport. It reads candle
  series and calls the scanner's own evidence recorder.
- **Shadow is separate from Production.** Simulated fills go to a private
  ``Wallet`` inside the replay evaluator. Nothing here can reach the ledger,
  financial truth, or the real position.
- **No fabricated evidence.** A market with no usable candles is reported as
  ``NO_CANDLE_DATA`` with zero evaluations. It is never given a synthetic series to
  make the pipeline look busy, because that would turn "we evaluated it" into a
  false statement. Only markets with real captured candles produce real numbers.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from autofund.decimal_utils import financial

from .economics import DEFAULT_POLICY, EconomicPolicy
from .historical import CandleSeries
from .profile_library import PROFILE_REGISTRY
from .profiles import (
    COMPATIBLE,
    INSUFFICIENT_EVIDENCE,
    RESEARCH_ONLY,
    market_compatibility,
)
from .replay_eval import verify_no_lookahead
from .research import (
    DEFAULT_REQUIREMENTS,
    EvidenceRequirements,
    ProfileMarketEvidence,
    evaluate_profile_market,
)

SHADOW_PIPELINE_VERSION = "autofund.shadow-research.v1"

NO_CANDLE_DATA = "NO_CANDLE_DATA"
NO_ELIGIBLE_MARKET = "NO_ELIGIBLE_MARKET"
EVALUATED = "EVALUATED"

# Reasons a market cannot be shadow-evaluated. Reported per market so an absence of
# evidence is always explained rather than appearing as a silent zero.
EXCLUDED_FIAT_LIKE = "MARKET_CLASS_STABLE_OR_FIAT_EXCLUDED_FROM_VOLATILE_RESEARCH"
EXCLUDED_NO_CANDLES = "NO_CAPTURED_CANDLE_DATA_FOR_MARKET"
EXCLUDED_NO_BOOK = "NO_OBSERVED_ORDER_BOOK_FOR_MARKET"


class SeriesProvider(Protocol):
    """Supplies candle series per market. Read-only by construction."""

    def series_for(self, market: str) -> CandleSeries | None: ...


@dataclass(frozen=True, slots=True)
class MarketBook:
    """Observed depth for one market, used to walk real execution prices.

    Book data is required rather than optional: without it, slippage cannot be
    estimated and every order would be refused for insufficient depth, which would
    make the shadow pipeline silently inert. A market with no observed book is
    therefore excluded with a reason, not evaluated against an empty one.
    """

    bids: tuple[Any, ...] = ()
    asks: tuple[Any, ...] = ()

    @property
    def usable(self) -> bool:
        return bool(self.bids) and bool(self.asks)


@dataclass(frozen=True, slots=True)
class MarketShadowResult:
    """Shadow research outcome for one market, with its exclusion reason if any."""

    market: str
    status: str
    reason: str
    evaluations: int
    signals: int
    shadow_trades: int
    gross_pnl_mxn: Decimal
    fees_mxn: Decimal
    slippage_mxn: Decimal
    net_pnl_mxn: Decimal
    max_drawdown_mxn: Decimal
    economic_reject_rate: Decimal
    data_source: str
    evidence: tuple[ProfileMarketEvidence, ...] = ()

    def telemetry(self) -> dict[str, Any]:
        return {"version": SHADOW_PIPELINE_VERSION, "market": self.market,
                "status": self.status, "reason": self.reason,
                "evaluations": self.evaluations, "signals": self.signals,
                "shadow_trades": self.shadow_trades,
                "gross_pnl_mxn": str(self.gross_pnl_mxn), "fees_mxn": str(self.fees_mxn),
                "slippage_mxn": str(self.slippage_mxn), "net_pnl_mxn": str(self.net_pnl_mxn),
                "max_drawdown_mxn": str(self.max_drawdown_mxn),
                "economic_reject_rate": str(self.economic_reject_rate),
                "data_source": self.data_source,
                "profiles": [item.public() for item in self.evidence],
                "production_orders": 0, "shadow_is_separate_from_production": True}


@dataclass(frozen=True, slots=True)
class ShadowRun:
    """A complete shadow research pass over the discovered eligible universe."""

    markets: tuple[MarketShadowResult, ...]
    excluded: tuple[dict[str, str], ...]

    @property
    def evaluated_markets(self) -> int:
        return sum(1 for item in self.markets if item.status == EVALUATED)

    @property
    def total_evaluations(self) -> int:
        return sum(item.evaluations for item in self.markets)

    @property
    def total_shadow_trades(self) -> int:
        return sum(item.shadow_trades for item in self.markets)

    @property
    def total_net_pnl_mxn(self) -> Decimal:
        return sum((item.net_pnl_mxn for item in self.markets), Decimal("0"))

    def telemetry(self) -> dict[str, Any]:
        return {"version": SHADOW_PIPELINE_VERSION,
                "markets_discovered": len(self.markets) + len(self.excluded),
                "markets_evaluated": self.evaluated_markets,
                "evaluations": self.total_evaluations,
                "shadow_trades": self.total_shadow_trades,
                "net_pnl_mxn": str(self.total_net_pnl_mxn),
                "results": [item.telemetry() for item in self.markets],
                "excluded": [dict(item) for item in self.excluded],
                "production_orders": 0, "promotion": "DISABLED"}


def _is_volatile_research_market(market: str) -> bool:
    """Whether a market belongs in volatile-crypto strategy research.

    A stablecoin or fiat-like book is excluded by name. It is not "not volatile
    enough"; it is a different instrument class whose price is not the phenomenon
    these profiles model, and saying so explicitly prevents a fiat pair from ever
    being selected as the most attractive research target.
    """
    from .profiles import CLASS_STABLE_OR_FIAT, classify_market

    return classify_market(market) != CLASS_STABLE_OR_FIAT


@financial
def run_shadow_research(
    *,
    eligible_markets: tuple[str, ...],
    provider: SeriesProvider,
    taker_fee_rate: Decimal,
    spread_bps: Decimal,
    books: dict[str, MarketBook] | None = None,
    budget_mxn: Decimal = Decimal("11"),
    policy: EconomicPolicy | None = None,
    requirements: EvidenceRequirements = DEFAULT_REQUIREMENTS,
    profile_ids: tuple[str, ...] | None = None,
) -> ShadowRun:
    """Evaluate every compatible profile on every eligible market with candle data.

    Returns real counts. A market without captured candles, or without an observed
    order book, yields zero evaluations and an explicit reason, which is the honest
    outcome and is what makes ``shadow_evaluations`` meaningful rather than merely
    non-zero.
    """
    active_policy = policy if policy is not None else DEFAULT_POLICY
    observed_books = books or {}
    wanted = profile_ids if profile_ids is not None else tuple(p.profile_id
                                                              for p in PROFILE_REGISTRY)
    results: list[MarketShadowResult] = []
    excluded: list[dict[str, str]] = []

    for market in sorted(eligible_markets):
        if not _is_volatile_research_market(market):
            excluded.append({"market": market, "reason": EXCLUDED_FIAT_LIKE})
            continue
        series = provider.series_for(market)
        if series is None or not series.candles:
            excluded.append({"market": market, "reason": EXCLUDED_NO_CANDLES})
            results.append(MarketShadowResult(
                market=market, status=NO_CANDLE_DATA, reason=EXCLUDED_NO_CANDLES,
                evaluations=0, signals=0, shadow_trades=0, gross_pnl_mxn=Decimal("0"),
                fees_mxn=Decimal("0"), slippage_mxn=Decimal("0"), net_pnl_mxn=Decimal("0"),
                max_drawdown_mxn=Decimal("0"), economic_reject_rate=Decimal("0"),
                data_source="NONE"))
            continue
        book = observed_books.get(market)
        if book is None or not book.usable:
            excluded.append({"market": market, "reason": EXCLUDED_NO_BOOK})
            results.append(MarketShadowResult(
                market=market, status=NO_CANDLE_DATA, reason=EXCLUDED_NO_BOOK,
                evaluations=0, signals=0, shadow_trades=0, gross_pnl_mxn=Decimal("0"),
                fees_mxn=Decimal("0"), slippage_mxn=Decimal("0"), net_pnl_mxn=Decimal("0"),
                max_drawdown_mxn=Decimal("0"), economic_reject_rate=Decimal("0"),
                data_source=series.source))
            continue

        evidence: list[ProfileMarketEvidence] = []
        for profile_id in wanted:
            item = evaluate_profile_market(
                market=market, profile_id=profile_id, series=series,
                taker_fee_rate=taker_fee_rate, spread_bps=spread_bps,
                bids=book.bids, asks=book.asks, budget_mxn=budget_mxn,
                policy=active_policy, requirements=requirements)
            evidence.append(item)

        evaluations = sum(item.replay.evaluations for item in evidence)
        signals = sum(item.replay.buy_signals for item in evidence)
        trades = sum(len(item.replay.round_trips) for item in evidence)
        gross = sum((item.replay.gross_pnl_mxn for item in evidence), Decimal("0"))
        fees = sum((item.replay.fees_mxn for item in evidence), Decimal("0"))
        slippage = sum((trip.slippage_cost_mxn for item in evidence
                        for trip in item.replay.round_trips), Decimal("0"))
        net = sum((item.replay.net_pnl_mxn for item in evidence), Decimal("0"))
        drawdown = max((item.replay.max_drawdown_mxn for item in evidence), default=Decimal("0"))
        considered = sum(item.replay.economic_admissions + item.replay.economic_rejections
                         for item in evidence)
        rejected = sum(item.replay.economic_rejections for item in evidence)
        reject_rate = (Decimal(rejected) / Decimal(considered)) if considered else Decimal("0")

        results.append(MarketShadowResult(
            market=market, status=EVALUATED, reason=EVALUATED, evaluations=evaluations,
            signals=signals, shadow_trades=trades, gross_pnl_mxn=gross, fees_mxn=fees,
            slippage_mxn=slippage, net_pnl_mxn=net, max_drawdown_mxn=drawdown,
            economic_reject_rate=reject_rate, data_source=series.source,
            evidence=tuple(evidence)))

    if not results:
        results.append(MarketShadowResult(
            market="", status=NO_ELIGIBLE_MARKET, reason=NO_ELIGIBLE_MARKET, evaluations=0,
            signals=0, shadow_trades=0, gross_pnl_mxn=Decimal("0"), fees_mxn=Decimal("0"),
            slippage_mxn=Decimal("0"), net_pnl_mxn=Decimal("0"),
            max_drawdown_mxn=Decimal("0"), economic_reject_rate=Decimal("0"),
            data_source="NONE"))
    return ShadowRun(markets=tuple(results), excluded=tuple(excluded))


def shadow_evidence_payload(run: ShadowRun) -> dict[str, Any]:
    """Payload the scanner records, so its own shadow fields stop being zeros."""
    return run.telemetry()


def profile_certification_notes(*, series: CandleSeries, profile_id: str,
                               market: str) -> tuple[bool, str]:
    """Lookahead re-verification, exposed for reporting alongside shadow results."""
    from .profile_library import evaluator_for

    return verify_no_lookahead(candles=series.candles, evaluator=evaluator_for(profile_id),
                               market=market)


def compatibility_of(*, profile_id: str, market: str) -> str:
    """Compatibility of one profile with one market, for reporting."""
    from .profile_library import PROFILE_BY_ID

    definition = PROFILE_BY_ID[profile_id]
    return market_compatibility(definition.markets, market)


__all__ = [
    "COMPATIBLE",
    "EVALUATED",
    "EXCLUDED_FIAT_LIKE",
    "EXCLUDED_NO_BOOK",
    "EXCLUDED_NO_CANDLES",
    "INSUFFICIENT_EVIDENCE",
    "NO_CANDLE_DATA",
    "NO_ELIGIBLE_MARKET",
    "RESEARCH_ONLY",
    "SHADOW_PIPELINE_VERSION",
    "MarketBook",
    "MarketShadowResult",
    "SeriesProvider",
    "ShadowRun",
    "compatibility_of",
    "market_compatibility",
    "profile_certification_notes",
    "run_shadow_research",
    "shadow_evidence_payload",
]

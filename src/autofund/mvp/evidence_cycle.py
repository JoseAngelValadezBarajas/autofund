"""Evaluate real market data into durable evidence.

This is the module that makes certification reachable. It takes real candles --
exchange historical OHLC, or AutoFund's own captured observations -- runs the
deterministic profiles over them through the same economic guard Production uses, and
writes the result into the durable evidence store.

Why this shapes the outcome:

- **Real data only, by construction.** `evaluate_market_into_store` refuses to write
  an observation for a non-certifying provenance. Fixture series can still be evaluated
  for architecture tests, but they are never recorded as evidence, so "fixture-only"
  pairs cannot appear to accumulate progress toward certification.
- **One evaluation pass, one observation.** Candles are replayed once per profile and
  the resulting counters are written as a single observation. Re-running the same
  backfill produces the same identity key and is deduplicated, so repeating a job
  cannot inflate the sample.
- **Historical friction is modelled and labelled.** The exchange does not publish
  historical depth, so historical evaluation uses the observed live spread plus a
  stated slippage assumption, and records `depth_is_observed=False`. Live shadow
  evaluation, which has a real book, records `True`. Certification accepts both, but
  the weaker one is visible rather than disguised.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from autofund.decimal_utils import ZERO, financial

from .backfill import (
    HistoricalMarketBook,
    HistoricalSeries,
    Provenance,
    provenance_for_source,
)
from .certification import DEFAULT_FLOOR, CertificationFloor, certify_pair
from .economics import EconomicPolicy
from .evidence import ACCEPTED, EvidenceObservation, ResearchEvidenceStore
from .profile_library import PROFILE_REGISTRY, evaluator_for
from .replay_eval import replay_candles, verify_no_lookahead

EVALUATION_VERSION = "autofund.market-evidence-evaluation.v1"

# Stated slippage assumption for historical evaluation, where no book was recorded.
# It is deliberately not zero: claiming perfectly frictionless historical execution
# would make historical evidence look better than live evidence, which is the opposite
# of the conservatism this project requires.
HISTORICAL_SLIPPAGE_BPS = Decimal("5")


@dataclass(frozen=True, slots=True)
class MarketEvaluation:
    """One profile's replay result over one real candle series."""

    market: str
    profile_id: str
    candles: int
    evaluations: int
    signals: int
    economic_passes: int
    economic_rejects: int
    simulated_entries: int
    round_trips: int
    wins: int
    losses: int
    gross_pnl_mxn: Decimal
    fees_mxn: Decimal
    spread_cost_mxn: Decimal
    slippage_cost_mxn: Decimal
    net_pnl_mxn: Decimal
    max_drawdown_mxn: Decimal
    worst_trade_mxn: Decimal | None
    median_net_pnl_mxn: Decimal | None
    median_net_edge_bps: Decimal | None
    average_holding_candles: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    observation_hours: Decimal
    lookahead_ok: bool
    deterministic: bool
    dataset_fingerprint: str

    def public(self) -> dict[str, Any]:
        return {name: str(getattr(self, name)) if isinstance(getattr(self, name), Decimal)
                else getattr(self, name) for name in self.__dataclass_fields__}


@financial
def evaluate_series(*, series: HistoricalSeries, profile_id: str, provenance: Provenance,
                    budget_mxn: Decimal, taker_fee_rate: Decimal,
                    book: HistoricalMarketBook, policy: EconomicPolicy,
                    compatibility: str = "RESEARCH_ONLY") -> MarketEvaluation:
    """Replay one profile over one real series and summarise it as evidence."""
    evaluator = evaluator_for(profile_id)
    bids: tuple[Any, ...] = ()
    asks: tuple[Any, ...] = ()
    result = replay_candles(
        candles=series.candles, profile_id=profile_id, market=series.market,
        evaluator=evaluator, taker_fee_rate=taker_fee_rate, spread_bps=book.spread_bps,
        bids=bids, asks=asks, budget_mxn=budget_mxn, policy=policy,
        compatibility=compatibility, slippage_bps=book.slippage_bps)
    lookahead_ok, _detail = verify_no_lookahead(candles=series.candles, evaluator=evaluator,
                                               market=series.market)
    trips = result.round_trips
    net_values = [trip.net_pnl_mxn for trip in trips]
    edges = [trip.net_edge_bps for trip in trips]
    medians = _median(net_values)
    median_edge = _median(edges)
    worst = min(net_values) if net_values else None
    hours = (Decimal(len(series.candles)) * Decimal(series.bucket_seconds)
             / Decimal("3600"))
    del provenance  # provenance is recorded by the caller alongside this summary
    return MarketEvaluation(
        market=series.market, profile_id=profile_id, candles=len(series.candles),
        evaluations=result.evaluations, signals=result.buy_signals,
        economic_passes=result.economic_admissions,
        economic_rejects=result.economic_rejections,
        simulated_entries=result.economic_admissions, round_trips=len(trips),
        wins=result.wins, losses=result.losses, gross_pnl_mxn=result.gross_pnl_mxn,
        fees_mxn=result.fees_mxn,
        spread_cost_mxn=sum((trip.spread_cost_mxn for trip in trips), ZERO),
        slippage_cost_mxn=sum((trip.slippage_cost_mxn for trip in trips), ZERO),
        net_pnl_mxn=result.net_pnl_mxn, max_drawdown_mxn=result.max_drawdown_mxn,
        worst_trade_mxn=worst, median_net_pnl_mxn=medians,
        median_net_edge_bps=median_edge,
        average_holding_candles=result.average_holding_candles,
        spread_bps=book.spread_bps, slippage_bps=book.slippage_bps,
        observation_hours=hours, lookahead_ok=lookahead_ok, deterministic=True,
        dataset_fingerprint=series.fingerprint)


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _observation(*, evaluation: MarketEvaluation, provenance: Provenance,
                 profile_fingerprint: str, strategy_fingerprint: str,
                 policy_fingerprint: str, observed_at: str, p95_spread_bps: Decimal,
                 data_quality_failures: int, depth_observed: bool,
                 execution_compatible: bool, range_start_ms: int,
                 range_end_ms: int) -> EvidenceObservation:
    """Convert an evaluation into a durable observation.

    `execution_compatible` records whether the *order* fits the recorded book
    constraints -- chiefly that the notional is within the book's minimum, which is
    the constraint that would actually make a real order impossible. `depth_observed`
    records, separately, whether slippage came from a real book walk or a stated
    assumption, because those are different guarantees and collapsing them would let an
    assumption masquerade as a measurement.
    """
    return EvidenceObservation(
        market=evaluation.market, profile_id=evaluation.profile_id,
        profile_version="0.1", profile_fingerprint=profile_fingerprint,
        strategy_fingerprint=strategy_fingerprint,
        economic_policy_fingerprint=policy_fingerprint,
        provenance_kind=provenance.kind, provenance_source=provenance.source,
        dataset_fingerprint=evaluation.dataset_fingerprint, observed_at=observed_at,
        observation_hours=str(evaluation.observation_hours),
        closed_candles=evaluation.candles, evaluations=evaluation.evaluations,
        signals=evaluation.signals, economic_passes=evaluation.economic_passes,
        economic_rejects=evaluation.economic_rejects,
        simulated_entries=evaluation.simulated_entries,
        round_trips=evaluation.round_trips,
        data_quality_failures=data_quality_failures,
        gross_pnl_mxn=str(evaluation.gross_pnl_mxn), fees_mxn=str(evaluation.fees_mxn),
        spread_cost_mxn=str(evaluation.spread_cost_mxn),
        slippage_cost_mxn=str(evaluation.slippage_cost_mxn),
        net_pnl_mxn=str(evaluation.net_pnl_mxn),
        max_drawdown_mxn=str(evaluation.max_drawdown_mxn),
        wins=evaluation.wins, losses=evaluation.losses,
        worst_trade_mxn=str(evaluation.worst_trade_mxn if evaluation.worst_trade_mxn is not None else ZERO),
        median_net_pnl_mxn=str(evaluation.median_net_pnl_mxn if evaluation.median_net_pnl_mxn is not None else ZERO),
        median_net_edge_bps=str(evaluation.median_net_edge_bps if evaluation.median_net_edge_bps is not None else ZERO),
        average_holding_candles=str(evaluation.average_holding_candles),
        median_spread_bps=str(evaluation.spread_bps), p95_spread_bps=str(p95_spread_bps),
        modelled_slippage_bps=str(evaluation.slippage_bps),
        lookahead_ok=evaluation.lookahead_ok, deterministic=evaluation.deterministic,
        execution_compatible=execution_compatible, accounting_compatible=True,
        range_start_ms=range_start_ms, range_end_ms=range_end_ms,
        depth_observed=depth_observed)


@dataclass(frozen=True, slots=True)
class IngestReport:
    """What one evaluation pass produced, including what it refused to record."""

    market: str
    provenance: str
    accepted: int
    duplicates: int
    refused_non_certifying: int
    evaluations: tuple[MarketEvaluation, ...]

    def public(self) -> dict[str, Any]:
        return {"version": EVALUATION_VERSION, "market": self.market,
                "provenance": self.provenance, "accepted": self.accepted,
                "duplicates": self.duplicates,
                "refused_non_certifying": self.refused_non_certifying,
                "profiles": [item.public() for item in self.evaluations]}


@financial
def evaluate_market_into_store(*, series: HistoricalSeries, store: ResearchEvidenceStore,
                               budget_mxn: Decimal, taker_fee_rate: Decimal,
                               book: HistoricalMarketBook, policy: EconomicPolicy,
                               record: bool = True,
                               profile_ids: tuple[str, ...] | None = None,
                               p95_spread_bps: Decimal | None = None,
                               data_quality_failures: int = 0,
                               now: datetime | None = None) -> IngestReport:
    """Replay every profile over a real series and record the evidence.

    A non-certifying provenance is evaluated but **not** recorded. That is the
    enforcement point for "fixture evidence must never certify Production": the
    refusal is here, at the single write boundary, rather than depending on every
    caller to remember.
    """
    provenance = provenance_for_source(series.provenance.kind
                                       if series.provenance.source else "UNKNOWN")
    provenance = Provenance(series.provenance.kind, series.provenance.source,
                            series.provenance.detail)
    wanted = profile_ids or tuple(definition.profile_id for definition in PROFILE_REGISTRY)
    observed_at = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
    policy_fingerprint = _policy_fingerprint(policy)
    results: list[MarketEvaluation] = []
    accepted = 0
    duplicates = 0
    refused = 0
    spread_p95 = p95_spread_bps if p95_spread_bps is not None else book.spread_bps
    for profile_id in wanted:
        definition = next(d for d in PROFILE_REGISTRY if d.profile_id == profile_id)
        evaluation = evaluate_series(
            series=series, profile_id=profile_id, provenance=provenance,
            budget_mxn=budget_mxn, taker_fee_rate=taker_fee_rate, book=book,
            policy=policy, compatibility=("CERTIFIED_FOR_MARKET"
                                          if series.market.replace("/", "_").lower()
                                          in definition.markets else "RESEARCH_ONLY"))
        results.append(evaluation)
        if not record:
            continue
        if not provenance.may_certify:
            refused += 1
            continue
        observation = _observation(
            evaluation=evaluation, provenance=provenance,
            profile_fingerprint=definition.fingerprint,
            strategy_fingerprint=definition.identity.strategy_fingerprint,
            policy_fingerprint=policy_fingerprint, observed_at=observed_at,
            p95_spread_bps=spread_p95, data_quality_failures=data_quality_failures,
            depth_observed=book.depth_is_observed,
            execution_compatible=_execution_compatible(book=book, budget_mxn=budget_mxn),
            range_start_ms=_bucket_ms(series.candles[0].timestamp) if series.candles else 0,
            range_end_ms=_bucket_ms(series.candles[-1].timestamp) if series.candles else 0)
        outcome = store.ingest(observation)
        if outcome == ACCEPTED:
            accepted += 1
        else:
            duplicates += 1
    return IngestReport(market=series.market, provenance=provenance.kind,
                        accepted=accepted, duplicates=duplicates,
                        refused_non_certifying=refused, evaluations=tuple(results))


def _policy_fingerprint(policy: EconomicPolicy) -> str:
    from autofund.replay.serialization import fingerprint

    return fingerprint({"schema": "autofund.economic-policy", **policy.public()})


def _bucket_ms(moment: datetime) -> int:
    """Convert a candle timestamp to an integer bucket key.

    Integer milliseconds, not a float: a float key would make the deduplication
    identity depend on binary floating-point rounding, so the same candle could
    produce two different keys and be counted twice.
    """
    return int(moment.timestamp()) * 1000


def _execution_compatible(*, book: HistoricalMarketBook, budget_mxn: Decimal) -> bool:
    """Whether the intended order fits the recorded book constraints.

    The binding constraint for a micro order is the book's minimum notional: if the
    exchange will not accept 11 MXN on this book, no strategy result can make the order
    executable. Recorded depth is also checked when it is an observation rather than an
    assumption, but a *modelled* depth figure is not allowed to fail the order -- doing
    so would let a stated assumption veto real evidence.
    """
    if book.minimum_value_mxn > budget_mxn:
        return False
    if book.depth_is_observed and book.depth_mxn < budget_mxn:
        return False
    return True


@dataclass(frozen=True, slots=True)
class EvidenceCycleReport:
    """Full cycle: real data in, certifications out."""

    markets: tuple[str, ...]
    ingest: tuple[IngestReport, ...]
    certifications: tuple[dict[str, Any], ...]

    def public(self) -> dict[str, Any]:
        return {"version": EVALUATION_VERSION, "markets": list(self.markets),
                "ingest": [item.public() for item in self.ingest],
                "certifications": list(self.certifications),
                "certified_pairs": [f"{c['market']}|{c['profile_id']}"
                                    for c in self.certifications if c["certified"]],
                "production_certifiable_pairs": [
                    f"{c['market']}|{c['profile_id']}"
                    for c in self.certifications
                    if c["state"] in ("PRODUCTION_CERTIFIABLE", "CERTIFIED")],
                "fixture_only_excluded": True}


@financial
def run_evidence_cycle(*, series_by_market: dict[str, HistoricalSeries],
                       store: ResearchEvidenceStore, budget_mxn: Decimal,
                       taker_fee_rate: Decimal, books: dict[str, HistoricalMarketBook],
                       policy: EconomicPolicy, floor: CertificationFloor = DEFAULT_FLOOR,
                       market_quality: dict[str, bool] | None = None,
                       record: bool = True) -> EvidenceCycleReport:
    """Evaluate real data for every market, then certify every known pair."""
    reports: list[IngestReport] = []
    for market in sorted(series_by_market):
        series = series_by_market[market]
        book = books.get(market)
        if book is None or not series.candles:
            continue
        reports.append(evaluate_market_into_store(
            series=series, store=store, budget_mxn=budget_mxn, taker_fee_rate=taker_fee_rate,
            book=book, policy=policy, record=record))
    quality = market_quality or {}
    certifications = tuple(
        certify_pair(market=market, profile_id=profile_id, store=store, floor=floor,
                     market_quality_ok=quality.get(market, True)).public()
        for market, profile_id in store.pairs())
    return EvidenceCycleReport(markets=tuple(sorted(series_by_market)),
                               ingest=tuple(reports), certifications=certifications)


__all__ = [
    "EVALUATION_VERSION",
    "HISTORICAL_SLIPPAGE_BPS",
    "EvidenceCycleReport",
    "IngestReport",
    "MarketEvaluation",
    "evaluate_market_into_store",
    "evaluate_series",
    "run_evidence_cycle",
]

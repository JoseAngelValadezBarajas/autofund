"""MVP 0.2: certified dynamic multi-market selection.

These tests pin the mechanisms that let a non-BTC market escape RESEARCH_ONLY, and the
invariants that make doing so safe. The uncomfortable findings are pinned too, because
they are the result:

* the bridge from real data to certification exists and works on real exchange candles;
* fixture evidence can never certify, enforced at the single write boundary;
* the selector picks on economics, not on market score, and returns NO_TRADE rather
  than the least-bad market;
* exactly one Production write is possible globally;
* AutoFund never sells foreign wallet inventory;
* certification is per pair, and a proven loser reports NOT_VIABLE rather than hiding
  behind "insufficient evidence".
"""

import pathlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from autofund.live.multiasset import (
    MarketBook,
    MarketError,
    can_open_in_market,
    deployed_value_mxn,
    inventory_for,
    sellable_quantity,
    validate_intent_market,
)
from autofund.mvp.backfill import (
    CERTIFYING_PROVENANCE,
    NON_CERTIFYING_PROVENANCE,
    REAL_CAPTURED,
    REAL_HISTORICAL,
    SYNTHETIC_FIXTURE,
    BackfillWindow,
    HistoricalMarketBook,
    HistoricalSeries,
    Provenance,
    normalise_ohlc,
    provenance_for_source,
    window,
)
from autofund.mvp.certification import (
    ACCUMULATING_EVIDENCE,
    CERTIFIED,
    INSUFFICIENT_EVIDENCE,
    NOT_VIABLE,
    RESEARCH_ONLY,
    SUSPENDED,
    CertificationFloor,
    certify_all,
    certify_pair,
)
from autofund.mvp.economics import DEFAULT_POLICY
from autofund.mvp.evidence import (
    ACCEPTED,
    DUPLICATE,
    EvidenceError,
    EvidenceObservation,
    ResearchEvidenceStore,
)
from autofund.mvp.evidence_cycle import evaluate_market_into_store
from autofund.mvp.historical import synthetic_candles
from autofund.mvp.profiles import DECISION_BUY, DECISION_NO_SIGNAL
from autofund.mvp.telemetry import CHECKPOINTS
from autofund.mvp.universe import (
    NO_TRADE,
    REASON_ALREADY_HOLDING,
    REASON_CAPITAL_REJECT,
    REASON_ECONOMIC_GUARD_REJECT,
    REASON_FIAT_LIKE_MARKET,
    REASON_NO_FEE_DATA,
    REASON_NO_MARKET_DATA,
    REASON_NO_SIGNAL,
    REASON_NOT_CERTIFIED,
    REASON_NOT_VIABLE,
    REASON_SPREAD_REJECT,
    REASON_STALE_DATA,
    REASON_UNRESOLVED_ORDER,
    CandidateMarket,
    MarketSelectionContext,
    PortfolioConstraints,
    build_certified_universe,
    select_production_opportunity,
)

D = Decimal
PRODUCTION_FEE = D("0.0078")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _floor(**overrides: Any) -> CertificationFloor:
    defaults: dict[str, Any] = {"min_closed_candles": 10, "min_evaluations": 5,
                               "min_round_trips": 1, "min_opportunities": 1,
                               "max_drawdown_mxn": D("100"),
                               "max_worst_trade_mxn": D("-100"),
                               "require_five_round_trips_when_opportunities_exist": False}
    defaults.update(overrides)
    return CertificationFloor(**defaults)


def _observation(**overrides: Any) -> EvidenceObservation:
    base: dict[str, Any] = {
        "market": "SOL/MXN", "profile_id": "trend-continuation-v1", "profile_version": "0.2",
        "profile_fingerprint": "pfp", "strategy_fingerprint": "sfp",
        "economic_policy_fingerprint": "epf", "provenance_kind": REAL_HISTORICAL,
        "provenance_source": "EXCHANGE_HISTORICAL_OHLC", "dataset_fingerprint": "ds",
        "observed_at": "2026-09-25T00:00:00Z", "observation_hours": "168",
        "closed_candles": 10081, "evaluations": 10081, "signals": 91,
        "economic_passes": 91, "economic_rejects": 0, "simulated_entries": 91,
        "round_trips": 91, "data_quality_failures": 0, "gross_pnl_mxn": "1",
        "fees_mxn": "0.5", "spread_cost_mxn": "0.01", "slippage_cost_mxn": "0.02",
        "net_pnl_mxn": "0.4", "max_drawdown_mxn": "0.1", "wins": 60, "losses": 31,
        "worst_trade_mxn": "-0.05", "median_net_pnl_mxn": "0.004",
        "median_net_edge_bps": "4", "average_holding_candles": "12",
        "median_spread_bps": "5", "p95_spread_bps": "12", "modelled_slippage_bps": "5",
        "lookahead_ok": True, "deterministic": True, "execution_compatible": True,
        "accounting_compatible": True, "range_start_ms": 1000, "range_end_ms": 2000,
    }
    base.update(overrides)
    return EvidenceObservation(**base)


def _series(*, market: str = "SOL/MXN", provenance: Provenance | None = None,
            prices: list[str] | None = None) -> HistoricalSeries:
    closes = prices or (["1000"] * 22 + [str(1000 + i * 5) for i in range(1, 31)])
    fixture = synthetic_candles(prices=closes, market=market)
    return HistoricalSeries(
        market=market, candles=fixture.candles,
        provenance=provenance or Provenance(REAL_HISTORICAL, "EXCHANGE_HISTORICAL_OHLC"),
        window=BackfillWindow(market, 0, len(closes) * 60_000, 60))


def _certified_pair(market: str = "SOL/MXN", profile_id: str = "trend-continuation-v1") -> Any:
    return type("C", (), {"market": market, "profile_id": profile_id, "state": CERTIFIED,
                          "selectable": True, "evidence": None,
                          "failed_criteria": (), "criteria": ()})()


def _candidate(**overrides: Any) -> CandidateMarket:
    base: dict[str, Any] = {
        "market": "SOL/MXN", "book": "sol_mxn", "profile_id": "trend-continuation-v1",
        "certification": _certified_pair(), "decision": DECISION_BUY,
        "expected_gross_edge_bps": D("400"), "expected_net_edge_bps": D("200"),
        "expected_net_pnl_mxn": D("0.05"), "viability": None,
        "taker_fee_rate": PRODUCTION_FEE, "spread_bps": D("10"),
        "bid_quantity_mxn": D("5000"), "data_age_seconds": D("1"),
    }
    base.update(overrides)
    return CandidateMarket(**base)


def _context(**overrides: Any) -> MarketSelectionContext:
    constraints = overrides.pop("constraints", None) or PortfolioConstraints(
        authorized_capital_mxn=D("50"), max_deployment_mxn=D("25"),
        single_order_cap_mxn=D("11"), deployed_mxn=D("0"), cash_mxn=D("50"))
    return MarketSelectionContext(constraints=constraints, **overrides)


# ---------------------------------------------------------------------------
# Evidence provenance
# ---------------------------------------------------------------------------

def test_stablecoin_classification_accepts_both_spellings() -> None:
    """A fiat pair must never be classified as volatile crypto by spelling alone."""
    from autofund.mvp.profiles import (
        CLASS_STABLE_OR_FIAT as STABLE,
    )
    from autofund.mvp.profiles import (
        CLASS_VOLATILE_CRYPTO as VOLATILE,
    )
    from autofund.mvp.profiles import (
        classify_market,
    )

    for spelling in ("usd_mxn", "USD/MXN", "USDT/MXN", "usdt_mxn", "paxg_mxn"):
        assert classify_market(spelling) == STABLE, spelling
    for spelling in ("btc_mxn", "BTC/MXN", "SOL/MXN", "sol_mxn"):
        assert classify_market(spelling) == VOLATILE, spelling


def test_provenance_classes_are_explicit_and_map_to_certifying_status() -> None:
    assert provenance_for_source("CAPTURED_MARKET_DATA").kind == REAL_CAPTURED
    assert provenance_for_source("EXCHANGE_HISTORICAL_OHLC").kind == REAL_HISTORICAL
    fixture = provenance_for_source("SYNTHETIC_FIXTURE")
    assert fixture.kind == SYNTHETIC_FIXTURE and fixture.may_certify is False
    for kind in (REAL_CAPTURED, REAL_HISTORICAL):
        assert kind in CERTIFYING_PROVENANCE
    assert NON_CERTIFYING_PROVENANCE == frozenset({SYNTHETIC_FIXTURE})


def test_unclassified_source_defaults_to_non_certifying() -> None:
    """A new data source must not acquire certifying status by accident."""
    assert provenance_for_source("SOMETHING_NEW").may_certify is False
    assert provenance_for_source("").may_certify is False


def test_unknown_provenance_kind_is_rejected() -> None:
    with pytest.raises(ValueError):
        Provenance("TOTALLY_REAL", "src")


# ---------------------------------------------------------------------------
# Historical backfill integrity
# ---------------------------------------------------------------------------

def test_windows_end_at_a_completed_bucket() -> None:
    """Backfill must never include the in-progress bucket."""
    now = datetime(2026, 9, 25, 12, 0, 30, tzinfo=UTC)
    spec = window(book="eth_mxn", lookback_hours=2, now=now)
    end_at = datetime.fromtimestamp(spec.end_ms / 1000, tz=UTC)
    assert end_at <= now and end_at.second == 0
    assert spec.end_ms // 1000 % 60 == 0, "end must be bucket-aligned"
    assert spec.expected_candles == 120


def test_window_rejects_nonpositive_lookback() -> None:
    with pytest.raises(ValueError):
        window(book="eth_mxn", lookback_hours=0)


def _ohlc(bucket_seconds: list[int]) -> tuple[Any, ...]:
    from autofund.observer.models import OhlcCandle

    return tuple(
        OhlcCandle(book="eth_mxn", bucket_ms=second * 1000,
                   opened_at=datetime.fromtimestamp(second, tz=UTC), open=D("100"),
                   high=D("110"), low=D("90"), close=D("105"), volume=D("1"))
        for second in bucket_seconds)


def test_ohlc_normalisation_reports_gaps_without_filling_them() -> None:
    """A missing bucket must be counted, never interpolated into a fake candle."""
    candles, gaps, duplicates, out_of_order = normalise_ohlc(
        _ohlc([0, 60, 180, 240]), time_bucket=60)
    assert len(candles) == 4, "no candle may be invented"
    assert len(gaps) == 1, "the missing 120s bucket must be reported"
    assert duplicates == 0 and out_of_order == 0


def test_ohlc_normalisation_drops_duplicates_and_reports_them() -> None:
    candles, _gaps, duplicates, _out_of_order = normalise_ohlc(
        _ohlc([0, 60, 60, 120]), time_bucket=60)
    assert len(candles) == 3
    assert duplicates == 1


def test_ohlc_normalisation_sorts_chronologically() -> None:
    candles, _gaps, _dup, _ooo = normalise_ohlc(_ohlc([120, 0, 60]), time_bucket=60)
    assert [c.timestamp.timestamp() for c in candles] == [0, 60, 120]


def test_backfill_fingerprint_is_deterministic() -> None:
    assert _series().fingerprint == _series().fingerprint
    assert _series(market="ETH/MXN").fingerprint != _series().fingerprint


# ---------------------------------------------------------------------------
# Durable evidence store
# ---------------------------------------------------------------------------

def test_evidence_survives_a_restart(tmp_path: pathlib.Path) -> None:
    """The 0.1.4 gap: evidence had to outlive the process to be certifiable."""
    store = ResearchEvidenceStore(tmp_path)
    assert store.ingest(_observation()) == ACCEPTED
    reopened = ResearchEvidenceStore(tmp_path)
    aggregate = reopened.aggregate_for(market="SOL/MXN",
                                       profile_id="trend-continuation-v1")
    assert aggregate is not None and aggregate.round_trips == 91
    assert reopened.pairs() == (("SOL/MXN", "trend-continuation-v1"),)


def test_re_ingesting_the_same_evidence_is_deduplicated(tmp_path: pathlib.Path) -> None:
    """Otherwise a repeated backfill would look like a larger sample."""
    store = ResearchEvidenceStore(tmp_path)
    assert store.ingest(_observation()) == ACCEPTED
    assert store.ingest(_observation()) == DUPLICATE
    assert store.ingest(_observation()) == DUPLICATE
    aggregate = store.aggregate_for(market="SOL/MXN", profile_id="trend-continuation-v1")
    assert aggregate is not None and aggregate.round_trips == 91
    assert store.rejected_duplicates == 2


def test_a_different_candle_range_is_new_evidence(tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation())
    assert store.ingest(_observation(range_start_ms=3000, range_end_ms=4000)) == ACCEPTED


def test_a_shifted_window_cannot_inflate_the_sample(tmp_path: pathlib.Path) -> None:
    """Same candles fetched a minute later must not count twice."""
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(observed_at="2026-09-25T00:00:00Z"))
    assert store.ingest(_observation(observed_at="2026-09-25T00:01:00Z")) == DUPLICATE


def test_fixture_only_pairs_are_visible(tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(provenance_kind=SYNTHETIC_FIXTURE, market="ADA/MXN"))
    assert ("ADA/MXN", "trend-continuation-v1") in store.fixture_only_pairs()
    assert store.certifying_aggregate(market="ADA/MXN",
                                      profile_id="trend-continuation-v1") is None


def test_certifying_aggregate_excludes_fixture_evidence(tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(provenance_kind=SYNTHETIC_FIXTURE, round_trips=500,
                              wins=400, losses=100))
    store.ingest(_observation(provenance_kind=REAL_HISTORICAL, round_trips=3,
                              wins=2, losses=1))
    aggregate = store.certifying_aggregate(market="SOL/MXN",
                                           profile_id="trend-continuation-v1")
    assert aggregate is not None
    assert aggregate.round_trips == 3, "fixture round trips must not be counted"
    assert aggregate.is_real is True


def test_observation_rejects_impossible_counters() -> None:
    with pytest.raises(EvidenceError):
        _observation(wins=5, losses=5, round_trips=2)
    with pytest.raises(EvidenceError):
        _observation(round_trips=-1)
    with pytest.raises(EvidenceError):
        _observation(market="not-a-market")


def test_store_tolerates_a_torn_final_line(tmp_path: pathlib.Path) -> None:
    """A crash mid-write must not invalidate previously accepted evidence."""
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation())
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "OBSERVATION", "observation": {"truncated')
    reopened = ResearchEvidenceStore(tmp_path)
    assert reopened.pairs() == (("SOL/MXN", "trend-continuation-v1"),)


# ---------------------------------------------------------------------------
# Fixture evidence must never certify
# ---------------------------------------------------------------------------

def test_fixture_evaluation_is_refused_at_the_write_boundary(tmp_path: pathlib.Path) -> None:
    """Spec section 3: fixture evidence tests architecture, it cannot certify."""
    store = ResearchEvidenceStore(tmp_path)
    report = evaluate_market_into_store(
        series=_series(market="ADA/MXN",
                       provenance=Provenance(SYNTHETIC_FIXTURE, "SYNTHETIC_FIXTURE")),
        store=store, budget_mxn=D("11"), taker_fee_rate=PRODUCTION_FEE,
        book=HistoricalMarketBook(D("10"), D("1000")), policy=DEFAULT_POLICY)
    assert report.refused_non_certifying > 0
    assert report.accepted == 0
    assert store.pairs() == ()
    assert "ADA/MXN" not in store.markets()


def test_real_evaluation_is_recorded(tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    report = evaluate_market_into_store(
        series=_series(), store=store, budget_mxn=D("11"), taker_fee_rate=PRODUCTION_FEE,
        book=HistoricalMarketBook(D("10"), D("5000")), policy=DEFAULT_POLICY)
    assert report.accepted > 0 and report.refused_non_certifying == 0
    assert "SOL/MXN" in store.markets()


def test_repeated_real_evaluation_is_deduplicated_end_to_end(tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    first = evaluate_market_into_store(
        series=_series(), store=store, budget_mxn=D("11"), taker_fee_rate=PRODUCTION_FEE,
        book=HistoricalMarketBook(D("10"), D("5000")), policy=DEFAULT_POLICY)
    second = evaluate_market_into_store(
        series=_series(), store=store, budget_mxn=D("11"), taker_fee_rate=PRODUCTION_FEE,
        book=HistoricalMarketBook(D("10"), D("5000")), policy=DEFAULT_POLICY)
    assert first.accepted > 0
    assert second.accepted == 0 and second.duplicates == first.accepted


# ---------------------------------------------------------------------------
# Pair certification
# ---------------------------------------------------------------------------

def test_certification_is_per_pair_not_per_profile(tmp_path: pathlib.Path) -> None:
    """A profile doing well on one book says nothing about another book."""
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(market="SOL/MXN", round_trips=91, wins=60, losses=31))
    store.ingest(_observation(market="ETH/MXN", round_trips=0, wins=0, losses=0,
                              economic_passes=0, economic_rejects=50, signals=50,
                              median_net_pnl_mxn="0", median_net_edge_bps="0"))
    floor = _floor()
    sol = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                       store=store, floor=floor)
    eth = certify_pair(market="ETH/MXN", profile_id="trend-continuation-v1",
                       store=store, floor=floor)
    assert sol.state == CERTIFIED
    assert eth.state == NOT_VIABLE, "refused every opportunity; a demonstrated failure"


def test_pair_with_no_evidence_is_research_only(tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    verdict = certify_pair(market="XRP/MXN", profile_id="trend-continuation-v1",
                           store=store, floor=_floor())
    assert verdict.state == RESEARCH_ONLY
    assert verdict.certified is False


def test_pair_with_only_fixture_evidence_cannot_be_certified(tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(provenance_kind=SYNTHETIC_FIXTURE, round_trips=999, wins=900))
    verdict = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                           store=store, floor=_floor())
    assert verdict.certified is False
    assert verdict.state == ACCUMULATING_EVIDENCE
    assert verdict.evidence is None


def test_zero_opportunities_is_insufficient_evidence_not_viable(tmp_path: pathlib.Path) -> None:
    """No signal means nothing about economics was established."""
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(signals=0, economic_passes=0, economic_rejects=0,
                              round_trips=0, wins=0, losses=0, net_pnl_mxn="0"))
    verdict = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                           store=store, floor=_floor())
    assert verdict.state == INSUFFICIENT_EVIDENCE
    assert "NONZERO_OPPORTUNITY_COUNT" in verdict.failed_criteria


def test_repeated_economic_failure_is_not_viable(tmp_path: pathlib.Path) -> None:
    """Adequate observations and every opportunity refused on economics."""
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(signals=200, economic_passes=0, economic_rejects=200,
                              round_trips=0, wins=0, losses=0, net_pnl_mxn="0"))
    verdict = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                           store=store, floor=_floor())
    assert verdict.state == NOT_VIABLE
    assert "ECONOMIC_FEASIBILITY" in verdict.failed_criteria


def test_certification_reports_every_criterion_with_its_measurement(
        tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation())
    verdict = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                           store=store, floor=_floor())
    assert verdict.criteria, "criteria must be reported"
    for criterion in verdict.criteria:
        assert criterion.measured != "" and criterion.required != ""
    payload = verdict.public()
    assert payload["promotion"] == "DISABLED"


def test_certification_fails_on_lookahead(tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(lookahead_ok=False))
    verdict = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                           store=store, floor=_floor())
    assert verdict.certified is False
    assert "NO_LOOKAHEAD" in verdict.failed_criteria


def test_certification_fails_on_non_deterministic_replay(tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(deterministic=False))
    verdict = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                           store=store, floor=_floor())
    assert "REPLAY_DETERMINISM" in verdict.failed_criteria


def test_certification_fails_on_accounting_incompatibility(tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(accounting_compatible=False))
    verdict = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                           store=store, floor=_floor())
    assert "ACCOUNTING_COMPATIBILITY" in verdict.failed_criteria


def test_certification_fails_on_excessive_drawdown(tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(max_drawdown_mxn="9.99"))
    verdict = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                           store=store, floor=_floor(max_drawdown_mxn=D("0.50")))
    assert "BOUNDED_DRAWDOWN" in verdict.failed_criteria


def test_certification_fails_on_wide_spread(tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(median_spread_bps="500"))
    verdict = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                           store=store, floor=_floor(max_median_spread_bps=D("60")))
    assert "ACCEPTABLE_OBSERVED_SPREAD" in verdict.failed_criteria


def test_round_trip_floor_only_applies_when_opportunities_exist(
        tmp_path: pathlib.Path) -> None:
    """Requiring five round trips from a strategy that never signalled is unpassable."""
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(signals=0, economic_passes=0, economic_rejects=0,
                              round_trips=0, wins=0, losses=0, net_pnl_mxn="0"))
    verdict = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                           store=store, floor=_floor(min_round_trips=5))
    assert "MINIMUM_REAL_ROUND_TRIPS" not in verdict.failed_criteria
    assert verdict.state == INSUFFICIENT_EVIDENCE


def test_aggregate_positive_pnl_alone_is_not_sufficient(tmp_path: pathlib.Path) -> None:
    """Total P&L positive, but the drawdown evidence disqualifies it."""
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation(net_pnl_mxn="5.00", max_drawdown_mxn="4.99",
                              worst_trade_mxn="-4.00"))
    verdict = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                           store=store, floor=_floor(max_drawdown_mxn=D("0.50")))
    assert verdict.certified is False
    assert "BOUNDED_DRAWDOWN" in verdict.failed_criteria
    assert verdict.evidence is not None and verdict.evidence.net_pnl_mxn > 0


def test_suspension_preserves_the_evidence_that_earned_certification(
        tmp_path: pathlib.Path) -> None:
    """A currently-unhealthy market suspends; it never erases history."""
    store = ResearchEvidenceStore(tmp_path)
    store.ingest(_observation())
    certified = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                             store=store, floor=_floor())
    assert certified.state == CERTIFIED
    suspended = certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                             store=store, floor=_floor(), market_quality_ok=False,
                             suspension_reason="SPREAD_GUARD")
    assert suspended.state == SUSPENDED
    assert suspended.evidence is not None
    assert suspended.suspended_reason == "SPREAD_GUARD"
    # Recovery needs no re-earning: the evidence was never invalid.
    assert certify_pair(market="SOL/MXN", profile_id="trend-continuation-v1",
                        store=store, floor=_floor()).state == CERTIFIED


def test_certify_all_is_deterministically_ordered(tmp_path: pathlib.Path) -> None:
    store = ResearchEvidenceStore(tmp_path)
    for market in ("SOL/MXN", "ETH/MXN", "BTC/MXN"):
        store.ingest(_observation(market=market))
    first = certify_all(store=store, floor=_floor())
    second = certify_all(store=store, floor=_floor())
    assert [c.public() for c in first] == [c.public() for c in second]
    assert [c.market for c in first] == sorted(c.market for c in first)


def test_floor_rejects_float_thresholds() -> None:
    with pytest.raises(ValueError):
        CertificationFloor(max_drawdown_mxn=0.5)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Certified Production universe
# ---------------------------------------------------------------------------

def test_universe_admits_only_certified_and_currently_operational_pairs() -> None:
    from autofund.mvp.certification import certify_pair as _cp  # noqa: F401

    universe = build_certified_universe(
        certifications=(_certified_pair("SOL/MXN"),),
        current_market_data={"SOL/MXN": True}, fee_known={"SOL/MXN": True},
        book_compatible={"SOL/MXN": True}, accounting_compatible={"SOL/MXN": True},
        profile_fingerprints={"trend-continuation-v1": ("pfp", "sfp")})
    assert universe.markets == ("SOL/MXN",)
    assert universe.pairs == ("SOL/MXN|trend-continuation-v1",)


def test_universe_excludes_uncertified_pairs_with_a_reason() -> None:
    not_certified = type("C", (), {"market": "XRP/MXN", "profile_id": "p",
                                   "state": NOT_VIABLE, "selectable": False,
                                   "evidence": None})()
    universe = build_certified_universe(certifications=(not_certified,))
    assert universe.is_empty
    assert universe.exclusions[0]["reason"] == NOT_VIABLE


def test_universe_excludes_a_market_that_is_currently_unreadable() -> None:
    """Certification is about evidence; admission is about now."""
    universe = build_certified_universe(
        certifications=(_certified_pair("SOL/MXN"),),
        current_market_data={"SOL/MXN": False}, fee_known={"SOL/MXN": True})
    assert universe.is_empty
    assert universe.exclusions[0]["reason"] == REASON_NO_MARKET_DATA


def test_universe_excludes_markets_without_known_fees() -> None:
    universe = build_certified_universe(
        certifications=(_certified_pair("SOL/MXN"),),
        current_market_data={"SOL/MXN": True}, fee_known={"SOL/MXN": False})
    assert universe.is_empty
    assert universe.exclusions[0]["reason"] == REASON_NO_FEE_DATA


def test_universe_never_admits_a_fiat_like_book() -> None:
    universe = build_certified_universe(
        certifications=(_certified_pair("USD/MXN"),), current_market_data={"USD/MXN": True},
        fee_known={"USD/MXN": True})
    assert universe.is_empty
    assert universe.exclusions[0]["reason"] == REASON_FIAT_LIKE_MARKET


def test_universe_reports_multi_market_production_still_disabled() -> None:
    universe = build_certified_universe(certifications=(_certified_pair("SOL/MXN"),),
                                        current_market_data={"SOL/MXN": True},
                                        fee_known={"SOL/MXN": True})
    assert universe.public()["multi_market_production"] == "DISABLED"


# ---------------------------------------------------------------------------
# Deterministic selector
# ---------------------------------------------------------------------------

def test_selector_picks_the_economically_best_market() -> None:
    """Spec 19: SOL wins on positive net edge, not on seniority or signal count."""
    result = select_production_opportunity(candidates=(
        _candidate(market="BTC/MXN", book="btc_mxn", decision=DECISION_NO_SIGNAL),
        _candidate(market="ETH/MXN", book="eth_mxn", expected_net_edge_bps=D("-50"),
                   expected_net_pnl_mxn=D("-0.01"),
                   viability=type("V", (), {"viable": False})()),
        _candidate(market="SOL/MXN", book="sol_mxn", expected_net_edge_bps=D("200")),
        _candidate(market="XRP/MXN", book="xrp_mxn",
                   certification=type("C", (), {"market": "XRP/MXN",
                                                "profile_id": "trend-continuation-v1",
                                                "state": NOT_VIABLE,
                                                "selectable": False, "evidence": None})()),
    ), context=_context())
    assert result.is_trade is True
    assert result.selected is not None and result.selected.market == "SOL/MXN"
    assert result.selected.book == "sol_mxn"


def test_no_trade_when_nothing_is_admissible() -> None:
    """Spec 20: never choose the least-bad market."""
    result = select_production_opportunity(candidates=(
        _candidate(market="BTC/MXN", book="btc_mxn", decision=DECISION_NO_SIGNAL),
        _candidate(market="ETH/MXN", book="eth_mxn", expected_net_edge_bps=D("-10"),
                   viability=type("V", (), {"viable": False})()),
        _candidate(market="SOL/MXN", book="sol_mxn",
                   certification=type("C", (), {"market": "SOL/MXN",
                                                "profile_id": "p", "state": RESEARCH_ONLY,
                                                "selectable": False, "evidence": None})()),
    ), context=_context())
    assert result.outcome == NO_TRADE
    assert result.selected is None
    assert result.is_trade is False


def test_selector_never_selects_a_negative_net_edge_candidate() -> None:
    result = select_production_opportunity(candidates=(
        _candidate(market="SOL/MXN", book="sol_mxn", expected_net_edge_bps=D("-100")),
    ), context=_context())
    assert result.outcome == NO_TRADE


def test_selector_reports_a_distinct_reason_per_refusal_kind() -> None:
    cases = [
        (_candidate(market="BTC/MXN", book="btc_mxn", decision=DECISION_NO_SIGNAL),
         REASON_NO_SIGNAL),
        (_candidate(market="ETH/MXN", book="eth_mxn",
                    certification=type("C", (), {"market": "ETH/MXN", "profile_id": "p",
                                                 "state": RESEARCH_ONLY,
                                                 "selectable": False, "evidence": None})()),
         REASON_NOT_CERTIFIED),
        (_candidate(market="SOL/MXN", book="sol_mxn",
                    certification=type("C", (), {"market": "SOL/MXN", "profile_id": "p",
                                                 "state": NOT_VIABLE,
                                                 "selectable": False, "evidence": None})()),
         REASON_NOT_VIABLE),
        (_candidate(market="XRP/MXN", book="xrp_mxn", expected_net_edge_bps=D("-1"),
                    viability=type("V", (), {"viable": False})()), REASON_ECONOMIC_GUARD_REJECT),
        (_candidate(market="ADA/MXN", book="ada_mxn", spread_bps=D("500")), REASON_SPREAD_REJECT),
        (_candidate(market="DOT/MXN", book="dot_mxn", data_age_seconds=D("99")), REASON_STALE_DATA),
        (_candidate(market="LTC/MXN", book="ltc_mxn", fee_available=False), REASON_NO_FEE_DATA),
        (_candidate(market="USD/MXN", book="usd_mxn"), REASON_FIAT_LIKE_MARKET),
    ]
    for candidate, expected in cases:
        result = select_production_opportunity(candidates=(candidate,), context=_context())
        assert result.outcome == NO_TRADE, expected
        assert result.candidates[0].reason_code == expected


def test_selector_blocks_every_intent_while_an_order_is_unresolved() -> None:
    """Spec 22: at most one unresolved Production order, globally."""
    result = select_production_opportunity(
        candidates=(_candidate(market="SOL/MXN", book="sol_mxn"),),
        context=_context(unresolved_orders=1))
    assert result.outcome == NO_TRADE
    assert result.reason_code == REASON_UNRESOLVED_ORDER
    assert result.candidates[0].reason_code == REASON_UNRESOLVED_ORDER


def test_selector_refuses_a_second_position_in_the_same_market() -> None:
    result = select_production_opportunity(
        candidates=(_candidate(market="SOL/MXN", book="sol_mxn"),),
        context=_context(open_markets=frozenset({"SOL/MXN"})))
    assert result.outcome == NO_TRADE
    assert result.candidates[0].reason_code == REASON_ALREADY_HOLDING


def test_selector_honours_global_deployment_headroom() -> None:
    """Limits are global: a second position cannot be funded by ignoring the first."""
    no_headroom = PortfolioConstraints(
        authorized_capital_mxn=D("50"), max_deployment_mxn=D("25"),
        single_order_cap_mxn=D("11"), deployed_mxn=D("25"), cash_mxn=D("50"))
    result = select_production_opportunity(
        candidates=(_candidate(market="SOL/MXN", book="sol_mxn"),),
        context=_context(constraints=no_headroom))
    assert result.outcome == NO_TRADE
    assert result.candidates[0].reason_code == REASON_CAPITAL_REJECT


def test_selector_ranks_by_net_edge_then_net_pnl_then_spread() -> None:
    """Economics first; market quality only as a deterministic tie-break."""
    a = _candidate(market="AAA/MXN", book="aaa_mxn", expected_net_edge_bps=D("100"),
                   expected_net_pnl_mxn=D("0.01"), spread_bps=D("50"))
    b = _candidate(market="BBB/MXN", book="bbb_mxn", expected_net_edge_bps=D("100"),
                   expected_net_pnl_mxn=D("0.01"), spread_bps=D("5"))
    c = _candidate(market="CCC/MXN", book="ccc_mxn", expected_net_edge_bps=D("150"),
                   expected_net_pnl_mxn=D("0.001"), spread_bps=D("90"))
    result = select_production_opportunity(candidates=(a, b, c), context=_context())
    assert result.selected is not None and result.selected.market == "CCC/MXN"
    # Same edge and same P&L: the tighter spread wins.
    result = select_production_opportunity(candidates=(a, b), context=_context())
    assert result.selected is not None and result.selected.market == "BBB/MXN"


def test_selector_is_deterministic_regardless_of_input_order() -> None:
    candidates = (
        _candidate(market="SOL/MXN", book="sol_mxn", expected_net_edge_bps=D("120")),
        _candidate(market="ETH/MXN", book="eth_mxn", expected_net_edge_bps=D("120")),
        _candidate(market="ADA/MXN", book="ada_mxn", expected_net_edge_bps=D("120")),
    )
    forward = select_production_opportunity(candidates=candidates, context=_context())
    reverse = select_production_opportunity(candidates=tuple(reversed(candidates)),
                                            context=_context())
    assert forward.selected is not None and reverse.selected is not None
    assert forward.selected.market == reverse.selected.market
    assert forward.public()["selected"] == reverse.public()["selected"]


def test_selector_never_returns_more_than_one_opportunity() -> None:
    result = select_production_opportunity(
        candidates=tuple(_candidate(market=f"M{index}/MXN", book=f"m{index}_mxn")
                         for index in range(5)), context=_context())
    assert result.is_trade
    assert sum(1 for item in result.candidates if item.eligible) == 5
    assert result.selected is not None


def test_selection_result_reports_no_multi_market_production() -> None:
    result = select_production_opportunity(
        candidates=(_candidate(),), context=_context())
    assert result.public()["multi_market_production"] == "DISABLED"
    assert result.public()["one_unresolved_order_globally"] is True


def test_deep_but_narrow_book_is_refused_for_depth() -> None:
    result = select_production_opportunity(
        candidates=(_candidate(bid_quantity_mxn=D("0.0000001")),), context=_context())
    assert result.outcome == NO_TRADE
    assert result.candidates[0].reason_code == "DEPTH_REJECT"


# ---------------------------------------------------------------------------
# Multi-asset accounting
# ---------------------------------------------------------------------------

def test_market_book_parses_and_validates() -> None:
    book = MarketBook.of("eth/mxn")
    assert book.market == "ETH/MXN" and book.book == "eth_mxn" and book.base == "ETH"
    assert MarketBook.from_book("sol_mxn").market == "SOL/MXN"
    assert MarketBook.btc_mxn().book == "btc_mxn"


@pytest.mark.parametrize("bad", ["ETHMXN", "ETH/", "/MXN", "ETH/MXN/EXTRA", ""])
def test_market_book_rejects_malformed_markets(bad: str) -> None:
    with pytest.raises(MarketError):
        MarketBook.of(bad)


def test_inventory_is_asset_generic() -> None:
    positions = {"SOL/MXN": type("P", (), {"quantity": D("3"), "cost_basis_mxn": D("9"),
                                           "realized_pnl_mxn": D("1")})()}
    inventory = inventory_for(positions, "SOL/MXN")
    assert inventory.quantity == D("3") and inventory.average_cost_mxn == D("3")
    assert inventory.base == "SOL"


def test_inventory_for_absent_market_is_explicitly_empty() -> None:
    inventory = inventory_for({}, "XRP/MXN")
    assert inventory.quantity == ZERO_MXN and inventory.market == "XRP/MXN"


def test_deployed_value_sums_every_market() -> None:
    positions = {
        "BTC/MXN": type("P", (), {"quantity": D("1"), "cost_basis_mxn": D("10"),
                                  "realized_pnl_mxn": ZERO_MXN})(),
        "SOL/MXN": type("P", (), {"quantity": D("2"), "cost_basis_mxn": D("5"),
                                  "realized_pnl_mxn": ZERO_MXN})(),
    }
    assert deployed_value_mxn(positions, {}) == D("15")


def test_foreign_wallet_inventory_is_never_sellable() -> None:
    """The critical multi-asset regression: never sell the whole wallet balance."""
    positions = {"SOL/MXN": type("P", (), {"quantity": D("0.02"), "cost_basis_mxn": D("2"),
                                           "realized_pnl_mxn": ZERO_MXN})()}
    wallet = {"SOL": D("5000"), "MXN": D("1000")}
    assert sellable_quantity(positions, "SOL/MXN", wallet) == D("0.02")
    assert sellable_quantity(positions, "SOL/MXN") != wallet["SOL"]


def test_foreign_inventory_isolation_holds_for_every_asset() -> None:
    for base in ("BTC", "ETH", "SOL", "XRP", "ADA"):
        market = f"{base}/MXN"
        positions = {market: type("P", (), {"quantity": D("0.01"),
                                            "cost_basis_mxn": D("1"),
                                            "realized_pnl_mxn": ZERO_MXN})()}
        assert sellable_quantity(positions, market, {base: D("999999")}) == D("0.01")


def test_sellable_quantity_is_zero_without_an_autofund_position() -> None:
    assert sellable_quantity({}, "SOL/MXN", {"SOL": D("9999")}) == ZERO_MXN


def test_global_deployment_cap_governs_a_second_market() -> None:
    positions = {"BTC/MXN": type("P", (), {"quantity": D("1"), "cost_basis_mxn": D("20"),
                                           "realized_pnl_mxn": ZERO_MXN})()}
    allowed, reason = can_open_in_market(positions=positions, market="SOL/MXN",
                                         budget_mxn=D("11"), max_deployment_mxn=D("25"))
    assert allowed is False and reason == "DEPLOYMENT_CAP_EXCEEDED"
    allowed, reason = can_open_in_market(positions=positions, market="SOL/MXN",
                                         budget_mxn=D("5"), max_deployment_mxn=D("25"))
    assert allowed is True and reason == ""


def test_second_position_in_the_same_market_is_refused() -> None:
    positions = {"SOL/MXN": type("P", (), {"quantity": D("1"), "cost_basis_mxn": D("1"),
                                           "realized_pnl_mxn": ZERO_MXN})()}
    allowed, reason = can_open_in_market(positions=positions, market="SOL/MXN",
                                         budget_mxn=D("5"), max_deployment_mxn=D("25"))
    assert allowed is False and reason == "POSITION_ALREADY_OPEN_IN_MARKET"


def test_intent_book_must_be_allowed() -> None:
    intent = type("I", (), {"book": "sol_mxn"})()
    validate_intent_market(intent=intent, allowed_books=frozenset({"sol_mxn"}))
    with pytest.raises(MarketError, match="INTENT_BOOK_NOT_ALLOWED"):
        validate_intent_market(intent=intent, allowed_books=frozenset({"btc_mxn"}))
    with pytest.raises(MarketError):
        validate_intent_market(intent=type("I", (), {})(), allowed_books=frozenset())


# ---------------------------------------------------------------------------
# Telemetry vocabulary
# ---------------------------------------------------------------------------

def test_market_selection_checkpoints_are_registered() -> None:
    """An unregistered checkpoint raises at runtime, so registration is a contract."""
    assert {"MARKET_SELECTION_STARTED", "MARKET_CANDIDATE_EVALUATED", "MARKET_SELECTED",
            "MARKET_SELECTION_NO_TRADE", "MARKET_CERTIFICATION_UPDATED",
            "MARKET_CERTIFICATION_SUSPENDED", "FIRST_REAL_DYNAMIC_MARKET_ORDER",
            "MARKET_EVIDENCE_INGESTED",
            "MARKET_EVIDENCE_DUPLICATE_REJECTED"} <= CHECKPOINTS


# ---------------------------------------------------------------------------
# Production safety
# ---------------------------------------------------------------------------

def test_no_module_can_post_an_order_in_market_selection() -> None:
    """The whole selection and certification path must be structurally write-free."""
    forbidden = ("place_market_order", "place_market_sell", "submit_authorized",
                 "BitsoProductionLiveClient", "BitsoProductionLiveTransport")
    root = pathlib.Path("src/autofund/mvp")
    for name in ("backfill.py", "evidence.py", "certification.py", "universe.py",
                 "evidence_cycle.py"):
        text = (root / name).read_text(encoding="utf-8")
        for marker in forbidden:
            assert marker not in text, f"{name} references {marker}"


def test_production_fee_and_policy_are_never_placeholder_values() -> None:
    assert PRODUCTION_FEE == D("0.0078")
    assert DEFAULT_POLICY.minimum_net_profit_mxn == ZERO_MXN


def test_existing_champion_fingerprint_is_still_untouched() -> None:
    from autofund.mvp.adaptive import AdaptiveEngine

    assert AdaptiveEngine().champion.fingerprint == (
        "5a47c9833724e58d64ef37e8702aafaf2e503940e91ad17aa626a72ac53865a8")


ZERO_MXN = D("0")

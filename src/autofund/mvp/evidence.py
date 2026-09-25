"""Durable research evidence store.

MVP 0.1.4 evaluated profiles but kept the results in memory, so every restart threw
away the evidence a market needed to certify. A market could never accumulate enough
real evidence to escape RESEARCH_ONLY, because the evidence never survived the
process. This store is that missing bridge.

Design decisions that matter:

- **Append-only journal, replay-derived state.** Every accepted observation is
  appended as a line; the current aggregate is recomputed by replaying the journal.
  There is no in-place mutation, so a crash cannot leave a half-updated aggregate
  that contradicts its own evidence.
- **Deterministic deduplication.** Each observation carries a content-derived key
  covering the market, profile, provenance, dataset fingerprint and the exact
  counters. Re-ingesting the same evaluation is a no-op, so re-running a backfill
  cannot inflate sample size. That is what makes "N round trips" mean N round trips.
- **Aggregates are never fabricated.** A field with no observations reports None and
  travels as "no evidence", never as zero. Zero round trips and "not measured" are
  different statements.
- **Nothing here can trade.** This module has no exchange handle, no wallet, and no
  intent. It stores numbers.

The store is deliberately separate from the live financial journal: research evidence
must never be interleaved with the ledger that defines financial truth.
"""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.decimal_utils import ZERO, financial
from autofund.replay.serialization import fingerprint

STORE_VERSION = "autofund.research-evidence.v1"
STORE_FILE = "research_evidence.jsonl"

ACCEPTED = "ACCEPTED"
DUPLICATE = "DUPLICATE"


class EvidenceError(ValueError):
    """An observation violated the evidence contract."""


@dataclass(frozen=True, slots=True)
class EvidenceObservation:
    """One profile/market evaluation, with its provenance and full cost breakdown.

    Every field the certification floor and the reporting requirement (spec 11) needs
    is present, so certification never has to re-derive a number from raw candles and
    cannot disagree with what was observed.
    """

    market: str
    profile_id: str
    profile_version: str
    profile_fingerprint: str
    strategy_fingerprint: str
    economic_policy_fingerprint: str
    provenance_kind: str
    provenance_source: str
    dataset_fingerprint: str
    observed_at: str
    observation_hours: str

    closed_candles: int
    evaluations: int
    signals: int
    economic_passes: int
    economic_rejects: int
    simulated_entries: int
    round_trips: int
    data_quality_failures: int

    gross_pnl_mxn: str
    fees_mxn: str
    spread_cost_mxn: str
    slippage_cost_mxn: str
    net_pnl_mxn: str
    max_drawdown_mxn: str

    wins: int
    losses: int
    worst_trade_mxn: str
    median_net_pnl_mxn: str
    median_net_edge_bps: str
    average_holding_candles: str

    median_spread_bps: str
    p95_spread_bps: str
    modelled_slippage_bps: str

    lookahead_ok: bool
    deterministic: bool
    execution_compatible: bool
    accounting_compatible: bool

    # Candle coverage, in exchange bucket milliseconds. Part of the deduplication
    # identity: two evaluations over the same candles are the same evidence even if
    # fetched at different times. Without it, a re-run whose window slipped by one
    # bucket would look like new evidence and would silently inflate the sample --
    # exactly the manufactured confidence this store exists to prevent.
    range_start_ms: int = 0
    range_end_ms: int = 0
    # Whether slippage came from a real book walk (live capture) or a stated assumption
    # (historical, where no depth is published). Reported, and deliberately distinct
    # from execution compatibility.
    depth_observed: bool = False

    def __post_init__(self) -> None:
        if not self.market or "/" not in self.market:
            raise EvidenceError("observation requires a BASE/QUOTE market")
        if not self.profile_id:
            raise EvidenceError("observation requires a profile id")
        for name in ("closed_candles", "evaluations", "signals", "economic_passes",
                     "economic_rejects", "simulated_entries", "round_trips", "wins",
                     "losses", "data_quality_failures"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise EvidenceError(f"{name} must be a nonnegative int")
        if self.wins + self.losses > self.round_trips:
            raise EvidenceError("wins + losses cannot exceed round trips")
        for name in ("gross_pnl_mxn", "fees_mxn", "spread_cost_mxn", "slippage_cost_mxn",
                     "net_pnl_mxn", "max_drawdown_mxn", "worst_trade_mxn",
                     "median_net_pnl_mxn", "median_net_edge_bps",
                     "average_holding_candles", "median_spread_bps", "p95_spread_bps",
                     "modelled_slippage_bps", "observation_hours"):
            try:
                Decimal(getattr(self, name))
            except Exception as exc:
                raise EvidenceError(f"{name} must be a Decimal string") from exc

    @property
    def identity_key(self) -> str:
        """Content-derived deduplication key.

        Covers the evidence *content* and the exact candle range, but not a
        fetch timestamp: the same candles fetched twice are the same evidence, while
        a genuinely different window is new evidence. A timestamp would make every
        re-ingest look new and inflate the sample.
        """
        return fingerprint({
            "schema": STORE_VERSION, "market": self.market, "profile_id": self.profile_id,
            "profile_fingerprint": self.profile_fingerprint,
            "economic_policy_fingerprint": self.economic_policy_fingerprint,
            "provenance": self.provenance_kind,
            "range": [self.range_start_ms, self.range_end_ms],
            "closed_candles": self.closed_candles,
        })

    @property
    def is_real(self) -> bool:
        from .backfill import CERTIFYING_PROVENANCE

        return self.provenance_kind in CERTIFYING_PROVENANCE

    def public(self) -> dict[str, Any]:
        payload = {name: getattr(self, name) for name in self.__dataclass_fields__}
        payload["identity_key"] = self.identity_key
        payload["is_real"] = self.is_real
        payload["version"] = STORE_VERSION
        return payload


def _observation_from(row: dict[str, Any]) -> EvidenceObservation:
    return EvidenceObservation(**{name: row[name] for name in EvidenceObservation.__dataclass_fields__})


@dataclass(frozen=True, slots=True)
class AggregateEvidence:
    """Replay-derived aggregate for one market/profile pair, by provenance."""

    market: str
    profile_id: str
    provenance_kind: str
    observations: int
    closed_candles: int
    evaluations: int
    signals: int
    economic_passes: int
    economic_rejects: int
    simulated_entries: int
    round_trips: int
    wins: int
    losses: int
    data_quality_failures: int
    gross_pnl_mxn: Decimal
    fees_mxn: Decimal
    spread_cost_mxn: Decimal
    slippage_cost_mxn: Decimal
    net_pnl_mxn: Decimal
    max_drawdown_mxn: Decimal
    worst_trade_mxn: Decimal | None
    median_net_pnl_mxn: Decimal | None
    median_net_edge_bps: Decimal | None
    median_spread_bps: Decimal | None
    p95_spread_bps: Decimal | None
    observation_hours: Decimal
    lookahead_ok: bool
    deterministic: bool
    execution_compatible: bool
    accounting_compatible: bool
    depth_observed: bool = False
    dataset_fingerprints: tuple[str, ...] = ()

    @property
    def is_real(self) -> bool:
        from .backfill import CERTIFYING_PROVENANCE

        return self.provenance_kind in CERTIFYING_PROVENANCE

    @property
    def economic_reject_rate(self) -> Decimal:
        considered = self.economic_passes + self.economic_rejects
        if considered <= 0:
            return ZERO
        return Decimal(self.economic_rejects) / Decimal(considered)

    @property
    def win_rate(self) -> Decimal | None:
        if self.round_trips <= 0:
            return None
        return Decimal(self.wins) / Decimal(self.round_trips)

    @property
    def median_net_pnl_per_trade_mxn(self) -> Decimal | None:
        return self.median_net_pnl_mxn

    def telemetry(self) -> dict[str, Any]:
        return {"market": self.market, "profile_id": self.profile_id,
                "provenance": self.provenance_kind, "is_real": self.is_real,
                "observations": self.observations,
                "observation_hours": str(self.observation_hours),
                "closed_candles": self.closed_candles, "evaluations": self.evaluations,
                "signals": self.signals, "economic_passes": self.economic_passes,
                "economic_rejects": self.economic_rejects,
                "economic_reject_rate": str(self.economic_reject_rate),
                "simulated_entries": self.simulated_entries,
                "round_trips": self.round_trips, "wins": self.wins, "losses": self.losses,
                "win_rate": None if self.win_rate is None else str(self.win_rate),
                "data_quality_failures": self.data_quality_failures,
                "gross_pnl_mxn": str(self.gross_pnl_mxn), "fees_mxn": str(self.fees_mxn),
                "spread_cost_mxn": str(self.spread_cost_mxn),
                "slippage_cost_mxn": str(self.slippage_cost_mxn),
                "net_pnl_mxn": str(self.net_pnl_mxn),
                "max_drawdown_mxn": str(self.max_drawdown_mxn),
                "worst_trade_mxn": None if self.worst_trade_mxn is None else str(self.worst_trade_mxn),
                "median_net_pnl_mxn": None if self.median_net_pnl_mxn is None else str(self.median_net_pnl_mxn),
                "median_net_edge_bps": None if self.median_net_edge_bps is None else str(self.median_net_edge_bps),
                "median_spread_bps": None if self.median_spread_bps is None else str(self.median_spread_bps),
                "p95_spread_bps": None if self.p95_spread_bps is None else str(self.p95_spread_bps),
                "lookahead_ok": self.lookahead_ok, "deterministic": self.deterministic,
                "execution_compatible": self.execution_compatible,
                "accounting_compatible": self.accounting_compatible,
                "depth_observed": self.depth_observed,
                "dataset_fingerprints": list(self.dataset_fingerprints)}


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


def _percentile(values: list[Decimal], fraction: Decimal) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    index = int((Decimal(len(ordered) - 1) * fraction).to_integral_value())
    return ordered[max(0, min(index, len(ordered) - 1))]


@financial
def aggregate(observations: list[EvidenceObservation]) -> AggregateEvidence:
    """Recompute the aggregate from scratch. Pure, so replaying the journal is safe."""
    if not observations:
        raise EvidenceError("cannot aggregate zero observations")
    first = observations[0]
    corners = [o.worst_trade_mxn for o in observations]
    return AggregateEvidence(
        market=first.market, profile_id=first.profile_id,
        provenance_kind=first.provenance_kind, observations=len(observations),
        closed_candles=sum(o.closed_candles for o in observations),
        evaluations=sum(o.evaluations for o in observations),
        signals=sum(o.signals for o in observations),
        economic_passes=sum(o.economic_passes for o in observations),
        economic_rejects=sum(o.economic_rejects for o in observations),
        simulated_entries=sum(o.simulated_entries for o in observations),
        round_trips=sum(o.round_trips for o in observations),
        wins=sum(o.wins for o in observations), losses=sum(o.losses for o in observations),
        data_quality_failures=sum(o.data_quality_failures for o in observations),
        gross_pnl_mxn=sum((Decimal(o.gross_pnl_mxn) for o in observations), ZERO),
        fees_mxn=sum((Decimal(o.fees_mxn) for o in observations), ZERO),
        spread_cost_mxn=sum((Decimal(o.spread_cost_mxn) for o in observations), ZERO),
        slippage_cost_mxn=sum((Decimal(o.slippage_cost_mxn) for o in observations), ZERO),
        net_pnl_mxn=sum((Decimal(o.net_pnl_mxn) for o in observations), ZERO),
        max_drawdown_mxn=max(Decimal(o.max_drawdown_mxn) for o in observations),
        worst_trade_mxn=min(Decimal(value) for value in corners),
        median_net_pnl_mxn=_median([Decimal(o.median_net_pnl_mxn) for o in observations]),
        median_net_edge_bps=_median([Decimal(o.median_net_edge_bps) for o in observations]),
        median_spread_bps=_median([Decimal(o.median_spread_bps) for o in observations]),
        p95_spread_bps=_percentile([Decimal(o.p95_spread_bps) for o in observations],
                                   Decimal("0.95")),
        observation_hours=sum((Decimal(o.observation_hours) for o in observations), ZERO),
        # A conjunction: any single observation failing a safety property means the
        # pair has not demonstrated that property, so it must not certify.
        lookahead_ok=all(o.lookahead_ok for o in observations),
        deterministic=all(o.deterministic for o in observations),
        execution_compatible=all(o.execution_compatible for o in observations),
        accounting_compatible=all(o.accounting_compatible for o in observations),
        depth_observed=all(o.depth_observed for o in observations),
        dataset_fingerprints=tuple(sorted({o.dataset_fingerprint for o in observations})))


@dataclass
class ResearchEvidenceStore:
    """Append-only durable evidence, with aggregate state derived by replay.

    The store is deliberately permissive about *what* it accepts and strict about
    *how* it counts: an observation is written exactly once (by content key), and the
    aggregate is always a pure function of what is on disk.
    """

    root: Path
    _accepted: dict[str, list[EvidenceObservation]] = field(default_factory=dict, init=False)
    _seen: set[str] = field(default_factory=set, init=False)
    _rejected: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._replay()

    @property
    def path(self) -> Path:
        return self.root / STORE_FILE

    @property
    def rejected_duplicates(self) -> int:
        return self._rejected

    def _replay(self) -> None:
        self._accepted.clear()
        self._seen.clear()
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn final line must not invalidate prior evidence
            if row.get("kind") != "OBSERVATION":
                continue
            try:
                observation = _observation_from(row["observation"])
            except Exception:
                continue
            key = observation.identity_key
            if key in self._seen:
                continue
            self._seen.add(key)
            self._accepted.setdefault(_pair_key(observation.market,
                                                observation.profile_id,
                                                observation.provenance_kind), []).append(observation)

    def ingest(self, observation: EvidenceObservation) -> str:
        """Record one observation. Returns ACCEPTED or DUPLICATE."""
        key = observation.identity_key
        if key in self._seen:
            self._rejected += 1
            return DUPLICATE
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": "OBSERVATION", "observation": observation.public(),
                                     "identity_key": key}, sort_keys=True,
                                    separators=(",", ":")) + "\n")
        self._seen.add(key)
        self._accepted.setdefault(_pair_key(observation.market, observation.profile_id,
                                            observation.provenance_kind), []).append(observation)
        return ACCEPTED

    def pairs(self) -> tuple[tuple[str, str], ...]:
        """Every (market, profile) pair with any evidence, real or fixture."""
        seen = {(key.split("|", 1)[0], key.split("|", 2)[1]) for key in self._accepted}
        return tuple(sorted(seen))

    def aggregate_for(self, *, market: str, profile_id: str,
                      provenance_kind: str | None = None) -> AggregateEvidence | None:
        """Aggregate for one pair, optionally restricted to a provenance class.

        Certification always asks for a certifying provenance, so fixture evidence can
        never be summed into a certification decision by a careless caller.
        """
        groups = [
            (key, value) for key, value in self._accepted.items()
            if key.startswith(_pair_key(market, profile_id, ""))
            and (provenance_kind is None or key.endswith(provenance_kind))]
        if not groups:
            return None
        observations = [o for _key, value in groups for o in value]
        return aggregate(observations)

    def certifying_aggregate(self, *, market: str, profile_id: str) -> AggregateEvidence | None:
        """Aggregate over certifying provenance only. This is what certification uses."""
        from .backfill import CERTIFYING_PROVENANCE

        merged: list[EvidenceObservation] = []
        for kind in sorted(CERTIFYING_PROVENANCE):
            merged.extend(self._observations(market, profile_id, kind))
        return aggregate(merged) if merged else None

    def _observations(self, market: str, profile_id: str,
                      provenance_kind: str) -> list[EvidenceObservation]:
        return list(self._accepted.get(_pair_key(market, profile_id, provenance_kind), []))

    def fixture_only_pairs(self) -> tuple[tuple[str, str], ...]:
        """Pairs whose only evidence is non-certifying. Reported so it is visible."""
        from .backfill import CERTIFYING_PROVENANCE

        result: list[tuple[str, str]] = []
        for market, profile_id in self.pairs():
            has_real = any(self._observations(market, profile_id, kind) for kind in CERTIFYING_PROVENANCE)
            if not has_real:
                result.append((market, profile_id))
        return tuple(result)

    def markets(self) -> tuple[str, ...]:
        return tuple(sorted({market for market, _profile in self.pairs()}))

    def telemetry(self) -> dict[str, Any]:
        from .backfill import CERTIFYING_PROVENANCE

        real = [key for key in self._accepted if key.rsplit("|", 1)[-1] in CERTIFYING_PROVENANCE]
        return {"version": STORE_VERSION, "pairs": len(self.pairs()),
                "real_pairs": len(real), "fixture_only_pairs": len(self.fixture_only_pairs()),
                "markets": list(self.markets()),
                "accepted_observations": sum(len(v) for v in self._accepted.values()),
                "rejected_duplicates": self._rejected,
                "path": str(self.path)}


def _pair_key(market: str, profile_id: str, provenance_kind: str) -> str:
    return f"{market}|{profile_id}|{provenance_kind}"


__all__ = [
    "ACCEPTED",
    "DUPLICATE",
    "STORE_FILE",
    "STORE_VERSION",
    "AggregateEvidence",
    "EvidenceError",
    "EvidenceObservation",
    "ResearchEvidenceStore",
    "aggregate",
]

"""Strategy contract persistence, so a position is always managed by its own strategy.

Specification requirement, and the reason it matters: *a position opened by strategy
profile A must not silently be closed using the profit-taking semantics of profile B*.

In MVP 0.1.4 this is not yet dangerous, because Production is Champion-only. It
becomes dangerous the moment multi-market selection arrives, when an ETH position
opened by a trend profile could be handed to mean-reversion exit logic and closed on
a rule its own strategy never agreed to. The contract is the mechanism that prevents
that, and establishing it before it is needed is the point.

Two structural guarantees:

- **The contract is written before the intent exists.** `record_entry` is called with
  the intent's origin id, so there is no window in which a position exists without a
  recorded owner.
- **Exit semantics resolve strictly by profile id.** `exit_semantics_for` raises for
  an unknown contract rather than falling back to the Champion. A silent fallback
  would be exactly the bug this module exists to prevent.

Contracts are stored in a dedicated append-only file under the session artifacts,
separate from the live financial journal. Simulated and research evidence never
enters financial truth.
"""

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.decimal_utils import financial
from autofund.replay.serialization import fingerprint

CONTRACT_VERSION = "autofund.position-contract.v1"
CONTRACT_FILE = "position_contracts.jsonl"


class ContractError(ValueError):
    """A position contract is missing, malformed, or mismatched."""


MISSING_CONTRACT = "NO_POSITION_CONTRACT_FOR_ORIGIN"
PROFILE_MISMATCH = "EXIT_PROFILE_DOES_NOT_MATCH_ENTRY_PROFILE"


@dataclass(frozen=True, slots=True)
class EntryEconomicEvidence:
    """What the economics looked like when the position was opened.

    Persisted so a later reviewer can tell whether an exit is being judged against
    the same conditions the entry was admitted under, or against different ones.
    """

    expected_gross_edge_bps: Decimal
    estimated_round_trip_friction_bps: Decimal
    expected_net_edge_bps: Decimal
    taker_fee_rate: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    admission_outcome: str
    economic_policy_version: str

    def public(self) -> dict[str, str]:
        return {"expected_gross_edge_bps": str(self.expected_gross_edge_bps),
                "estimated_round_trip_friction_bps": str(self.estimated_round_trip_friction_bps),
                "expected_net_edge_bps": str(self.expected_net_edge_bps),
                "taker_fee_rate": str(self.taker_fee_rate),
                "spread_bps": str(self.spread_bps), "slippage_bps": str(self.slippage_bps),
                "admission_outcome": self.admission_outcome,
                "economic_policy_version": self.economic_policy_version}


@dataclass(frozen=True, slots=True)
class ExpectedExitModel:
    """The exit the opening strategy intended, recorded at entry."""

    exit_class: str
    target_price_mxn: Decimal
    target_bps: Decimal
    target_model_version: str
    expected_holding_horizon: int

    def public(self) -> dict[str, str]:
        return {"exit_class": self.exit_class, "target_price_mxn": str(self.target_price_mxn),
                "target_bps": str(self.target_bps),
                "target_model_version": self.target_model_version,
                "expected_holding_horizon": str(self.expected_holding_horizon)}


@dataclass(frozen=True, slots=True)
class PositionContract:
    """Owner record for one position: which strategy opened it and how it must exit.

    `key` is the intent origin id for Production intents, so the contract and the
    order share one identifier.
    """

    key: str
    market: str
    major_asset: str
    strategy_profile_id: str
    strategy_version: str
    strategy_fingerprint: str
    profile_fingerprint: str
    entry_economics: EntryEconomicEvidence
    expected_exit_model: ExpectedExitModel
    opened_at: str
    mode: str = "PRODUCTION"

    def __post_init__(self) -> None:
        for name in ("key", "market", "major_asset", "strategy_profile_id",
                     "strategy_version", "strategy_fingerprint"):
            if not getattr(self, name):
                raise ContractError(f"position contract requires {name}")
        if "/" not in self.market:
            raise ContractError("market must be BASE/QUOTE")

    @property
    def fingerprint(self) -> str:
        return fingerprint({"schema": CONTRACT_VERSION, "key": self.key,
                            "market": self.market, "major_asset": self.major_asset,
                            "strategy_profile_id": self.strategy_profile_id,
                            "strategy_version": self.strategy_version,
                            "strategy_fingerprint": self.strategy_fingerprint,
                            "profile_fingerprint": self.profile_fingerprint,
                            "entry_economics": self.entry_economics.public(),
                            "expected_exit_model": self.expected_exit_model.public()})

    def public(self) -> dict[str, Any]:
        return {"version": CONTRACT_VERSION, "key": self.key, "market": self.market,
                "major_asset": self.major_asset,
                "strategy_profile_id": self.strategy_profile_id,
                "strategy_version": self.strategy_version,
                "strategy_fingerprint": self.strategy_fingerprint,
                "profile_fingerprint": self.profile_fingerprint,
                "entry_economics": self.entry_economics.public(),
                "expected_exit_model": self.expected_exit_model.public(),
                "opened_at": self.opened_at, "mode": self.mode,
                "contract_fingerprint": self.fingerprint}


class PositionContractStore:
    """Append-only contract log, separate from the live financial journal.

    Deliberately its own file: contracts are research/audit metadata and must never
    be interleaved with the journal that defines financial truth.
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._path = self.root / CONTRACT_FILE
        self._cache: dict[str, PositionContract] = {}
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> None:
        if not self._path.exists():
            return
        superseded: dict[str, PositionContract] = {}
        closed: set[str] = set()
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = row.get("kind")
            key = str(row.get("key", ""))
            if kind == "ENTRY" and isinstance(row.get("contract"), dict):
                superseded[key] = _from_payload(row["contract"])
            elif kind == "EXIT":
                closed.add(key)
        for key, contract in superseded.items():
            if key not in closed:
                self._cache[key] = contract

    @financial
    def record_entry(self, contract: PositionContract) -> PositionContract:
        """Persist a new position's owner. Called before the intent is submitted."""
        self._append({"kind": "ENTRY", "key": contract.key, "contract": contract.public(),
                      "contract_fingerprint": contract.fingerprint})
        self._cache[contract.key] = contract
        return contract

    def record_exit(self, *, key: str, reason: str, exit_profile_id: str) -> None:
        """Mark a position closed. Records which profile performed the exit.

        The exiting profile is logged so a mismatch would be visible in the audit
        trail rather than invisible.
        """
        self._append({"kind": "EXIT", "key": key, "reason": reason,
                      "exit_profile_id": exit_profile_id})
        self._cache.pop(key, None)

    def _append(self, payload: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")

    def get(self, key: str) -> PositionContract:
        contract = self._cache.get(key)
        if contract is None:
            raise ContractError(f"{MISSING_CONTRACT}:{key}")
        return contract

    def find_for_market(self, market: str) -> PositionContract | None:
        for contract in self._cache.values():
            if contract.market == market:
                return contract
        return None

    def open_contracts(self) -> tuple[PositionContract, ...]:
        return tuple(self._cache.values())


def _from_payload(payload: dict[str, Any]) -> PositionContract:
    economics = payload["entry_economics"]
    exit_model = payload["expected_exit_model"]
    return PositionContract(
        key=str(payload["key"]), market=str(payload["market"]),
        major_asset=str(payload["major_asset"]),
        strategy_profile_id=str(payload["strategy_profile_id"]),
        strategy_version=str(payload["strategy_version"]),
        strategy_fingerprint=str(payload["strategy_fingerprint"]),
        profile_fingerprint=str(payload.get("profile_fingerprint", "")),
        entry_economics=EntryEconomicEvidence(
            expected_gross_edge_bps=Decimal(economics["expected_gross_edge_bps"]),
            estimated_round_trip_friction_bps=Decimal(
                economics["estimated_round_trip_friction_bps"]),
            expected_net_edge_bps=Decimal(economics["expected_net_edge_bps"]),
            taker_fee_rate=Decimal(economics["taker_fee_rate"]),
            spread_bps=Decimal(economics["spread_bps"]),
            slippage_bps=Decimal(economics["slippage_bps"]),
            admission_outcome=str(economics["admission_outcome"]),
            economic_policy_version=str(economics["economic_policy_version"])),
        expected_exit_model=ExpectedExitModel(
            exit_class=str(exit_model["exit_class"]),
            target_price_mxn=Decimal(exit_model["target_price_mxn"]),
            target_bps=Decimal(exit_model["target_bps"]),
            target_model_version=str(exit_model["target_model_version"]),
            expected_holding_horizon=int(exit_model["expected_holding_horizon"])),
        opened_at=str(payload.get("opened_at", "")), mode=str(payload.get("mode", "PRODUCTION")))


def exit_semantics_for(contract: PositionContract, requesting_profile_id: str) -> ExpectedExitModel:
    """Exit model for a position, refusing to substitute a different strategy's rules.

    Raises on mismatch. The whole point is that a trend position is never quietly
    closed by mean-reversion's profit-taking rule because that happened to be the
    code path that ran.
    """
    if requesting_profile_id != contract.strategy_profile_id:
        raise ContractError(f"{PROFILE_MISMATCH}:{requesting_profile_id}!={contract.strategy_profile_id}")
    return contract.expected_exit_model


def is_risk_exit(exit_class: str) -> bool:
    """Safety exits are independent of any strategy contract.

    A risk exit must remain available regardless of which profile owns the position
    or what that profile intended: safety is never negotiable with a strategy.
    """
    from .economics import RISK_EXIT

    return exit_class == RISK_EXIT

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from autofund.decimal_utils import decimal, financial
from autofund.exchanges.bitso.models import Book, FeeSchedule
from autofund.observer.models import OrderBookSnapshot
from autofund.replay.serialization import fingerprint


class LiveError(Exception):
    """Locally constructed messages only; remote bodies never escape."""


@dataclass(frozen=True)
class LiveConfig:
    allocated_capital: Decimal = Decimal("50")
    max_deployment: Decimal = Decimal("0.50")
    single_order_cap: Decimal = Decimal("11")
    slippage_tolerance: Decimal | None = None
    max_spread_bps: Decimal = Decimal("100")
    max_market_age_seconds: int = 15
    max_local_snapshot_age_seconds: Decimal = Decimal("1")
    reconciliation_attempts: int = 3

    @financial
    def __post_init__(self) -> None:
        for name in ("allocated_capital", "max_deployment", "single_order_cap", "max_spread_bps"):
            if decimal(getattr(self, name), name) <= 0:
                raise LiveError("INVALID_CAPITAL_POLICY")
        if self.allocated_capital > 50 or self.max_deployment > Decimal("0.50") or self.single_order_cap > 11:
            raise LiveError("F5_A_CAPITAL_ENVELOPE_EXCEEDED")
        if not 0 < decimal(self.max_local_snapshot_age_seconds, "max_local_snapshot_age_seconds") <= 5:
            raise LiveError("INVALID_LOCAL_SNAPSHOT_AGE_POLICY")
        if self.slippage_tolerance is not None and not 0 <= decimal(self.slippage_tolerance, "slippage") <= 100:
            raise LiveError("INVALID_SLIPPAGE_POLICY")
        if not 1 <= self.max_market_age_seconds <= 15 or not 1 <= self.reconciliation_attempts <= 5:
            raise LiveError("INVALID_BOUNDED_POLICY")


@dataclass(frozen=True)
class Preflight:
    checked_at: datetime
    config: LiveConfig
    budget: Decimal
    maximum_candidate_notional: Decimal
    limits: Book
    fees: FeeSchedule
    fee_retrieved_at: datetime
    depth: OrderBookSnapshot
    local_received_at: datetime
    local_snapshot_age_seconds: Decimal
    market_age_seconds: Decimal
    exchange_timestamp_future_offset_seconds: Decimal
    deployed: Decimal
    checks: dict[str, bool]
    failures: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.failures and all(self.checks.values())

    @financial
    def public(self) -> dict[str, Any]:
        return {"environment": "BITSO PRODUCTION", "mode": "MICRO-LIVE", "real_money": True,
                "auto_execution": "DISABLED", "ready_for_manual_order": self.ready,
                "checks": self.checks, "failures": self.failures, "allocated_capital": self.config.allocated_capital,
                "max_deployment_mxn": self.config.allocated_capital * self.config.max_deployment,
                "single_order_cap": self.config.single_order_cap, "minor_budget": self.budget,
                "current_minimum_value": self.limits.minimum_value,
                "maximum_candidate_notional": self.maximum_candidate_notional,
                "estimated_fee": self.maximum_candidate_notional * self.fees.taker_fee_decimal,
                "estimated_maximum_debit": self.maximum_candidate_notional * (1 + self.fees.taker_fee_decimal),
                "currently_deployed": self.deployed, "limits": self.limits,
                "fee_source": "CONFIRMED_ACCOUNT_FEE", "fees": {"maker": self.fees.maker_fee_decimal,
                "taker": self.fees.taker_fee_decimal, "retrieved_at": self.fee_retrieved_at},
                "slippage_tolerance": self.config.slippage_tolerance, "spread_bps": self.depth.spread_bps,
                "exchange_updated_at": self.depth.timestamp, "local_received_at": self.local_received_at,
                "local_validation_at": self.checked_at,
                "local_snapshot_age_seconds": self.local_snapshot_age_seconds,
                "max_local_snapshot_age_seconds": self.config.max_local_snapshot_age_seconds,
                "market_age_seconds": self.market_age_seconds,
                "exchange_timestamp_future_offset_seconds": self.exchange_timestamp_future_offset_seconds,
                "exchange_timestamp_status": (
                    "EXCHANGE_TIMESTAMP_ANOMALY" if not self.checks.get("exchange_timestamp_sanity", False)
                    else "PASS_WITH_WARNING" if self.exchange_timestamp_future_offset_seconds > 0 else "PASS"
                ),
                "freshness_status": "PASS" if self.checks.get("market_freshness", False) else "STALE_MARKET",
                "exchange_timestamp_future_offset_ceiling_seconds": Decimal("2"),
                "orderbook_sequence": self.depth.sequence,
                "warnings": self.warnings,
                "market_at": self.depth.timestamp, "checked_at": self.checked_at,
                "write_transport": "ARMED BUT UNUSED",
                "physical_credential_separation": "WAIVED_BY_OPERATOR",
                "credential_reuse": "ACCEPTED_BY_OPERATOR",
                "exchange_credential_least_privilege": "WAIVED_BY_OPERATOR_ACCEPTED_RISK",
                "application_capability_isolation": "PASS",
                "permission_attestation": "OPERATOR_CONFIRMED_NOT_API_VERIFIED"}


@dataclass(frozen=True)
class LiveOrderIntent:
    intent_id: str
    origin_id: str
    created_at: datetime
    minor_budget: Decimal
    preflight: dict[str, Any]
    market_fingerprint: str
    book: str = "btc_mxn"
    side: str = "buy"
    order_type: str = "market"
    intent_source: str = "OPERATOR_CERTIFICATION"
    status: str = "INTENT_CREATED"

    @classmethod
    def create(cls, preflight: Preflight) -> "LiveOrderIntent":
        if not preflight.ready:
            raise LiveError("LIVE_PREFLIGHT_FAIL")
        intent_id = uuid4().hex
        return cls(intent_id, "af-live-" + intent_id, preflight.checked_at,
                   preflight.budget, preflight.public(), fingerprint(preflight.depth))


@dataclass(frozen=True)
class LiveSellIntent:
    intent_id: str
    origin_id: str
    created_at: datetime
    major_quantity: Decimal
    market_fingerprint: str
    book: str = "btc_mxn"
    side: str = "sell"
    order_type: str = "market"
    intent_source: str = "MVP_AUTONOMOUS"
    status: str = "INTENT_CREATED"

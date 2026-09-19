"""GET-only live readiness; exchange account money never funds the F0 wallet."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from time import monotonic

from autofund.capital import CapitalManager
from autofund.decimal_utils import financial
from autofund.risk import RiskEngine
from autofund.wallet import Wallet

from .client import BitsoProductionLiveClient
from .models import LiveConfig, Preflight


@financial
def preflight(client: BitsoProductionLiveClient, config: LiveConfig, wallet: Wallet,
              *, unresolved: bool, budget: Decimal | None = None,
              prior_sequence: int | None = None,
              clock: Callable[[], datetime] = lambda: datetime.now(UTC),
              monotonic_clock: Callable[[], float] = monotonic) -> Preflight:
    def utc_now() -> datetime:
        value = clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("live preflight clock must be timezone-aware")
        return value.astimezone(UTC)

    balances = client.balances()
    fees = client.fees()
    fee_retrieved_at = utc_now()
    limits = client.available_books()
    depth = client.order_book()
    received_monotonic = monotonic_clock()
    received_at = utc_now()
    validation_at = utc_now()
    validation_monotonic = monotonic_clock()
    if validation_at < received_at:
        raise ValueError("live preflight clock moved backwards")
    marks = {"BTC/MXN": depth.best_bid}
    deployed = wallet.deployed_value(marks)
    # Reserve a quote-denominated fee conservatively. No assumption about the
    # exchange's fee currency is allowed to expand the total-debit cap.
    available = min(config.single_order_cap, wallet.cash_mxn,
                    max(Decimal("0"), config.allocated_capital * config.max_deployment - deployed))
    candidate = (available / (1 + fees.taker_fee_decimal)).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
    budget = candidate if budget is None else budget
    debit = budget * (1 + fees.taker_fee_decimal)
    max_price = depth.best_ask * (1 + (config.slippage_tolerance or Decimal("0")) / 100)
    def seconds(value: timedelta) -> Decimal:
        return (Decimal(value.days * 86400 + value.seconds)
                + Decimal(value.microseconds) / Decimal("1000000"))
    future_offset = max(Decimal("0"), seconds(depth.timestamp - received_at))
    age = max(Decimal("0"), seconds(validation_at - depth.timestamp))
    local_age = max(Decimal("0"), Decimal(str(validation_monotonic - received_monotonic)))
    ask_value = sum((x.price * x.amount for x in depth.asks if x.price <= max_price), Decimal("0"))
    mxn = next((x.available for x in balances if x.currency == "mxn"), Decimal("0"))
    checks = {
        "authentication": True, "fees": True,
        "permissions": client.credentials.permissions_confirmed,
        "reconciliation": not unresolved,
        "slippage_policy": config.slippage_tolerance is not None,
        "local_snapshot_freshness": local_age <= config.max_local_snapshot_age_seconds,
        "exchange_timestamp_sanity": future_offset <= Decimal("2"),
        "market_freshness": age <= config.max_market_age_seconds,
        "orderbook_sequence": prior_sequence is None or depth.sequence >= prior_sequence,
        "spread": depth.spread_bps <= config.max_spread_bps,
        "liquidity": ask_value >= budget,
        "price_limits": limits.minimum_price <= depth.best_bid <= max_price <= limits.maximum_price,
        "value_limits": limits.minimum_value <= budget <= limits.maximum_value,
        "amount_limits": budget / max_price >= limits.minimum_amount and budget / depth.best_ask <= limits.maximum_amount,
        "order_cap": 0 < budget <= config.single_order_cap and debit <= config.single_order_cap,
        "capital_envelope": debit <= available,
        "exchange_available": debit <= mxn,
    }
    try:
        RiskEngine(limits.minimum_value).check_buy(wallet, CapitalManager(config.max_deployment),
                                                 "BTC/MXN", debit, depth.best_ask, marks)
        checks["risk"] = True
    except Exception:
        checks["risk"] = False
    failure_names = {
        "local_snapshot_freshness": "LOCAL_SNAPSHOT_STALE",
        "exchange_timestamp_sanity": "EXCHANGE_TIMESTAMP_ANOMALY_GT_2S",
        "market_freshness": "STALE_MARKET",
        "orderbook_sequence": "ORDERBOOK_SEQUENCE_REGRESSION",
        "spread": "SPREAD_GUARD",
        "liquidity": "INSUFFICIENT_DEPTH",
    }
    failures = tuple(failure_names.get(key, key.upper() + "_FAIL")
                     for key, value in checks.items() if not value)
    if not checks["value_limits"] or not checks["amount_limits"]:
        failures += ("MICRO_LIVE_NOT_EXECUTABLE_WITH_CURRENT_CAP",)
    warnings = (("EXCHANGE_TIMESTAMP_FUTURE_OFFSET",) if 0 < future_offset <= Decimal("2") else ())
    return Preflight(validation_at, config, budget, candidate, limits, fees, fee_retrieved_at, depth, received_at,
                     local_age, age, future_offset, deployed, checks, failures, warnings)

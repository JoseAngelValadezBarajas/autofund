import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime
from decimal import Decimal as D
from decimal import localcontext

import pytest

from autofund.replay import (
    ReplayConfig,
    ReplayValidationError,
    SimpleMeanReversionV0,
    StrategyContractError,
    StrategyIdentity,
    canonical_json,
    fingerprint,
)


def test_canonical_json_exact_and_context_independent():
    value = {
        "z": D("-0.000"),
        "b": D("100.000"),
        "a": D("123.456789"),
        "t": datetime(2026, 1, 1, tzinfo=UTC),
    }
    expected = '{"a":"123.456789","b":"100","t":"2026-01-01T00:00:00.000000Z","z":"0"}'
    with localcontext() as context:
        context.prec = 3
        assert canonical_json(value) == expected
    assert fingerprint({"a": D("1.00"), "b": 2}) == fingerprint({"b": 2, "a": D("1")})


@pytest.mark.parametrize(
    "value", [1.0, D("NaN"), D("Infinity"), datetime(2026, 1, 1), object(), {1: "bad"}]
)
def test_noncanonical_types_rejected(value):
    with pytest.raises(ReplayValidationError):
        canonical_json(value)


def test_config_canonical_identity():
    first = ReplayConfig()
    assert (
        first.fingerprint
        == replace(first, initial_equity=D("50.00"), fee_rate=D("0.0010")).fingerprint
    )
    for field, value in (
        ("fee_rate", D("0.002")),
        ("slippage_bps", D("10")),
        ("initial_equity", D("51")),
        ("max_deployment_fraction", D("0.40")),
        ("minimum_order", D("2")),
    ):
        assert first.fingerprint != replace(first, **{field: value}).fingerprint
    # Unsupported policies are rejected, never silently interpreted as the default.
    payload = json.loads(canonical_json(first))
    original = fingerprint(payload)
    payload["execution_policy"] = "same_candle_open"
    assert fingerprint(payload) != original


@pytest.mark.parametrize(
    "changes",
    [
        {"initial_equity": D("0")},
        {"initial_equity": 50.0},
        {"max_deployment_fraction": D("1.1")},
        {"minimum_order": D("0")},
        {"fee_rate": D("1")},
        {"slippage_bps": D("10000")},
        {"fee_rate": D("NaN")},
        {"execution_policy": "same_candle_open"},
        {"end_of_data_policy": "force_liquidate"},
        {"base_currency": "USD"},
        {"schema_version": "unknown"},
    ],
)
def test_invalid_config(changes):
    with pytest.raises(ReplayValidationError):
        ReplayConfig(**changes)


def test_strategy_fingerprint_semantics():
    first = SimpleMeanReversionV0()
    assert (
        first.identity.fingerprint
        == replace(first, entry_threshold=D("0.0500")).identity.fingerprint
    )
    assert first.identity.fingerprint != replace(first, window=4).identity.fingerprint
    assert (
        first.identity.fingerprint
        != replace(first, allocation_fraction=D("0.21")).identity.fingerprint
    )
    assert (
        first.identity.fingerprint
        != replace(first, entry_threshold=D("0.06")).identity.fingerprint
    )
    assert (
        StrategyIdentity("x", "1", (("a", D("1.0")), ("b", 2))).fingerprint
        == StrategyIdentity("x", "1", (("b", 2), ("a", D("1")))).fingerprint
    )
    assert (
        StrategyIdentity("x", "1").fingerprint != StrategyIdentity("x", "2").fingerprint
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"window": 1},
        {"window": 3.0},
        {"window": True},
        {"entry_threshold": D("1")},
        {"allocation_fraction": D("0")},
        {"entry_threshold": 0.05},
    ],
)
def test_invalid_dummy_configuration(changes):
    with pytest.raises(StrategyContractError):
        SimpleMeanReversionV0(**changes)


def test_identity_and_config_are_immutable():
    config = ReplayConfig()
    with pytest.raises(FrozenInstanceError):
        config.fee_rate = D("0")
    with pytest.raises(StrategyContractError):
        StrategyIdentity("x", "1", (("amount", 1.0),))
    with pytest.raises(StrategyContractError):
        StrategyIdentity("x", "1", (("amount", 1), ("amount", 2)))

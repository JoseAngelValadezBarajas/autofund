"""Canonical JSON v1: explicit types only, never repr or pickle."""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

from .errors import ReplayValidationError


def utc_timestamp(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ReplayValidationError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def decimal_string(value: Decimal) -> str:
    if not value.is_finite():
        raise ReplayValidationError("cannot serialize non-finite Decimal")
    if not value:
        return "0"
    # normalize() would round using the caller's Decimal context.
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def canonical_value(value: object) -> object:
    if isinstance(value, Enum):
        return canonical_value(value.value)
    if isinstance(value, Decimal):
        return decimal_string(value)
    if isinstance(value, datetime):
        return (
            utc_timestamp(value)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: canonical_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ReplayValidationError("canonical object keys must be strings")
        return {key: canonical_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical_value(item) for item in value]
    raise ReplayValidationError(f"unsupported canonical type: {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(
        canonical_value(value),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

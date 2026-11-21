"""The history audit must keep catching real credential assignments and must not cry wolf."""

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "history_secret_audit", Path(__file__).parents[2] / "scripts" / "history_secret_audit.py")
assert _SPEC and _SPEC.loader
audit = importlib.util.module_from_spec(_SPEC)
sys.modules["history_secret_audit"] = audit  # dataclasses resolve annotations through sys.modules
_SPEC.loader.exec_module(audit)
PATTERN = audit.PATTERNS["ENV_ASSIGNED_SECRET"]


@pytest.mark.parametrize("line", [
    "ALPACA_API_SECRET=FAKE_NOT_A_REAL_SECRET_0123456789abcdef",
    "API_KEY = FAKEPKABCDEFGHIJKLMNOPQRST",
    "DB_PASSWORD=FAKE-correcthorsebatterystaple",
    "AUTH_TOKEN=FAKE_ghp_abcdefghijklmnopqrstuvwxyz",
])
def test_real_looking_env_assignments_are_still_flagged(line):
    assert PATTERN.search(line)


@pytest.mark.parametrize("line", [
    '_TOKEN = re.compile(r"[A-Za-z0-9_=+/.-]{1,512}\Z")',
    'PAGE_TOKEN = re.compile(r"[a-z]{1,40}")',
    "API_KEY=",
    "TOKEN=short",
])
def test_regex_constants_and_short_values_are_not_flagged(line):
    assert not PATTERN.search(line)

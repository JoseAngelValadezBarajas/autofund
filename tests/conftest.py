from decimal import Decimal as D

import pytest

from autofund import PaperExecutionEngine, Wallet


@pytest.fixture
def wallet():
    result = Wallet()
    result.deposit(D("50"))
    return result


@pytest.fixture
def engine(wallet):
    return PaperExecutionEngine(wallet)


@pytest.fixture(autouse=True)
def _isolated_secret_store(tmp_path, monkeypatch):
    """No test may touch the developer's real OS credential store or ~/.autofund."""
    monkeypatch.setenv("AUTOFUND_SECRETS_BACKEND", "file")
    monkeypatch.setenv("AUTOFUND_SECRETS_DIR", str(tmp_path / "secrets"))

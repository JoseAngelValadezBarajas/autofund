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

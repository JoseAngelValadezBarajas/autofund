from datetime import UTC, datetime

import pytest

from autofund.exchanges.alpaca.paper import AlpacaPaperClient
from autofund.lab.service import LabService

from .helpers import CREDS, FakeBroker, FakeData


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
def data():
    return FakeData()


@pytest.fixture
def clock():
    return {"now": datetime(2026, 11, 23, 15, 0, tzinfo=UTC)}


@pytest.fixture
def service(tmp_path, broker, data, clock):
    ticks = [0.0]
    paper = AlpacaPaperClient(CREDS, transport=broker, sleep=lambda s: None,
                              monotonic=lambda: ticks.__setitem__(0, ticks[0] + 1) or ticks[0])
    svc = LabService(root=tmp_path, credentials=lambda: CREDS, data_client=lambda: data,
                     paper_client=lambda: paper, now=lambda: clock["now"], auto_interval_seconds=0.01)
    svc.session._sleep = lambda s: None
    yield svc
    svc.shutdown()


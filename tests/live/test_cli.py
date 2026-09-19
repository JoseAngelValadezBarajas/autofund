from decimal import Decimal

from autofund.live import cli
from autofund.live.client import BitsoProductionLiveClient, LiveCredentials
from autofund.live.journal import LiveExecutionJournal

from .test_execution import FakeTransport


def setup_cli(monkeypatch, tmp_path):
    fake = FakeTransport()
    client = BitsoProductionLiveClient(LiveCredentials("FAKE", "FAKE", True), transport=fake)
    monkeypatch.setattr(cli.LiveCredentials, "from_environment", lambda: client.credentials)
    monkeypatch.setattr(cli, "BitsoProductionLiveClient", lambda credentials, **kwargs: client)
    monkeypatch.setattr(cli, "LiveExecutionJournal", lambda: LiveExecutionJournal(tmp_path / "live.jsonl"))
    monkeypatch.setattr(cli, "LivePublisher", lambda: DummyPublisher())
    return fake


class DummyPublisher:
    def event(self, *args, **kwargs):
        pass

    def start(self):
        pass

    def close(self):
        pass


def test_preflight_command_only_get_no_browser_no_order_intent(monkeypatch, tmp_path):
    fake = setup_cli(monkeypatch, tmp_path)
    def forbidden(*args, **kwargs):
        raise AssertionError("preflight cannot open browser")
    monkeypatch.setattr(cli, "ensure_dashboard", forbidden)
    assert cli.main(["preflight", "--slippage-tolerance", "0.5"]) == 0
    assert fake.posts == 0
    assert all(call.startswith("GET ") for call in fake.calls)
    assert "LIVE_INTENT_CREATED" not in (tmp_path / "live.jsonl").read_text()


def test_operational_cli_opens_dashboard_exactly_once_and_flag_alone_cannot_submit(monkeypatch, tmp_path):
    fake = setup_cli(monkeypatch, tmp_path)
    openings = []
    def dashboard(session, **kwargs):
        openings.append(kwargs)
        return "http://127.0.0.1:8000"
    monkeypatch.setattr(cli, "ensure_dashboard", dashboard)
    assert cli.main(["certify-buy", "--slippage-tolerance", "0.5", "--minor-budget", str(Decimal("5")), "--confirm-real-money"]) == 2
    assert openings == [{"open_browser": True}]
    assert fake.posts == 0

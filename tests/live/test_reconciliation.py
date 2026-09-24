"""Deterministic tests for bounded ACK reconciliation and its failure semantics.

The virtual clock from the live suite keeps these fast and reproducible: the
production code path is identical, only the clock and sleep are injected.
"""

from decimal import Decimal as D

import pytest

from autofund.live.execution import LiveExecution
from autofund.live.journal import LiveExecutionJournal
from autofund.live.models import (
    MAX_RECONCILIATION_WINDOW_SECONDS,
    LiveConfig,
    LiveError,
)
from live.test_execution import (  # noqa: F401
    BitsoProductionLiveClient,
    FakeTransport,
    LiveCredentials,
    setup,
    trade,
)
from live.test_execution import (
    clock as clock_fixture,
)

# The shared virtual-clock fixture is defined in the live suite and consumed by
# name below; alias the import so the local parameter name stays idiomatic.
clock = clock_fixture

D5 = D("5")


def make(tmp_path, transport, clock, **config):
    """Build an execution with a virtual clock so the window is deterministic."""
    client = BitsoProductionLiveClient(LiveCredentials("FAKE", "FAKE", True), transport=transport)
    settings = {"slippage_tolerance": D("0.5"), **config}
    return LiveExecution(client, LiveExecutionJournal(tmp_path / "live.jsonl"), LiveConfig(**settings),
                         sleep=clock.sleep, monotonic_clock=clock.monotonic)


def test_ack_then_late_trade_visibility_reconciles_within_window(tmp_path, clock):
    """The exchange may ACK before trades are visible; bounded polling recovers it.

    This is the real Production failure mode: the POST succeeded, but the trade
    evidence was not yet queryable when reconciliation ran.
    """
    fake = FakeTransport()
    engine = make(tmp_path, fake, clock)
    intent = engine.create(engine.check(D5))
    fill = trade(intent.origin_id)
    hidden_probes = 3
    probes = {"n": 0}
    posts = {"n": 0}

    def request(method, path, body, authorization, permit=None):
        if method == "POST":
            posts["n"] += 1
            permit.consume(body)  # type: ignore[union-attr]
            return {"oid": "order1"}
        if path.startswith("/api/v3/order_trades?"):
            probes["n"] += 1
            return [fill] if probes["n"] >= hidden_probes else []
        return fake.request(method, path, body, authorization, permit)

    engine.client._transport = type("T", (), {"request": staticmethod(request)})()  # type: ignore[assignment]

    engine.submit_authorized(intent)

    assert engine.states[intent.origin_id] == "RECONCILED"
    assert engine.public()["inventory_btc"] == D("0.000005")
    # Exactly one POST, one ledger entry for the fill, and no duplicate inventory.
    assert posts["n"] == 1
    assert len(engine.wallet.ledger) == 2
    # The window was genuinely exercised with bounded backoff between probes.
    assert probes["n"] >= hidden_probes
    assert clock.sleeps, "bounded backoff must wait between probes"
    assert all(value > 0 for value in clock.sleeps)
    engine.journal.close()


def test_ack_then_never_visible_blocks_without_a_second_post(tmp_path, clock):
    """Unresolved after the bounded window must fail closed and never re-POST."""
    fake = FakeTransport()
    engine = make(tmp_path, fake, clock, reconciliation_window_seconds=D("30"))
    intent = engine.create(engine.check(D5))
    fake.timeout = True  # ACK-shaped failure: POST raises, then no trades ever appear
    engine.submit_authorized(intent)
    assert engine.states[intent.origin_id] == "HALTED"
    assert intent.origin_id in engine.unresolved
    assert fake.posts == 1
    # The window is bounded: total virtual sleep cannot exceed the configured window.
    assert sum(clock.sleeps) <= 30, clock.sleeps
    assert engine.reconciliation_outcomes[intent.origin_id]["outcome"] in {"UNKNOWN", "OPEN_ORDER"}
    engine.journal.close()


def test_explicit_close_outcomes_are_classified(tmp_path, clock):
    """A cancelled or rejected order with no fills resolves deterministically."""
    fake = FakeTransport()
    engine = make(tmp_path, fake, clock)
    intent = engine.create(engine.check(D5))
    fake.timeout = True
    fake.orders = [{"oid": "order1", "origin_id": intent.origin_id, "book": "btc_mxn", "side": "buy",
                    "status": "cancelled", "original_amount": "0.000005", "unfilled_amount": "0.000005",
                    "price": "0"}]
    engine.submit_authorized(intent)
    assert engine.reconciliation_outcomes[intent.origin_id]["outcome"] == "CANCELLED"
    assert engine.states[intent.origin_id] == "RECONCILED"
    assert engine.public()["inventory_btc"] == D("0")
    assert fake.posts == 1
    engine.journal.close()


def test_contradictory_remote_state_is_reported_not_guessed(tmp_path, clock):
    """Remote evidence for another origin/side must not be accepted."""
    fake = FakeTransport()
    engine = make(tmp_path, fake, clock)
    intent = engine.create(engine.check(D5))
    fake.timeout = True
    fake.orders = [{"oid": "order9", "origin_id": "af-live-" + "b" * 32, "book": "btc_mxn", "side": "sell",
                    "status": "open", "original_amount": "0.000005", "unfilled_amount": "0.000005",
                    "price": "0"}]
    engine.submit_authorized(intent)
    assert engine.reconciliation_outcomes[intent.origin_id]["outcome"] == "CONTRADICTORY_REMOTE_STATE"
    assert engine.states[intent.origin_id] == "HALTED"
    assert fake.posts == 1
    engine.journal.close()


def test_recovery_is_idempotent_across_repeated_runs(tmp_path, clock):
    """Repeated recovery must not duplicate fills, ledger entries or inventory."""
    fake = FakeTransport()
    engine = make(tmp_path, fake, clock)
    intent = engine.create(engine.check(D5))
    fake.rows = [trade(intent.origin_id)]
    engine.submit_authorized(intent)
    ledger_after = list(engine.wallet.ledger)
    inventory_after = engine.public()["inventory_btc"]
    for _ in range(4):
        engine.recover(intent.origin_id)
    assert list(engine.wallet.ledger) == list(ledger_after)
    assert engine.public()["inventory_btc"] == inventory_after
    assert fake.posts == 1
    assert engine.states[intent.origin_id] == "RECONCILED"
    engine.journal.close()


def test_recovery_after_restart_is_idempotent_and_never_posts(tmp_path, clock):
    """A restarted process rebuilds state from the journal and never re-submits."""
    fake = FakeTransport()
    engine = make(tmp_path, fake, clock)
    intent = engine.create(engine.check(D5))
    fake.rows = [trade(intent.origin_id)]
    engine.submit_authorized(intent)
    ledger = list(engine.wallet.ledger)
    engine.journal.close()

    restarted = make(tmp_path, fake, clock)
    restarted.recover()
    assert list(restarted.wallet.ledger) == list(ledger)
    assert not restarted.unresolved
    assert fake.posts == 1
    restarted.journal.close()


def test_config_rejects_an_unbounded_reconciliation_window():
    """The window is bounded by policy, independently of configuration."""
    with pytest.raises(LiveError):
        LiveConfig(slippage_tolerance=D("0.5"),
                   reconciliation_window_seconds=MAX_RECONCILIATION_WINDOW_SECONDS + D("1"))
    with pytest.raises(LiveError):
        LiveConfig(slippage_tolerance=D("0.5"), reconciliation_window_seconds=D("-1"))
    # The documented maximum itself is accepted.
    assert LiveConfig(slippage_tolerance=D("0.5"),
                      reconciliation_window_seconds=MAX_RECONCILIATION_WINDOW_SECONDS).reconciliation_window_seconds


def test_blocked_orders_are_distinguishable_from_resolved_ones(tmp_path, clock):
    """`blocked` identifies unknown outcomes so the app can fail closed."""
    fake = FakeTransport()
    engine = make(tmp_path, fake, clock, reconciliation_window_seconds=D("0"))
    intent = engine.create(engine.check(D5))
    fake.timeout = True
    engine.submit_authorized(intent)
    assert engine.blocked or intent.origin_id in engine.unresolved
    assert engine.states[intent.origin_id] == "HALTED"
    engine.journal.close()


def test_recovery_never_uses_a_submission_permit(tmp_path, clock):
    """Recovery is GET-only: no permit is ever created during reconciliation."""
    fake = FakeTransport()
    engine = make(tmp_path, fake, clock)
    intent = engine.create(engine.check(D5))
    fake.timeout = True
    engine.submit_authorized(intent)
    calls = [call for call in fake.calls]
    # Every call after the single submission must be a GET.
    posts = [call for call in calls if call.startswith("POST")]
    assert len(posts) == 1
    assert all(call.startswith("GET") for call in calls[calls.index(posts[0]) + 1:])
    engine.journal.close()

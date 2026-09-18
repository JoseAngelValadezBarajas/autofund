import json
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

import pytest

from autofund.observer.client import BitsoProductionReadOnlyClient, ReadResponse
from autofund.observer.errors import MarketDataInvalid
from autofund.observer.source import BitsoPublicMarketDataSource
from autofund.replay.serialization import canonical_json
from autofund.shadow.capture import ShadowCapture, replay


def trade_wire(trade):
    return {
        "book": trade.book,
        "tid": trade.trade_id,
        "created_at": trade.timestamp.isoformat(),
        "price": str(trade.price),
        "amount": str(trade.amount),
        "maker_side": trade.maker_side,
    }


def depth_wire(depth):
    return {
        "updated_at": depth.timestamp.isoformat(),
        "sequence": str(depth.sequence),
        **{
            side: [
                {"book": depth.book, "price": str(v.price), "amount": str(v.amount)}
                for v in getattr(depth, side)
            ]
            for side in ("bids", "asks")
        },
    }


def test_golden_complete_shadow_session_outbound_get_only(frames, session, tmp_path):
    class Transport:
        def __init__(self):
            self.methods = []
            self.index = 0

        def request(self, method, path, authorization=""):
            assert method == "GET" and not authorization
            self.methods.append(method)
            frame = frames[self.index]
            if path.startswith("/api/v3/trades"):
                payload = [trade_wire(t) for t in frame.trades]
            else:
                assert path.startswith("/api/v3/order_book")
                payload = depth_wire(frame.depth)
                self.index += 1
            return ReadResponse(
                200, json.dumps({"success": True, "payload": payload}).encode()
            )

    transport = Transport()
    client = BitsoProductionReadOnlyClient(transport=transport, sleep=lambda _: None)
    source = BitsoPublicMarketDataSource(client=client)
    capture = ShadowCapture(tmp_path, session)
    try:
        for expected in frames:
            # Fixture observation clock is explicit; financial timestamps remain
            # those parsed from REST, not local wall time.
            polled = source.poll()
            capture.consume(replace(polled, observed_at=expected.observed_at))
        result = capture.finalize("fixture_complete")
    finally:
        capture.close()
    assert set(transport.methods) == {"GET"} and len(transport.methods) == 12
    assert len(result["executions"]) == 2
    assert canonical_json(replay(tmp_path)) == canonical_json(result)


def test_trade_pagination_stuck_fails_closed(frames):
    trade = trade_wire(frames[0].trades[0])

    class Transport:
        def request(self, *args):
            return ReadResponse(
                200, json.dumps({"success": True, "payload": [trade] * 100}).encode()
            )

    source = BitsoPublicMarketDataSource(
        client=BitsoProductionReadOnlyClient(
            transport=Transport(), sleep=lambda _: None
        ),
        marker=1,
    )
    with pytest.raises(MarketDataInvalid, match="advance"):
        source.poll()


def test_public_fields_never_serialize_real_balance(frames, session, tmp_path):
    capture = ShadowCapture(tmp_path, session)
    for frame in frames:
        capture.consume(frame)
    capture.finalize("test")
    capture.close()
    for path in tmp_path.glob("*.json*"):
        content = path.read_text().lower()
        assert all(
            word not in content
            for word in (
                "api_key",
                "api_secret",
                "authorization",
                "real_exchange_balance",
                "account_id",
            )
        )


def test_frozen_golden_fingerprint(session, frames):
    expected = json.loads(
        (Path(__file__).parents[1] / "fixtures/shadow/golden_expected.json").read_text()
    )
    for frame in frames:
        session.process(frame)
    result = session.result()
    assert result["result_fingerprint"] == expected["result_fingerprint"]
    assert result["strategy_fingerprint"] == expected["strategy_fingerprint"]
    assert result["data_fingerprint"] == expected["data_fingerprint"]
    assert result["metrics"]["net_pnl"] == D(expected["net_pnl"])


def test_partial_sell_preserves_f0_basis_arithmetic(limits, fee, frames):
    from autofund.models import Side
    from autofund.replay.strategy import OrderIntent
    from autofund.shadow.config import ShadowConfig
    from autofund.shadow.execution import ShadowExecutionEngine
    from autofund.wallet import Wallet

    wallet = Wallet()
    wallet.deposit(D("50"))
    engine = ShadowExecutionEngine(wallet, ShadowConfig(), limits, fee)
    engine.execute(
        OrderIntent("BTC/MXN", Side.BUY, budget_mxn=D("10")),
        frames[0].depth,
        frames[0].observed_at,
    )
    engine.execute(
        OrderIntent("BTC/MXN", Side.SELL, quantity=D("0.003")),
        frames[1].depth,
        frames[1].observed_at,
    )
    wallet.assert_invariants()

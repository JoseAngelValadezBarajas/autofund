"""Safe artifact reader. It never opens credentials or invokes the trading core."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from autofund.replay.serialization import canonical_json
from autofund.shadow.capture import read_capture, replay

from . import models
from .demo_runtime import DemoRuntime
from .live import project_runtime, read_runtime


def _utc(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value).astimezone(UTC) if value else None


def _string(value: object, default: str = "0") -> str:
    return str(value) if value is not None else default


class DashboardDataProvider:
    """Read-only projection from one complete or currently-appending F4 bundle."""

    def __init__(self, artifacts: Path, *, demo: bool = False, runtime_path: Path | None = None, demo_live: bool = False) -> None:
        self.artifacts = artifacts
        self.demo = demo
        self.demo_runtime = DemoRuntime() if demo_live else None
        self.runtime_path = runtime_path
        self._live: dict[str, Any] | None = None
        self._runtime: dict[str, Any] | None = None
        self._signature: tuple[int, int] | None = None
        self._result: dict[str, Any] = {}
        self._header: dict[str, Any] = {}
        self._frames: list[Any] = []

    def _load_demo(self) -> None:
        fixture = Path(__file__).with_name("fixtures") / "golden_dashboard.json"
        raw = json.loads(fixture.read_text(encoding="utf-8"))
        self._header, self._result, self._frames = raw["header"], raw["result"], []
        if self.demo_runtime:
            self._result["quality"] = self.demo_runtime.snapshot().quality

    def refresh(self) -> None:
        if self.demo:
            self._load_demo()
            return
        manifest = self.artifacts / "manifest.json"
        if self.runtime_path:
            payload = read_runtime(self.runtime_path)
            if payload and "result" in payload:
                self._live = payload
                self._header, self._result = payload["header"], payload["result"]
                return
            if self._live:
                return
        journal = self.artifacts / "shadow_journal.jsonl"
        if not manifest.exists() or not journal.exists():
            raise FileNotFoundError("no complete F4 shadow session found")
        signature = (manifest.stat().st_mtime_ns, journal.stat().st_size)
        if signature == self._signature:
            return
        # F4 reader validates its integrity; completed records only are consumed.
        self._header, self._frames, _ = read_capture(self.artifacts)
        self._result = json.loads(canonical_json(replay(self.artifacts)))
        self._signature = signature

    @property
    def result(self) -> dict[str, Any]:
        self.refresh()
        return self._result

    def _metrics(self) -> dict[str, Any]:
        return dict(self.result["metrics"])

    def _latest_depth(self) -> dict[str, Any] | None:
        self.refresh()
        if self._live:
            return self._live.get("depth")
        if self.demo:
            return self._result.get("depth")
        if not self._frames:
            return None
        frame = self._frames[-1]
        return {"book": frame.depth.book, "timestamp": frame.depth.timestamp.isoformat(), "bids": [x.__dict__ for x in frame.depth.bids], "asks": [x.__dict__ for x in frame.depth.asks]}

    def _candles(self) -> list[dict[str, Any]]:
        return list(self.result.get("candles", []))

    def session_id(self) -> str:
        if self._live:
            return str(self._live["runtime"]["session_id"])
        return str(self.result.get("result_fingerprint", ""))[:16]

    def runtime(self) -> models.RuntimeView:
        if self.demo_runtime:
            return self.demo_runtime.snapshot()
        if self.runtime_path:
            payload = read_runtime(self.runtime_path)
            if payload:
                self._runtime = payload
                return project_runtime(payload["runtime"])
            if self._runtime:
                return project_runtime(self._runtime["runtime"])
        return models.RuntimeView(demo_mode=self.demo)

    def health(self) -> models.Health:
        try:
            result = self.result
        except FileNotFoundError:
            return models.Health(
                market_data_status="INVALID", accounting="UNKNOWN", current_session_id=None
            )
        last = self._live["runtime"].get("last_market_event_at") if self._live else self._frames[-1].observed_at.isoformat() if self._frames else self._header.get("start")
        return models.Health(
            demo_mode=self.demo, market_data_status=result["quality"], accounting=self.runtime().accounting_status if self._live else "PASS", last_market_event_at=_utc(last), current_session_id=self.session_id()
        )

    def overview(self) -> models.Overview:
        result, metrics, config = self.result, self._metrics(), self._header["config"]
        equity = _string(metrics["final_equity"])
        curve = result.get("equity_curve", [])
        deployed = _string(result.get("currently_deployed", "0") if self.demo else curve[-1]["deployed"] if curve else "0")
        max_deploy = _string(Decimal(config["initial_equity"]) * Decimal(config["max_deployment"]))
        return models.Overview(
            demo_mode=self.demo, market=result["book"], strategy_id=result["strategy_fingerprint"], initial_shadow_equity_mxn=_string(metrics["initial_equity"]), current_shadow_equity_mxn=equity,
            realized_pnl_mxn=_string(result.get("realized_pnl", metrics["net_pnl"])), unrealized_pnl_mxn=_string(result.get("unrealized_pnl", "0")), net_pnl_mxn=_string(metrics["net_pnl"]), return_pct=_string(metrics["return"]),
            max_deployment_mxn=max_deploy, currently_deployed_mxn=deployed, available_deployment_mxn=_string(max(Decimal(0), Decimal(max_deploy) - Decimal(deployed))), closed_trade_count=int(metrics["closed_trades"]), winning_trades=int(metrics["wins"]), losing_trades=int(metrics["losses"]), total_fees_mxn=_string(metrics["fees"]), spread_cost_mxn=_string(metrics["spread_cost"]), slippage_cost_mxn=_string(metrics["depth_slippage"]), max_drawdown_pct=_string(metrics["max_drawdown"]), market_quality=result["quality"], risk_status="HALTED" if result.get("halt_reason") else "NORMAL", last_update=self.health().last_market_event_at,
        )

    def portfolio(self) -> models.Portfolio:
        overview, result = self.overview(), self.result
        positions: list[models.Position] = []
        for market, value in result.get("positions", {}).items():
            quantity = _string(value.get("quantity"))
            positions.append(models.Position(market=market, quantity=quantity, average_cost_mxn=_string(value.get("cost_basis_mxn")), mark_price_mxn=None, market_value_mxn="0", unrealized_pnl_mxn="0", realized_pnl_mxn="0"))
        cash = str(Decimal(overview.current_shadow_equity_mxn) - Decimal(overview.currently_deployed_mxn))
        return models.Portfolio(cash_mxn=cash, equity_mxn=overview.current_shadow_equity_mxn, deployed_mxn=overview.currently_deployed_mxn, available_deployment_mxn=overview.available_deployment_mxn, positions=positions)

    def candles(self) -> list[models.Candle]:
        return [models.Candle(timestamp=_utc(c["timestamp"]) or datetime.now(UTC), open=_string(c["open"]), high=_string(c["high"]), low=_string(c["low"]), close=_string(c["close"]), volume=_string(c["volume"])) for c in self._candles()]

    def equity(self) -> list[models.EquityPoint]:
        points = self.result.get("equity_curve", [])
        return [models.EquityPoint(timestamp=_utc(x["timestamp"]) or datetime.now(UTC), equity_mxn=_string(x["equity"])) for x in points]

    def market(self) -> models.Market:
        depth, candles = self._latest_depth(), self.candles()
        bids = depth.get("bids", []) if depth else []
        asks = depth.get("asks", []) if depth else []
        bid, ask = (bids[0].get("price") if bids else None), (asks[0].get("price") if asks else None)
        spread = str(Decimal(str(ask)) - Decimal(str(bid))) if bid and ask else None
        bps = str((Decimal(spread) / ((Decimal(str(ask)) + Decimal(str(bid))) / 2)) * 10000) if spread else None
        return models.Market(market=self.result["book"], last_price_mxn=candles[-1].close if candles else None, best_bid_mxn=_string(bid) if bid else None, best_ask_mxn=_string(ask) if ask else None, spread_mxn=spread, spread_bps=bps, order_book_age_seconds=None, market_data_age_seconds=None, last_closed_candle=candles[-1] if candles else None, market_quality=self.result["quality"])

    def order_book(self) -> models.OrderBook:
        depth = self._latest_depth() or {}
        def convert(levels: list[dict[str, Any]]) -> list[models.OrderLevel]:
            return [models.OrderLevel(price_mxn=_string(x["price"]), amount=_string(x["amount"])) for x in levels[:20]]
        return models.OrderBook(market=self.result["book"], timestamp=_utc(depth.get("timestamp")), bids=convert(depth.get("bids", [])), asks=convert(depth.get("asks", [])))

    def quality(self) -> models.Quality:
        result, issues = self.result, self.result.get("operational_issues", {})
        return models.Quality(status=result["quality"], benchmark_eligible=bool(result["benchmark_candidate"]), stale_snapshots=int(issues.get("stale_orderbook", 0)), out_of_order_trades=int(result.get("out_of_order", 0)), gaps=len(result.get("gaps", [])), duplicate_trades=int(issues.get("duplicate_trades", 0)), contradictions=int(issues.get("contradictions", 0)), reconnects=int(issues.get("reconnects", 0)), invalid_payloads=int(issues.get("invalid_payload", 0)))

    def signals(self) -> list[models.Signal]:
        return [models.Signal(timestamp=_utc(x["candle"]) or datetime.now(UTC), decision=x["decision"], intent=str(x["intent"]) if x.get("intent") else None) for x in self.result.get("signals", [])]

    def ledger(self) -> list[models.LedgerEntry]:
        return [models.LedgerEntry(entry_id=int(x["entry_id"]), type=x["type"], market=x.get("market"), cash_delta_mxn=_string(x["cash_delta_mxn"]), asset_delta=_string(x["asset_delta"]), fee_mxn=_string(x["fee_mxn"]), realized_pnl_mxn=_string(x["realized_pnl_mxn"])) for x in self.result.get("ledger", [])]

    def fills(self) -> list[models.ShadowFill]:
        executions = [{**x, **x.get("fill", {})} for x in self.result.get("executions", [])]
        return [models.ShadowFill(timestamp=_utc(x.get("timestamp")), side=x["side"], market=x["market"], quantity=_string(x["quantity"]), reference_price_mxn=_string(x.get("reference_price")), vwap_execution_price_mxn=_string(x.get("execution_price_mxn", x.get("vwap"))), gross_mxn=_string(x.get("gross_notional_mxn", x.get("gross"))), fee_mxn=_string(x.get("fee_mxn", x.get("fee"))), realized_pnl_mxn=_string(x.get("realized_pnl"))) for x in executions]

    def risk(self) -> models.Risk:
        over, config, limits, fee = self.overview(), self._header["config"], self._header["limits"], self._header["fee"]
        executable = Decimal(config["single_order_cap"]) > Decimal(limits["minimum_value"])
        return models.Risk(status=over.risk_status, initial_capital_mxn=over.initial_shadow_equity_mxn, current_equity_mxn=over.current_shadow_equity_mxn, max_deployment_pct=_string(config["max_deployment"]), max_deployment_mxn=over.max_deployment_mxn, currently_deployed_mxn=over.currently_deployed_mxn, available_deployment_mxn=over.available_deployment_mxn, single_order_cap_mxn=_string(config["single_order_cap"]), minimum_market_value_mxn=_string(limits["minimum_value"]), fee_rate=_string(fee["rate"]), potential_executability="EXECUTABLE" if executable else "MICRO_ORDER_NOT_EXECUTABLE")

    def activity(self) -> list[models.Activity]:
        runtime = self.runtime()
        if runtime.session_id:
            return [models.Activity(timestamp=x.timestamp, event_type=x.event_type, market=x.market, detail=x.summary) for x in reversed(runtime.events)]
        events: list[models.Activity] = [models.Activity(timestamp=x.timestamp, event_type="CLOSED_CANDLE", market=self.result["book"], detail=f"close {x.close}") for x in self.candles()]
        events += [models.Activity(timestamp=x.timestamp, event_type="SIGNAL", market=self.result["book"], detail=x.decision) for x in self.signals()]
        events += [models.Activity(timestamp=None, event_type="QUALITY", detail=self.quality().status)]
        return sorted(events, key=lambda x: x.timestamp or datetime.min.replace(tzinfo=UTC), reverse=True)

    def session(self) -> models.SessionDetail:
        r, m = self.result, self._metrics()
        return models.SessionDetail(session_id=self.session_id(), start=_utc(self._header["start"]) or datetime.now(UTC), end=self.health().last_market_event_at, quality=r["quality"], closed_candles=len(r["candles"]), signals=len(r["signals"]), fills=len(r["executions"]), initial_equity_mxn=_string(m["initial_equity"]), final_equity_mxn=_string(m["final_equity"]), net_pnl_mxn=_string(m["net_pnl"]), result_fingerprint=r["result_fingerprint"], data_fingerprint=r["data_fingerprint"], strategy_fingerprint=r["strategy_fingerprint"], config_fingerprint=r["config_fingerprint"])

    def report(self) -> models.Report:
        s, m = self.session(), self._metrics()
        return models.Report(period_start=s.start, period_end=s.end, initial_equity_mxn=s.initial_equity_mxn, final_equity_mxn=s.final_equity_mxn, return_pct=_string(m["return"]), net_pnl_mxn=s.net_pnl_mxn, fees_mxn=_string(m["fees"]), closed_trades=int(m["closed_trades"]), wins=int(m["wins"]), losses=int(m["losses"]), max_drawdown_pct=_string(m["max_drawdown"]), quality=s.quality)

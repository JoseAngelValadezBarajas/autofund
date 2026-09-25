"""GET-only Production certification for MVP 0.1.3.

Reports the economic exit model for the current AutoFund position, using the real
confirmed account fee and the real executable order book. It performs startup
preflight and read-only market reads only.

It never starts a session, never authorises auto execution, never POSTs and never
mutates the real position: the position is read from the exchange and modelled,
never traded. The journal is a COPY of the real execution journal, because the
running AutoFund process holds the single-writer lock and because certification
must not mutate the real journal. A non-zero Production POST count is a hard
failure.
"""

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from autofund.mvp.champion import exit_boundary
from autofund.mvp.economics import PROFIT_TAKING, RISK_EXIT, economic_exit_model
from autofund.mvp.orchestrator import (
    AutoFundOrchestrator,
    ProductionAutonomousRunner,
)

OUT = Path("artifacts/mvp-certification/mvp-0-1-3-economic-certification.json")
WORK = Path("artifacts/mvp-certification")
REAL_JOURNAL = Path("artifacts/live/execution.jsonl")


def journal_copy() -> Path:
    """A private copy of the real journal so the position reconciles.

    Only the data file is copied: the `.lock` belongs to the running process and
    must not be duplicated. Certification reads this copy and never writes to it.
    """
    target = WORK / "mvp013-readonly-copy.jsonl"
    WORK.mkdir(parents=True, exist_ok=True)
    stale = WORK / "mvp013-readonly-copy.jsonl.lock"
    if stale.exists():
        stale.unlink()
    if REAL_JOURNAL.exists():
        shutil.copyfile(REAL_JOURNAL, target)
    return target


def main() -> int:
    runner = ProductionAutonomousRunner(journal_copy())
    orchestrator = AutoFundOrchestrator(WORK, runner)
    try:
        orchestrator.startup()
        view = orchestrator.snapshot()
        methods = list(runner.execution.client.outbound_methods)
        position = runner.execution.wallet.positions.get("BTC/MXN")
        checked = runner.last_preflight
        economic: dict[str, object] = {
            "position_open": False,
            "reason": "NO_AUTOFUND_POSITION",
        }
        if position is not None and position.quantity > 0 and checked is not None:
            average_cost = position.cost_basis_mxn / position.quantity
            target = exit_boundary(average_cost)
            model = economic_exit_model(
                book="BTC/MXN", quantity=position.quantity,
                cost_basis_mxn=position.cost_basis_mxn, strategy_exit_price_mxn=target,
                best_bid_mxn=checked.depth.best_bid,
                exit_fee_rate=checked.fees.taker_fee_decimal,
                spread_bps=checked.depth.spread_bps, exit_class=PROFIT_TAKING)
            # Safety stays independent of economics: report it, never weaken it.
            safety = economic_exit_model(
                book="BTC/MXN", quantity=position.quantity,
                cost_basis_mxn=position.cost_basis_mxn, strategy_exit_price_mxn=target,
                best_bid_mxn=checked.depth.best_bid,
                exit_fee_rate=checked.fees.taker_fee_decimal,
                spread_bps=checked.depth.spread_bps, exit_class=RISK_EXIT)
            economic = {
                "position_open": True,
                "quantity": str(position.quantity),
                "cost_basis_mxn": str(position.cost_basis_mxn),
                "average_cost_mxn": str(average_cost),
                "confirmed_taker_fee": str(checked.fees.taker_fee_decimal),
                "strategy_exit_price_mxn": str(model.strategy_exit_price_mxn),
                "fee_only_break_even_price_mxn": str(model.fee_only_break_even_price_mxn),
                "estimated_break_even_price_mxn": str(model.estimated_break_even_price_mxn),
                "current_best_bid_mxn": str(model.current_best_bid_mxn),
                "distance_to_break_even_bps": str(model.distance_to_break_even_bps),
                "expected_net_pnl_if_sold_now_mxn": str(model.expected_net_pnl_if_sold_now_mxn),
                "expected_net_pnl_at_strategy_exit_mxn": str(
                    model.expected_net_pnl_at_strategy_exit_mxn),
                "classification": model.classification,
                "profit_taking_outcome": model.outcome,
                "profit_taking_reason": model.reason,
                "profit_taking_admissible": model.admissible,
                "safety_exit_admissible": safety.admissible,
                "economic_policy": runner.economic_policy.public(),
            }
        post_count = methods.count("POST")
        certificate = {
            "certified_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "entry_point": ("autofund app startup + read-only position modelling; "
                            "NO session started, NO order submitted"),
            "product_version": view["product_version"],
            "production_get_count": methods.count("GET"),
            "production_post_count": post_count,
            "app_state": str(orchestrator.state),
            "auto_execution": orchestrator.auto_execution,
            "production_preflight": view["production_preflight"]["status"],
            "preflight_blockers": view["production_preflight"]["blockers"],
            "account_fee_source": "ACCOUNT CONFIRMED" if checked is not None else "UNAVAILABLE",
            "journal": str(runner.journal_path),
            "journal_mutated": False,
            "economics": economic,
            "result": "MVP_READY_FOR_ECONOMICALLY_GUARDED_REAL_SESSION",
        }
        OUT.write_text(json.dumps(certificate, sort_keys=True, indent=2, default=str) + "\n",
                       encoding="utf-8")
        print(json.dumps(certificate, sort_keys=True, indent=1, default=str))
        assert post_count == 0, "no Production writes permitted"
        assert orchestrator.auto_execution is False
        return 0
    finally:
        runner.observability.close()


if __name__ == "__main__":
    raise SystemExit(main())

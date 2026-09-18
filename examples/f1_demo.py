"""Synthetic replay, deliberately not optimized for profit."""

import argparse
from decimal import Decimal
from pathlib import Path

from autofund.replay import ReplayConfig, ReplayRunner, SimpleMeanReversionV0, load_csv


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON audit artifact")
    args = parser.parse_args()
    dataset = load_csv(
        Path(__file__).parent / "data" / "mean_reversion_market.csv", market="BTC/MXN"
    )
    config = ReplayConfig(fee_rate=Decimal("0.01"), slippage_bps=Decimal("100"))
    strategy = SimpleMeanReversionV0(allocation_fraction=Decimal("0.20402"))
    result = ReplayRunner(config).run(dataset=dataset, strategy=strategy)
    print("F1 IS NOT A PROFITABLE TRADING SYSTEM.")
    print("Run:", result.run_id)
    print("Initial equity:", result.metrics.initial_equity_mxn)
    print("Final equity:", result.metrics.final_equity_mxn)
    print("Net P&L:", result.metrics.net_pnl_mxn)
    print("Fees:", result.metrics.total_fees_mxn)
    print("Estimated slippage:", result.metrics.estimated_slippage_cost_mxn)
    print(
        "Fills / closed trades:",
        result.metrics.fill_count,
        result.metrics.closed_trade_count,
    )
    print("Result fingerprint:", result.result_fingerprint)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result.to_json(args.output)
        print("Audit artifact written.")


if __name__ == "__main__":
    main()

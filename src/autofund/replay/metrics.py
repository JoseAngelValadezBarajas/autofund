from dataclasses import dataclass
from decimal import Context, Decimal, localcontext

from autofund.decimal_utils import ONE, ZERO, financial

from .errors import ReplayValidationError
from .records import EquityPoint, ReplayTrace


def exact_sum(values: tuple[Decimal, ...]) -> Decimal:
    """Finite Decimal addition with enough digits to avoid aggregate rounding."""
    if any(not isinstance(value, Decimal) or not value.is_finite() for value in values):
        raise ReplayValidationError("exact_sum requires finite Decimal values")
    nonzero = tuple(value for value in values if value)
    if not nonzero:
        return ZERO
    exponents: list[int] = []
    for value in nonzero:
        exponent = value.as_tuple().exponent
        assert isinstance(exponent, int)  # Guaranteed by the finite check above.
        exponents.append(exponent)
    precision = (
        max(value.adjusted() for value in nonzero)
        - min(exponents)
        + len(str(len(nonzero)))
        + 2
    )
    with localcontext(Context(prec=max(50, precision))):
        return sum(nonzero, ZERO)


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    initial_equity_mxn: Decimal
    final_equity_mxn: Decimal
    realized_pnl_mxn: Decimal
    unrealized_pnl_mxn: Decimal
    net_pnl_mxn: Decimal
    accounting_rounding_mxn: Decimal
    total_fees_mxn: Decimal
    estimated_slippage_cost_mxn: Decimal
    cost_addback_pnl_mxn: Decimal
    return_fraction: Decimal
    return_pct: Decimal
    fill_count: int
    closed_trade_count: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    max_drawdown_mxn: Decimal
    max_drawdown_pct: Decimal
    max_deployed_mxn: Decimal
    max_deployed_fraction: Decimal
    ledger_entry_count: int


@financial
def drawdown(
    initial_equity: Decimal, curve: tuple[EquityPoint, ...]
) -> tuple[Decimal, Decimal]:
    """Absolute and percentage maxima can belong to different peak/trough pairs."""
    peak = initial_equity
    maximum = ZERO
    maximum_pct = ZERO
    for point in curve:
        peak = max(peak, point.equity_mxn)
        drop = peak - point.equity_mxn
        maximum = max(maximum, drop)
        maximum_pct = max(maximum_pct, drop / peak * Decimal("100"))
    return maximum, maximum_pct


@financial
def calculate_metrics(
    initial_equity: Decimal, trace: ReplayTrace
) -> PerformanceMetrics:
    final = trace.equity_curve[-1]
    net = final.equity_mxn - initial_equity
    # Sum the ledger, not an independent reconstruction of fills.
    fees = exact_sum(tuple(entry.fee_mxn for entry in trace.ledger))
    slippage = exact_sum(
        tuple(trade.estimated_slippage_cost_mxn for trade in trace.trades)
    )
    rounding = exact_sum((net, -final.realized_pnl_mxn, -final.unrealized_pnl_mxn))
    maximum, maximum_pct = drawdown(initial_equity, trace.equity_curve)
    return_fraction = final.equity_mxn / initial_equity - ONE
    return PerformanceMetrics(
        initial_equity,
        final.equity_mxn,
        final.realized_pnl_mxn,
        final.unrealized_pnl_mxn,
        net,
        rounding,
        fees,
        slippage,
        exact_sum((net, fees, slippage)),
        return_fraction,
        return_fraction * Decimal("100"),
        len(trace.trades),
        len(trace.closed_trades),
        sum(trade.realized_pnl_mxn > ZERO for trade in trace.closed_trades),
        sum(trade.realized_pnl_mxn < ZERO for trade in trace.closed_trades),
        sum(trade.realized_pnl_mxn == ZERO for trade in trace.closed_trades),
        maximum,
        maximum_pct,
        max(point.deployed_value_mxn for point in trace.equity_curve),
        max(
            point.deployed_value_mxn / point.equity_mxn for point in trace.equity_curve
        ),
        len(trace.ledger),
    )

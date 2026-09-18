from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType

from .decimal_utils import ZERO, decimal, financial
from .errors import AccountingInvariantError, InvalidFinancialInput
from .models import EntryType, Fill, LedgerEntry, Position, Side


class Wallet:
    """Accounting owner. Public views are immutable snapshots; fills are internal."""

    def __init__(self) -> None:
        self._cash = ZERO
        self._positions: dict[str, Position] = {}
        self._ledger: tuple[LedgerEntry, ...] = ()

    @property
    def cash_mxn(self) -> Decimal:
        return self._cash

    @property
    def positions(self) -> Mapping[str, Position]:
        return MappingProxyType(self._positions.copy())

    @property
    def ledger(self) -> tuple[LedgerEntry, ...]:
        return self._ledger

    @financial
    def deposit(self, amount: Decimal, note: str = "") -> None:
        decimal(amount, "deposit")
        if amount <= ZERO:
            raise InvalidFinancialInput("deposit must be positive")
        self.assert_invariants()
        entry = LedgerEntry(len(self._ledger) + 1, EntryType.DEPOSIT, amount, note=note)
        self._commit(entry, self._positions.copy())

    @financial
    def deployed_value(self, marks: Mapping[str, Decimal]) -> Decimal:
        self.assert_invariants()
        total = ZERO
        for market, pos in self._positions.items():
            if not pos.quantity:
                continue
            if market not in marks:
                raise InvalidFinancialInput(f"missing mark for {market}")
            mark = decimal(marks[market], "mark")
            if mark <= ZERO:
                raise InvalidFinancialInput("marks must be positive")
            total += pos.quantity * mark
        return total

    @financial
    def equity(self, marks: Mapping[str, Decimal]) -> Decimal:
        return self._cash + self.deployed_value(marks)

    @financial
    def realized_pnl(self) -> Decimal:
        self.assert_invariants()
        return sum((p.realized_pnl_mxn for p in self._positions.values()), ZERO)

    @financial
    def assert_invariants(self, marks: Mapping[str, Decimal] | None = None) -> None:
        try:
            self._check(self._cash, self._positions, self._ledger)
        except AccountingInvariantError:
            raise
        except Exception as exc:
            raise AccountingInvariantError("malformed accounting state") from exc
        if marks is not None:
            self.equity(marks)  # Also validates completeness of live marks.

    @staticmethod
    def _check(
        cash: Decimal,
        positions: Mapping[str, Position],
        ledger: tuple[LedgerEntry, ...],
    ) -> None:
        if not isinstance(cash, Decimal) or not cash.is_finite() or cash < ZERO:
            raise AccountingInvariantError("invalid cash")
        running = ZERO
        replay: dict[str, Position] = {}
        for index, entry in enumerate(ledger, 1):
            if entry.entry_id != index:
                raise AccountingInvariantError("ledger sequence mismatch")
            running += entry.cash_delta_mxn
            if running < ZERO:
                raise AccountingInvariantError("negative historical cash")
            if entry.type == EntryType.DEPOSIT:
                if (
                    entry.cash_delta_mxn <= ZERO
                    or entry.market is not None
                    or any(
                        (
                            entry.asset_delta,
                            entry.fee_mxn,
                            entry.realized_pnl_mxn,
                            entry.cost_basis_delta_mxn,
                            entry.rounding_adjustment_mxn,
                        )
                    )
                ):
                    raise AccountingInvariantError("invalid deposit entry")
                continue
            if entry.market is None or entry.fee_mxn < ZERO:
                raise AccountingInvariantError("invalid trade entry")
            old = replay.get(entry.market, Position(entry.market))
            if entry.type == EntryType.BUY:
                if (
                    entry.asset_delta <= ZERO
                    or entry.cash_delta_mxn >= ZERO
                    or entry.cost_basis_delta_mxn != -entry.cash_delta_mxn
                    or entry.realized_pnl_mxn != ZERO
                ):
                    raise AccountingInvariantError("invalid buy entry")
            elif entry.type == EntryType.SELL:
                if (
                    entry.asset_delta >= ZERO
                    or -entry.asset_delta > old.quantity
                    or entry.cash_delta_mxn < ZERO
                ):
                    raise AccountingInvariantError("invalid sell entry")
                removed = (
                    old.cost_basis_mxn
                    if -entry.asset_delta == old.quantity
                    else old.cost_basis_mxn * (-entry.asset_delta / old.quantity)
                )
                if (
                    entry.cost_basis_delta_mxn != -removed
                    or entry.realized_pnl_mxn != entry.cash_delta_mxn - removed
                ):
                    raise AccountingInvariantError("sell cost/P&L mismatch")
            else:
                raise AccountingInvariantError("unknown entry type")
            replay[entry.market] = Position(
                entry.market,
                old.quantity + entry.asset_delta,
                old.cost_basis_mxn + entry.cost_basis_delta_mxn,
                old.realized_pnl_mxn + entry.realized_pnl_mxn,
            )
        if cash != running or dict(positions) != replay:
            raise AccountingInvariantError(
                "cash or positions do not reconcile to ledger"
            )

    def _commit(self, entry: LedgerEntry, positions: dict[str, Position]) -> None:
        cash = self._cash + entry.cash_delta_mxn
        if entry.cash_delta_mxn != ZERO and cash == self._cash:
            raise InvalidFinancialInput(
                "cash movement is below representable precision"
            )
        ledger = (*self._ledger, entry)
        self._check(cash, positions, ledger)
        self._cash, self._positions, self._ledger = cash, positions, ledger

    def _apply_fill(self, fill: Fill, cost_removed: Decimal = ZERO) -> None:
        """Private execution boundary; never exposed to strategies."""
        self.assert_invariants()
        old = self._positions.get(fill.market, Position(fill.market))
        buying = fill.side == Side.BUY
        asset_delta = fill.quantity if buying else -fill.quantity
        basis_delta = -fill.cash_delta_mxn if buying else -cost_removed
        positions = self._positions.copy()
        positions[fill.market] = Position(
            fill.market,
            old.quantity + asset_delta,
            old.cost_basis_mxn + basis_delta,
            old.realized_pnl_mxn + fill.realized_pnl_mxn,
        )
        if positions[fill.market].quantity == old.quantity:
            raise InvalidFinancialInput(
                "asset movement is below representable precision"
            )
        self._commit(
            LedgerEntry(
                len(self._ledger) + 1,
                EntryType(fill.side.value),
                fill.cash_delta_mxn,
                fill.market,
                asset_delta,
                fill.fee_mxn,
                fill.realized_pnl_mxn,
                "immediate paper fill",
                basis_delta,
                fill.rounding_adjustment_mxn,
            ),
            positions,
        )

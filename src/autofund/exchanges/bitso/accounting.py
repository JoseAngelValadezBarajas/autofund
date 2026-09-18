from decimal import Decimal

from autofund.decimal_utils import financial
from autofund.models import Fill, Side
from autofund.wallet import Wallet

from .errors import ExchangeInvariantError
from .models import ExchangeTradeFill


class ConfirmedFillAccounting:
    """Only confirmed settlements enter F0. No fee estimate is booked."""

    def __init__(self, wallet: Wallet) -> None:
        self.wallet = wallet
        self.processed_fill_ids: dict[str, ExchangeTradeFill] = {}

    @financial
    def apply(self, remote: ExchangeTradeFill) -> bool:
        old = self.processed_fill_ids.get(remote.trade_id)
        if old is not None:
            if old != remote:
                raise ExchangeInvariantError("contradictory duplicate fill")
            return False
        base, quote = remote.book.split("_")
        if (
            quote != "mxn"
            or remote.confirmed_fee is None
            or remote.fee_currency not in (base, quote)
        ):
            raise ExchangeInvariantError(
                "confirmed fee/currency required for F0 accounting"
            )
        fee = remote.confirmed_fee
        base_fee = fee if remote.fee_currency == base else Decimal("0")
        quote_fee = fee if remote.fee_currency == quote else Decimal("0")
        buying = remote.side is Side.BUY
        quantity = (
            remote.major_quantity - base_fee
            if buying
            else remote.major_quantity + base_fee
        )
        market = remote.book.replace("_", "/").upper()
        cash = (
            -remote.minor_value - quote_fee
            if buying
            else remote.minor_value - quote_fee
        )
        removed = Decimal("0")
        if not buying:
            position = self.wallet.positions.get(market)
            if position is None or quantity > position.quantity:
                raise ExchangeInvariantError(
                    "confirmed sell exceeds allocated position"
                )
            removed = (
                position.cost_basis_mxn
                if quantity == position.quantity
                else position.cost_basis_mxn * quantity / position.quantity
            )
        fill = Fill(
            market,
            remote.side,
            quantity,
            remote.price,
            remote.minor_value,
            quote_fee + base_fee * remote.price,
            cash,
            Decimal("0") if buying else cash - removed,
        )
        self.wallet._apply_fill(fill, removed)
        self.processed_fill_ids[remote.trade_id] = remote
        return True

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from autofund.decimal_utils import financial
from autofund.models import Side

from .errors import BitsoValidationError
from .models import (
    BitsoOrderRequest,
    Book,
    ExecutionInstruction,
    ExecutionPolicy,
    FeeSchedule,
    OrderType,
)


@dataclass(frozen=True)
class PreparedOrder:
    instruction: ExecutionInstruction
    request: BitsoOrderRequest
    reserved_mxn: Decimal
    estimated_fee_mxn: Decimal
    adjustments: tuple[str, ...]


class ExchangePrecisionPolicy:
    """Price uses live tick. Amounts derived from budgets use eight decimals.

    No invented lot step: requested SELL amounts are preserved, not quantized.
    Derived BUY quantities use a conservative precision ceiling, recorded below.
    Server validation remains authoritative for undocumented amount precision.
    """

    @financial
    def prepare(
        self,
        instruction: ExecutionInstruction,
        book: Book,
        fees: FeeSchedule,
        policy: ExecutionPolicy,
        origin: str,
    ) -> PreparedOrder:
        intent = instruction.intent
        if (
            intent.market != book.book.replace("_", "/").upper()
            or book.minor_currency != "mxn"
        ):
            raise BitsoValidationError("F3 execution requires matching BASE/MXN")
        if fees.book != book.book:
            raise BitsoValidationError("fee schedule belongs to another book")
        rate = max(fees.maker_fee_decimal, fees.taker_fee_decimal)
        changes = []
        price = instruction.limit_price
        major, minor = intent.quantity, None
        if price is not None:
            rounding = ROUND_FLOOR if intent.side is Side.BUY else ROUND_CEILING
            normalized = (price / book.tick_size).to_integral_value(
                rounding=rounding
            ) * book.tick_size
            if normalized != price:
                changes.append(f"price:{price}->{normalized};tick={book.tick_size}")
            price = normalized
            if intent.side is Side.BUY:
                assert intent.budget_mxn is not None
                derived = (
                    intent.budget_mxn / (Decimal("1") + rate) / price
                    if price > 0
                    else Decimal("0")
                )
                major = derived.quantize(Decimal("0.00000001"), rounding=ROUND_FLOOR)
                changes.append(
                    f"budget:{intent.budget_mxn}->major:{major};fee_reserve={rate};derived_precision=8"
                )
        elif intent.side is Side.BUY:
            assert intent.budget_mxn is not None
            # Quote budget goes directly to minor, never through a stale BTC price.
            minor = (intent.budget_mxn / (Decimal("1") + rate)).quantize(
                Decimal("0.00000001"), rounding=ROUND_FLOOR
            )
            changes.append(
                f"budget:{intent.budget_mxn}->minor:{minor};fee_reserve={rate}"
            )
        request = BitsoOrderRequest(
            book.book,
            intent.side,
            instruction.order_type,
            origin,
            major,
            minor,
            price,
            instruction.post_only,
            policy.slippage_tolerance
            if instruction.order_type is OrderType.MARKET
            else None,
        )
        self.validate(request, book)
        notional = major * price if major is not None and price is not None else minor
        estimated = notional * rate if notional is not None else Decimal("0")
        reserve = (
            notional + estimated
            if notional is not None and intent.side is Side.BUY
            else Decimal("0")
        )
        return PreparedOrder(instruction, request, reserve, estimated, tuple(changes))

    @financial
    def validate(self, request: BitsoOrderRequest, book: Book) -> None:
        value: Decimal | None
        if request.book != book.book:
            raise BitsoValidationError("metadata book mismatch")
        if (
            request.major is not None
            and not book.minimum_amount <= request.major <= book.maximum_amount
        ):
            raise BitsoValidationError("amount outside exchange limits")
        if request.price is not None:
            if not book.minimum_price <= request.price <= book.maximum_price:
                raise BitsoValidationError("price outside exchange limits")
            if request.price % book.tick_size:
                raise BitsoValidationError("price violates tick size")
            assert request.major is not None
            value = request.major * request.price
        else:
            value = request.minor
        if value is not None and not book.minimum_value <= value <= book.maximum_value:
            raise BitsoValidationError("value outside exchange limits")

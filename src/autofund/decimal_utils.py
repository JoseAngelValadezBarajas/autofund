from collections.abc import Callable
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from functools import wraps

from .errors import InvalidFinancialInput

ZERO = Decimal("0")
ONE = Decimal("1")
PRECISION = 50


def financial[**P, T](fn: Callable[P, T]) -> Callable[P, T]:
    """Isolate arithmetic from caller precision, rounding, and traps."""

    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)):
            return fn(*args, **kwargs)

    return wrapped


def decimal(value: Decimal, name: str) -> Decimal:
    if not isinstance(value, Decimal):
        raise InvalidFinancialInput(f"{name} must be Decimal, never float/int")
    if not value.is_finite():
        raise InvalidFinancialInput(f"{name} must be finite")
    # Bound magnitudes to keep F0 computations away from Decimal overflow.
    if value and not -100 <= value.adjusted() <= 100:
        raise InvalidFinancialInput(f"{name} magnitude outside F0 range")
    if len(value.as_tuple().digits) > PRECISION:
        raise InvalidFinancialInput(f"{name} exceeds {PRECISION} significant digits")
    return value


def market_name(market: str) -> str:
    if not isinstance(market, str):
        raise InvalidFinancialInput("market must be BASE/MXN")
    parts = market.split("/")
    if (
        len(parts) != 2
        or parts[1] != "MXN"
        or not parts[0].isascii()
        or not parts[0].isalnum()
        or parts[0] != parts[0].upper()
        or parts[0] == "MXN"
    ):
        raise InvalidFinancialInput("market must be uppercase BASE/MXN")
    return market

class BitsoError(Exception):
    """Only locally constructed, sanitized messages cross this boundary."""


class BitsoAuthenticationError(BitsoError):
    pass


class BitsoPermissionError(BitsoAuthenticationError):
    pass


class BitsoValidationError(BitsoError):
    pass


class BitsoRateLimitError(BitsoError):
    pass


class BitsoInsufficientFundsError(BitsoError):
    pass


class BitsoOrderNotFoundError(BitsoError):
    pass


class BitsoTransportError(BitsoError):
    pass


class BitsoUnknownSubmissionOutcome(BitsoError):
    pass


class ExchangeInvariantError(BitsoError):
    pass


class ExecutionHalted(BitsoError):
    pass

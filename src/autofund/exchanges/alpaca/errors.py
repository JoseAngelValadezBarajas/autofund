class AlpacaError(Exception):
    """Only locally constructed, sanitized messages cross this boundary.

    Messages never contain a key, a secret, a request header or any part of a response body.
    """


class AlpacaAuthenticationError(AlpacaError):
    pass


class AlpacaValidationError(AlpacaError):
    pass


class AlpacaRateLimitError(AlpacaError):
    pass


class AlpacaTransportError(AlpacaError):
    pass


class AlpacaParseError(AlpacaError):
    pass


class AlpacaUnknownSubmissionOutcome(AlpacaError):
    """A write may or may not have reached the broker; reconcile by client order id."""


class AlpacaOrderRejected(AlpacaError):
    pass


class AlpacaSubscriptionError(AlpacaError):
    """The data plan does not permit this request (for example, very recent SIP data)."""

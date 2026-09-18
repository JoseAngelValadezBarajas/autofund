class ObserverError(Exception):
    pass


class ReadOnlyViolation(ObserverError):
    pass


class AuthenticationUnavailable(ObserverError):
    pass


class StrictReadOnlyLimitation(ObserverError):
    pass


class MarketDataInvalid(ObserverError):
    pass


class ReadUnavailable(ObserverError):
    pass

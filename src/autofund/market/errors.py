from autofund.errors import AutoFundError


class MarketDataError(AutoFundError):
    """F2 public observation error; never a financial execution error."""


class ConfigurationError(MarketDataError):
    pass


class InvalidMessage(MarketDataError):
    pass


class WrongStream(InvalidMessage):
    pass


class CaptureIntegrityError(MarketDataError):
    pass


class UnsupportedQuote(MarketDataError):
    pass


class NetworkUnavailable(MarketDataError):
    pass

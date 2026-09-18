from autofund.errors import AutoFundError


class DatasetValidationError(AutoFundError):
    """Local candle data does not satisfy the replay schema."""


class ReplayValidationError(AutoFundError):
    """Invalid replay configuration, clock transition, or serialization."""


class StrategyContractError(AutoFundError):
    """Strategy does not satisfy the deterministic callback contract."""

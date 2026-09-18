class AutoFundError(Exception):
    """Base domain error."""


class AccountingInvariantError(AutoFundError):
    """Accounting state cannot be reconciled."""


class RiskRejected(AutoFundError):
    """Order violates a risk rule."""


class InsufficientFunds(RiskRejected):
    """Order exceeds available cash."""


class InvalidFinancialInput(AutoFundError):
    """Invalid decimal, market, mark, or configuration."""

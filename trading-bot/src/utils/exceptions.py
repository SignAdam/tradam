"""Project-specific exceptions."""


class TradingBotError(Exception):
    """Base exception for recoverable trading-bot errors."""


class SafetyError(TradingBotError):
    """Raised when a safety guard blocks execution."""


class MT5ConnectionError(TradingBotError):
    """Raised when MetaTrader 5 cannot be initialized or queried."""


class DataUnavailableError(TradingBotError):
    """Raised when required market data is missing or unusable."""


class BrokerValidationError(TradingBotError):
    """Raised when an order fails broker-side pre-validation."""


class ConfigurationError(TradingBotError):
    """Raised when configuration is incomplete or unsafe."""


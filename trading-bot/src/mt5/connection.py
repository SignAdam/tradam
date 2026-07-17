"""MetaTrader 5 connection and account safety checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.utils.config import parse_bool
from src.utils.exceptions import MT5ConnectionError, SafetyError

try:  # MetaTrader5 is generally available only where the terminal is installed.
    import MetaTrader5 as mt5
except Exception:  # pragma: no cover - depends on local workstation.
    mt5 = None  # type: ignore[assignment]


@dataclass
class AccountSnapshot:
    login: int | None
    server: str | None
    name: str | None
    currency: str | None
    balance: float | None
    equity: float | None
    trade_mode: Any
    trade_allowed: bool
    is_demo: bool


def mt5_available() -> bool:
    return mt5 is not None


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "_asdict"):
        return dict(value._asdict())
    if isinstance(value, dict):
        return dict(value)
    return {name: getattr(value, name) for name in dir(value) if not name.startswith("_")}


class MT5Connection:
    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.initialized = False

    def initialize(self) -> None:
        if mt5 is None:
            raise MT5ConnectionError(
                "MetaTrader5 package is not available. Install/run this on a machine with MT5."
            )

        kwargs: dict[str, Any] = {}
        path = self.config.get("path")
        if path:
            kwargs["path"] = path
        if self.config.get("login"):
            kwargs["login"] = int(self.config["login"])
        if self.config.get("password"):
            kwargs["password"] = self.config["password"]
        if self.config.get("server"):
            kwargs["server"] = self.config["server"]
        if self.config.get("portable") is not None:
            kwargs["portable"] = bool(self.config.get("portable"))

        if not mt5.initialize(**kwargs):
            code, message = mt5.last_error()
            raise MT5ConnectionError(f"MT5 initialize failed ({code}): {message}")
        self.initialized = True

    def shutdown(self) -> None:
        if mt5 is not None and self.initialized:
            mt5.shutdown()
        self.initialized = False

    def account_snapshot(self) -> AccountSnapshot:
        if mt5 is None or not self.initialized:
            raise MT5ConnectionError("MT5 is not initialized.")
        info = _as_dict(mt5.account_info())
        if not info:
            raise MT5ConnectionError("MT5 account_info() returned no account.")
        trade_mode = info.get("trade_mode")
        demo_constant = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", None)
        is_demo = trade_mode == demo_constant
        if demo_constant is None:
            server_name = str(info.get("server", "")).lower()
            account_name = str(info.get("name", "")).lower()
            is_demo = "demo" in server_name or "demo" in account_name
        return AccountSnapshot(
            login=info.get("login"),
            server=info.get("server"),
            name=info.get("name"),
            currency=info.get("currency"),
            balance=info.get("balance"),
            equity=info.get("equity"),
            trade_mode=trade_mode,
            trade_allowed=bool(info.get("trade_allowed", False)),
            is_demo=bool(is_demo),
        )

    def ensure_account_safety(self, trading_config: dict[str, Any]) -> AccountSnapshot:
        snapshot = self.account_snapshot()
        mode = trading_config.get("mode", "demo_live")
        enable_live = parse_bool(trading_config.get("enable_live_trading"), False)
        require_demo = parse_bool(trading_config.get("require_demo_account"), True)

        if mode != "demo_live":
            raise SafetyError(f"Connected MT5 execution requires demo_live mode, got {mode}.")
        if enable_live:
            raise SafetyError("Real trading is permanently disabled.")
        if not require_demo:
            raise SafetyError("require_demo_account must remain true.")
        if not snapshot.trade_allowed:
            raise SafetyError("Account trading is not allowed by the terminal/account.")
        if not snapshot.is_demo:
            raise SafetyError("demo_live mode requires a MetaTrader 5 demo account.")
        return snapshot

    def terminal_info(self) -> dict[str, Any]:
        if mt5 is None or not self.initialized:
            raise MT5ConnectionError("MT5 is not initialized.")
        return _as_dict(mt5.terminal_info())

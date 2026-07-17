"""Pre-trade validation and order submission for MT5 or paper mode."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.mt5.connection import mt5
from src.utils.config import parse_bool
from src.utils.exceptions import BrokerValidationError, SafetyError


@dataclass
class OrderRequest:
    symbol: str
    direction: str
    lot: float
    entry_price: float
    stop_loss: float
    take_profit: float
    deviation_points: int
    magic: int = 260707
    comment: str = "Tradam demo scalper"


@dataclass
class OrderValidationResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    normalized_lot: float | None = None


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)


class OrderManager:
    def __init__(
        self,
        market_data: Any | None,
        trading_config: dict[str, Any],
        risk_config: dict[str, Any],
        symbols_config: dict[str, Any],
    ) -> None:
        self.market_data = market_data
        self.trading_config = trading_config
        self.risk_config = risk_config
        self.symbols_config = symbols_config.get("symbols", symbols_config)

    def validate_order(
        self,
        request: OrderRequest,
        symbol_info: dict[str, Any] | None = None,
        tick: Any | None = None,
        account_info: dict[str, Any] | None = None,
    ) -> OrderValidationResult:
        reasons: list[str] = []
        direction = request.direction.upper()
        if direction not in {"BUY", "SELL"}:
            reasons.append("Direction must be BUY or SELL")
        if request.stop_loss is None:
            reasons.append("Stop loss is mandatory")
        if request.take_profit is None:
            reasons.append("Take profit is mandatory")
        if request.lot <= 0:
            reasons.append("Lot size must be positive")

        symbol_info = symbol_info or (self.market_data.symbol_info(request.symbol) if self.market_data else {})
        tick = tick or (self.market_data.tick(request.symbol) if self.market_data else None)
        point = float(_get(symbol_info, "point", 0.00001) or 0.00001)
        volume_min = float(_get(symbol_info, "volume_min", 0.01) or 0.01)
        volume_max = float(_get(symbol_info, "volume_max", 100.0) or 100.0)
        volume_step = float(_get(symbol_info, "volume_step", 0.01) or 0.01)
        stop_level = float(_get(symbol_info, "trade_stops_level", 0) or 0)
        trade_mode = _get(symbol_info, "trade_mode", 1)
        spread_points = float(_get(tick, "spread_points", 0.0) or 0.0)
        logical_symbol = self._logical_symbol(request.symbol)
        max_spread = float(self.symbols_config.get(logical_symbol, {}).get("max_spread_points", 999999))

        normalized_lot = normalize_volume(request.lot, volume_min, volume_max, volume_step)
        if normalized_lot < volume_min or normalized_lot > volume_max:
            reasons.append(f"Lot {normalized_lot} outside broker bounds [{volume_min}, {volume_max}]")
        if abs(normalized_lot - request.lot) > max(volume_step / 2, 1e-9):
            reasons.append(f"Lot {request.lot} must align with broker step {volume_step}")
        if parse_bool(self.risk_config.get("execution", {}).get("reject_if_spread_above_symbol_limit"), True):
            if spread_points > max_spread:
                reasons.append(f"Spread too high: {spread_points:.1f} > {max_spread:.1f} points")
        if parse_bool(self.risk_config.get("execution", {}).get("validate_symbol_tradable"), True):
            disabled = getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", -1) if mt5 is not None else -1
            if trade_mode == disabled:
                reasons.append("Symbol trade mode is disabled")

        entry = request.entry_price
        sl_distance = abs(entry - request.stop_loss) / point
        tp_distance = abs(request.take_profit - entry) / point
        if sl_distance < stop_level:
            reasons.append(f"Stop loss too close: {sl_distance:.1f} < {stop_level:.1f} points")
        if tp_distance < stop_level:
            reasons.append(f"Take profit too close: {tp_distance:.1f} < {stop_level:.1f} points")
        if direction == "BUY" and not (request.stop_loss < entry < request.take_profit):
            reasons.append("BUY order requires SL below entry and TP above entry")
        if direction == "SELL" and not (request.take_profit < entry < request.stop_loss):
            reasons.append("SELL order requires TP below entry and SL above entry")

        if account_info and parse_bool(self.risk_config.get("execution", {}).get("require_account_trading_allowed"), True):
            if not bool(_get(account_info, "trade_allowed", True)):
                reasons.append("Account trading is not allowed")
            if parse_bool(self.risk_config.get("execution", {}).get("validate_margin"), True):
                free_margin = _get(account_info, "margin_free", None)
                if free_margin is not None and float(free_margin) <= 0:
                    reasons.append("No free margin available")

        return OrderValidationResult(ok=not reasons, reasons=reasons, normalized_lot=normalized_lot)

    def send_order(self, request: OrderRequest) -> dict[str, Any]:
        mode = self.trading_config.get("mode", "paper")
        validation = self.validate_order(request)
        if not validation.ok:
            raise BrokerValidationError("; ".join(validation.reasons))

        if mode in {"paper", "backtest"}:
            return {
                "mode": mode,
                "status": "simulated",
                "order_id": f"paper-{datetime.utcnow().isoformat()}",
                "request": request.__dict__,
            }

        if mode == "live" and not (
            parse_bool(self.trading_config.get("enable_live_trading"), False)
            and parse_bool(self.trading_config.get("live_trading_confirmation"), False)
        ):
            raise SafetyError("Live order blocked by ENABLE_LIVE_TRADING/LIVE_TRADING_CONFIRMATION.")
        if mt5 is None:
            raise BrokerValidationError("MetaTrader5 package is not available.")
        self._ensure_terminal_allows_autotrading()

        order_type = mt5.ORDER_TYPE_BUY if request.direction.upper() == "BUY" else mt5.ORDER_TYPE_SELL
        payload = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": request.symbol,
            "volume": validation.normalized_lot or request.lot,
            "type": order_type,
            "price": request.entry_price,
            "sl": request.stop_loss,
            "tp": request.take_profit,
            "deviation": request.deviation_points,
            "magic": request.magic,
            "comment": request.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        check = mt5.order_check(payload)
        if check is not None:
            check_data = check._asdict() if hasattr(check, "_asdict") else dict(check)
            ok_retcodes = {
                0,
                getattr(mt5, "TRADE_RETCODE_DONE", None),
                getattr(mt5, "TRADE_RETCODE_PLACED", None),
                getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", None),
            }
            if check_data.get("retcode") not in ok_retcodes:
                raise BrokerValidationError(f"MT5 order_check rejected request: {check_data}")
        result = mt5.order_send(payload)
        if result is None:
            raise BrokerValidationError(f"MT5 order_send returned None: {mt5.last_error()}")
        data = result._asdict() if hasattr(result, "_asdict") else dict(result)
        if data.get("retcode") != getattr(mt5, "TRADE_RETCODE_DONE", data.get("retcode")):
            raise BrokerValidationError(f"Order rejected by MT5: {data}")
        return data

    def _ensure_terminal_allows_autotrading(self) -> None:
        terminal_info = mt5.terminal_info()
        if terminal_info is None:
            raise BrokerValidationError(f"MT5 terminal_info returned None: {mt5.last_error()}")
        terminal = terminal_info._asdict() if hasattr(terminal_info, "_asdict") else dict(terminal_info)
        if terminal.get("trade_allowed") is False:
            raise BrokerValidationError(
                "AutoTrading/Algo Trading is disabled in the MT5 terminal. "
                "Enable the Algo Trading button before running demo_live."
            )

    def _logical_symbol(self, broker_symbol: str) -> str:
        broker_upper = broker_symbol.upper()
        for logical, config in self.symbols_config.items():
            aliases = [logical, *config.get("aliases", [])]
            if any(alias.upper() in broker_upper for alias in aliases):
                return logical
        return broker_symbol


def normalize_volume(volume: float, volume_min: float, volume_max: float, volume_step: float) -> float:
    if volume <= 0:
        return 0.0
    if volume_step <= 0:
        return round(min(max(volume, volume_min), volume_max), 8)
    bounded = min(max(volume, volume_min), volume_max)
    steps = math.floor(((bounded - volume_min) / volume_step) + 1e-12)
    normalized = volume_min + max(steps, 0) * volume_step
    return round(min(max(normalized, volume_min), volume_max), 8)

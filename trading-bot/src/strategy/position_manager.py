"""Persistent scalping position manager for TP1/TP2, break-even, and events."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.strategy.risk_manager import normalize_volume
from src.utils.identity import new_id, utc_now_iso


class PositionState(str, Enum):
    INITIAL_RISK = "INITIAL_RISK"
    RISK_REDUCED = "RISK_REDUCED"
    TP1_SECURED = "TP1_SECURED"
    BREAK_EVEN = "BREAK_EVEN"
    TRAILING = "TRAILING"
    CLOSED = "CLOSED"


@dataclass
class ManagedPosition:
    internal_trade_id: str
    symbol: str
    direction: str
    volume_initial: float
    volume_remaining: float
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    mt5_position_id: str | None = None
    state: PositionState = PositionState.INITIAL_RISK
    tp1_done: bool = False
    tp2_done: bool = False
    break_even_done: bool = False
    sl_modification_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def risk_price(self) -> float:
        return abs(self.entry_price - self.stop_loss)


@dataclass
class PositionManagerConfig:
    tp1_close_percent: float = 0.60
    tp1_r: float = 0.70
    tp2_r: float = 1.35
    risk_reduction_r: float = 0.40
    reduced_risk_r: float = 0.30
    break_even_buffer_points: float = 0.0
    max_sl_modify_attempts: int = 2
    max_duration_seconds: int = 1800


class PositionManager:
    def __init__(
        self,
        broker_api: Any | None,
        event_sink: Any,
        config: PositionManagerConfig | None = None,
    ) -> None:
        self.broker_api = broker_api
        self.event_sink = event_sink
        self.config = config or PositionManagerConfig()

    def evaluate_tick(
        self,
        position: ManagedPosition,
        bid: float,
        ask: float,
        symbol_info: dict[str, Any],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        current_price = bid if position.direction.upper() == "BUY" else ask
        current_r = self.current_r(position, current_price)
        spread = ask - bid

        if position.state == PositionState.INITIAL_RISK and current_r >= self.config.risk_reduction_r:
            reduced_sl = self.reduced_risk_stop(position)
            if self._sl_improves(position, reduced_sl):
                events.append(self.modify_stop_loss(position, reduced_sl, bid, ask, "RISK_REDUCED"))

        if not position.tp1_done and self._target_reached(position.direction, current_price, position.tp1):
            volume = self.tp1_volume(position, symbol_info)
            events.append(self.partial_close(position, volume, bid, ask, "TP1_REACHED"))
            if events[-1].get("confirmed"):
                position.tp1_done = True
                position.state = PositionState.TP1_SECURED
                be_price = self.break_even_price(position, symbol_info, spread)
                events.append(self.modify_stop_loss(position, be_price, bid, ask, "BREAK_EVEN_REQUESTED"))

        if not position.tp2_done and self._target_reached(position.direction, current_price, position.tp2):
            events.append(self.partial_close(position, position.volume_remaining, bid, ask, "TP2_REACHED"))
            if events[-1].get("confirmed"):
                position.tp2_done = True
                position.state = PositionState.CLOSED

        return events

    def current_r(self, position: ManagedPosition, current_price: float) -> float:
        if position.risk_price <= 0:
            return 0.0
        if position.direction.upper() == "BUY":
            return (current_price - position.entry_price) / position.risk_price
        return (position.entry_price - current_price) / position.risk_price

    def reduced_risk_stop(self, position: ManagedPosition) -> float:
        remaining_risk = position.risk_price * self.config.reduced_risk_r
        if position.direction.upper() == "BUY":
            return position.entry_price - remaining_risk
        return position.entry_price + remaining_risk

    def break_even_price(self, position: ManagedPosition, symbol_info: dict[str, Any], spread: float) -> float:
        point = float(symbol_info.get("point") or symbol_info.get("trade_tick_size") or 0.01)
        buffer = point * self.config.break_even_buffer_points
        if position.direction.upper() == "BUY":
            return position.entry_price + spread + buffer
        return position.entry_price - spread - buffer

    def tp1_volume(self, position: ManagedPosition, symbol_info: dict[str, Any]) -> float:
        volume_min = float(symbol_info.get("volume_min") or 0.01)
        volume_step = float(symbol_info.get("volume_step") or 0.01)
        raw = position.volume_initial * self.config.tp1_close_percent
        close_volume = normalize_volume(raw, volume_min, position.volume_remaining, volume_step)
        remaining = round(position.volume_remaining - close_volume, 8)
        if 0 < remaining < volume_min:
            close_volume = position.volume_remaining
        return close_volume

    def partial_close(
        self,
        position: ManagedPosition,
        volume: float,
        bid: float,
        ask: float,
        reason: str,
    ) -> dict[str, Any]:
        event = self._event(position, reason, bid, ask, volume=volume)
        if volume <= 0:
            event.update({"confirmed": False, "error_message": "Invalid partial close volume"})
            self._emit(event)
            return event
        result = self._send_partial_close(position, volume, bid, ask)
        event["mt5_retcode"] = result.get("retcode")
        event["error_message"] = result.get("comment")
        event["confirmed"] = bool(result.get("ok"))
        if result.get("ok"):
            position.volume_remaining = max(round(position.volume_remaining - volume, 8), 0.0)
            confirm = self._event(position, "PARTIAL_CLOSE_CONFIRMED", bid, ask, volume=volume)
            confirm["mt5_retcode"] = result.get("retcode")
            confirm["confirmed"] = True
            self._emit(event)
            self._emit(confirm)
            return confirm
        self._emit(event)
        return event

    def modify_stop_loss(
        self,
        position: ManagedPosition,
        new_stop_loss: float,
        bid: float,
        ask: float,
        event_type: str,
    ) -> dict[str, Any]:
        old_sl = position.stop_loss
        if not self._sl_improves(position, new_stop_loss):
            event = self._event(position, event_type, bid, ask, old_stop_loss=old_sl, new_stop_loss=old_sl)
            event.update({"confirmed": False, "error_message": "Refusing to move SL away from protection"})
            self._emit(event)
            return event
        attempts = 0
        last_result: dict[str, Any] = {}
        while attempts < self.config.max_sl_modify_attempts:
            attempts += 1
            last_result = self._send_sl_modify(position, new_stop_loss)
            if last_result.get("ok"):
                position.stop_loss = new_stop_loss
                position.sl_modification_count += 1
                if event_type.startswith("BREAK_EVEN"):
                    position.break_even_done = True
                    position.state = PositionState.BREAK_EVEN
                else:
                    position.state = PositionState.RISK_REDUCED
                event = self._event(
                    position,
                    event_type.replace("REQUESTED", "CONFIRMED"),
                    bid,
                    ask,
                    old_stop_loss=old_sl,
                    new_stop_loss=new_stop_loss,
                    attempts=attempts,
                )
                event.update({"mt5_retcode": last_result.get("retcode"), "confirmed": True})
                self._emit(event)
                return event
        event = self._event(
            position,
            event_type.replace("REQUESTED", "REJECTED"),
            bid,
            ask,
            old_stop_loss=old_sl,
            new_stop_loss=new_stop_loss,
            attempts=attempts,
        )
        event.update({"mt5_retcode": last_result.get("retcode"), "error_message": last_result.get("comment"), "confirmed": False})
        self._emit(event)
        return event

    def _send_partial_close(self, position: ManagedPosition, volume: float, bid: float, ask: float) -> dict[str, Any]:
        if self.broker_api is None:
            return {"ok": True, "retcode": 0, "comment": "paper-confirmed"}
        order_type = getattr(self.broker_api, "ORDER_TYPE_SELL", 1) if position.direction.upper() == "BUY" else getattr(self.broker_api, "ORDER_TYPE_BUY", 0)
        price = bid if position.direction.upper() == "BUY" else ask
        payload = {
            "action": getattr(self.broker_api, "TRADE_ACTION_DEAL", 1),
            "symbol": position.symbol,
            "volume": volume,
            "type": order_type,
            "position": int(position.mt5_position_id) if position.mt5_position_id else 0,
            "price": price,
            "comment": "Tradam partial close",
        }
        result = self.broker_api.order_send(payload)
        data = result._asdict() if hasattr(result, "_asdict") else dict(result or {})
        ok = data.get("retcode") == getattr(self.broker_api, "TRADE_RETCODE_DONE", data.get("retcode"))
        return {**data, "ok": ok}

    def _send_sl_modify(self, position: ManagedPosition, new_stop_loss: float) -> dict[str, Any]:
        if self.broker_api is None:
            return {"ok": True, "retcode": 0, "comment": "paper-confirmed"}
        payload = {
            "action": getattr(self.broker_api, "TRADE_ACTION_SLTP", 6),
            "symbol": position.symbol,
            "position": int(position.mt5_position_id) if position.mt5_position_id else 0,
            "sl": new_stop_loss,
            "tp": position.tp2,
            "comment": "Tradam SL protect",
        }
        result = self.broker_api.order_send(payload)
        data = result._asdict() if hasattr(result, "_asdict") else dict(result or {})
        ok = data.get("retcode") == getattr(self.broker_api, "TRADE_RETCODE_DONE", data.get("retcode"))
        return {**data, "ok": ok}

    def _event(self, position: ManagedPosition, event_type: str, bid: float, ask: float, **extra: Any) -> dict[str, Any]:
        current_price = bid if position.direction.upper() == "BUY" else ask
        return {
            "event_id": new_id("evt"),
            "internal_trade_id": position.internal_trade_id,
            "mt5_position_id": position.mt5_position_id,
            "event_type": event_type,
            "timestamp_utc": utc_now_iso(),
            "bid": bid,
            "ask": ask,
            "spread": ask - bid,
            "current_r": self.current_r(position, current_price),
            "volume": extra.get("volume"),
            "old_stop_loss": extra.get("old_stop_loss"),
            "new_stop_loss": extra.get("new_stop_loss"),
            "attempts": extra.get("attempts", 0),
        }

    def _emit(self, event: dict[str, Any]) -> None:
        if hasattr(self.event_sink, "log_position_event"):
            self.event_sink.log_position_event(event)
        elif callable(self.event_sink):
            self.event_sink(event)

    @staticmethod
    def _target_reached(direction: str, price: float, target: float) -> bool:
        if direction.upper() == "BUY":
            return price >= target
        return price <= target

    @staticmethod
    def _sl_improves(position: ManagedPosition, new_stop_loss: float) -> bool:
        if position.direction.upper() == "BUY":
            return new_stop_loss >= position.stop_loss
        return new_stop_loss <= position.stop_loss


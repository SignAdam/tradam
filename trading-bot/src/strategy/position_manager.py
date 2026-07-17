"""Persistent MT5 demo position manager for fast scalping exits."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from src.strategy.risk_manager import normalize_volume
from src.utils.exceptions import SafetyError
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
    mt5_position_id: str
    run_id: str | None = None
    session_id: str | None = None
    logical_symbol: str | None = None
    opened_at_utc: str = field(default_factory=utc_now_iso)
    initial_stop_loss: float | None = None
    state: PositionState = PositionState.INITIAL_RISK
    tp1_done: bool = False
    tp2_done: bool = False
    break_even_done: bool = False
    sl_modification_count: int = 0
    max_favorable_r: float = 0.0
    max_adverse_r: float = 0.0
    max_favorable_price: float | None = None
    max_adverse_price: float | None = None
    confirmation_counters: dict[str, int] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.initial_stop_loss is None:
            self.initial_stop_loss = self.stop_loss
        if not self.mt5_position_id:
            raise SafetyError("A precise MT5 position ticket is mandatory")
        if isinstance(self.state, str):
            self.state = PositionState(self.state)

    @property
    def risk_price(self) -> float:
        return abs(self.entry_price - float(self.initial_stop_loss or self.stop_loss))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ManagedPosition":
        payload = dict(data)
        payload["state"] = PositionState(payload.get("state", PositionState.INITIAL_RISK))
        return cls(**payload)


@dataclass
class PositionManagerConfig:
    tp1_close_percent: float = 0.65
    risk_reduction_r: float = 0.30
    reduced_risk_r: float = 0.25
    break_even_buffer_points: float = 2.0
    max_sl_modify_attempts: int = 2
    max_duration_seconds: int = 1200
    no_progress_timeout_seconds: int = 420
    no_progress_min_r: float = 0.20
    action_confirmation_ticks: int = 2
    trailing_enabled: bool = True
    trailing_atr_multiplier: float = 0.60


class PositionManager:
    def __init__(
        self,
        broker_api: Any,
        event_sink: Any,
        config: PositionManagerConfig | None = None,
        database: Any | None = None,
    ) -> None:
        if broker_api is None:
            raise SafetyError("PositionManager requires a real MT5 demo broker API")
        self.broker_api = broker_api
        self.event_sink = event_sink
        self.config = config or PositionManagerConfig()
        self.database = database

    def evaluate_tick(
        self,
        position: ManagedPosition,
        bid: float,
        ask: float,
        symbol_info: dict[str, Any],
        now: datetime | None = None,
        market_context: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if position.state == PositionState.CLOSED:
            return []
        market_context = market_context or {}
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        events: list[dict[str, Any]] = []
        current_price = bid if position.direction.upper() == "BUY" else ask
        current_r = self.current_r(position, current_price)
        self._update_excursions(position, current_price, current_r)

        if position.state == PositionState.INITIAL_RISK and current_r >= self.config.risk_reduction_r:
            if self._confirmed(position, "risk_reduction", True):
                reduced_sl = self.reduced_risk_stop(position)
                if self._sl_improves(position, reduced_sl):
                    events.append(self.modify_stop_loss(position, reduced_sl, bid, ask, "RISK_REDUCED", symbol_info))
        else:
            self._confirmed(position, "risk_reduction", False)

        tp1_reached = self._target_reached(position.direction, current_price, position.tp1)
        if not position.tp1_done and self._confirmed(position, "tp1", tp1_reached):
            self._emit(self._event(position, "TP1_REACHED", bid, ask))
            volume = self.tp1_volume(position, symbol_info)
            close_event = self.partial_close(position, volume, bid, ask, "TP1")
            events.append(close_event)
            if close_event.get("confirmed"):
                position.tp1_done = True
                position.state = PositionState.TP1_SECURED
                self._persist(position)
                be_price = self.break_even_price(position, symbol_info, ask - bid)
                events.append(
                    self.modify_stop_loss(
                        position, be_price, bid, ask, "BREAK_EVEN_REQUESTED", symbol_info
                    )
                )

        tp2_reached = self._target_reached(position.direction, current_price, position.tp2)
        if not position.tp2_done and self._confirmed(position, "tp2", tp2_reached):
            self._emit(self._event(position, "TP2_REACHED", bid, ask))
            close_event = self.partial_close(position, position.volume_remaining, bid, ask, "TP2")
            events.append(close_event)
            if close_event.get("confirmed"):
                position.tp2_done = True
                position.state = PositionState.CLOSED
                self._emit(self._event(position, "POSITION_CLOSED", bid, ask, volume=position.volume_remaining))
                self._persist(position, closed=True)
                return events

        if position.tp1_done and position.volume_remaining > 0 and self.config.trailing_enabled:
            trailing = self._trailing_candidate(position, current_price, symbol_info, market_context)
            if trailing is not None and self._sl_improves(position, trailing):
                events.append(self.modify_stop_loss(position, trailing, bid, ask, "TRAILING_UPDATED", symbol_info))

        exit_reason = self._time_or_momentum_exit(position, moment, current_r, market_context)
        if exit_reason and self._confirmed(position, exit_reason, True):
            event_type = "TIME_EXIT" if exit_reason in {"TIME_EXIT", "NO_PROGRESS_TIME_EXIT"} else "INVERSE_SIGNAL_EXIT"
            self._emit(self._event(position, event_type, bid, ask, error_message=exit_reason))
            close_event = self.partial_close(position, position.volume_remaining, bid, ask, exit_reason)
            events.append(close_event)
            if close_event.get("confirmed"):
                position.state = PositionState.CLOSED
                self._emit(self._event(position, "POSITION_CLOSED", bid, ask, error_message=exit_reason))
                self._persist(position, closed=True)
                return events
        elif exit_reason is None:
            for key in ("TIME_EXIT", "NO_PROGRESS_TIME_EXIT", "MOMENTUM_EXIT", "SPREAD_EXIT", "NEWS_EXIT"):
                self._confirmed(position, key, False)

        self._persist(position)
        return events

    def resume_from_broker(self) -> list[ManagedPosition]:
        if self.database is None:
            return []
        persisted = {
            str(item.get("mt5_position_id")): ManagedPosition.from_dict(item)
            for item in self.database.load_open_managed_positions()
        }
        broker_positions = list(self.broker_api.positions_get() or [])
        active_ids = {
            str(self._as_dict(item).get("ticket") or self._as_dict(item).get("identifier"))
            for item in broker_positions
        }
        resumed: list[ManagedPosition] = []
        for position_id, position in persisted.items():
            if position_id in active_ids:
                resumed.append(position)
            else:
                position.state = PositionState.CLOSED
                self._persist(position, closed=True)
        return resumed

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
        point = float(symbol_info.get("point") or symbol_info.get("trade_tick_size") or 0.0)
        if point <= 0:
            raise ValueError("Broker point/tick size is required for break-even")
        buffer = point * self.config.break_even_buffer_points
        commission_price = float(position.metadata.get("commission_cost_price") or 0.0)
        slippage_price = float(position.metadata.get("average_slippage_price") or 0.0)
        costs = spread + commission_price + slippage_price + buffer
        return position.entry_price + costs if position.direction.upper() == "BUY" else position.entry_price - costs

    def tp1_volume(self, position: ManagedPosition, symbol_info: dict[str, Any]) -> float:
        volume_min = float(symbol_info.get("volume_min") or 0.0)
        volume_max = float(symbol_info.get("volume_max") or position.volume_remaining)
        volume_step = float(symbol_info.get("volume_step") or 0.0)
        if volume_min <= 0 or volume_step <= 0:
            raise ValueError("Broker volume_min and volume_step are required")
        raw = position.volume_initial * self.config.tp1_close_percent
        close_volume = normalize_volume(raw, volume_min, min(volume_max, position.volume_remaining), volume_step)
        remaining = round(position.volume_remaining - close_volume, 8)
        if 0 < remaining < volume_min:
            candidate = normalize_volume(position.volume_remaining - volume_min, volume_min, position.volume_remaining, volume_step)
            close_volume = candidate if candidate > 0 else position.volume_remaining
        return min(close_volume, position.volume_remaining)

    def partial_close(
        self,
        position: ManagedPosition,
        volume: float,
        bid: float,
        ask: float,
        reason: str,
    ) -> dict[str, Any]:
        requested_type = "PARTIAL_CLOSE_REQUESTED" if volume < position.volume_remaining else f"{reason}_CLOSE_REQUESTED"
        requested = self._event(position, requested_type, bid, ask, volume=volume)
        self._emit(requested)
        if volume <= 0 or volume > position.volume_remaining + 1e-9:
            rejected = self._event(
                position,
                "PARTIAL_CLOSE_REJECTED",
                bid,
                ask,
                volume=volume,
                error_message="Invalid close volume",
            )
            rejected["confirmed"] = False
            self._emit(rejected)
            return rejected
        result = self._send_close(position, volume, bid, ask)
        confirmed = self._deal_confirmed(result, position)
        event_type = "PARTIAL_CLOSE_CONFIRMED" if confirmed else "PARTIAL_CLOSE_REJECTED"
        event = self._event(
            position,
            event_type,
            bid,
            ask,
            volume=volume,
            mt5_retcode=result.get("retcode"),
            error_message=result.get("comment"),
            payload={"reason": reason, "deal": result.get("deal"), "order": result.get("order")},
        )
        event["confirmed"] = confirmed
        if confirmed:
            position.volume_remaining = max(round(position.volume_remaining - volume, 8), 0.0)
            if reason == "TP1":
                event["tp1_actual_price"] = result.get("price")
            if reason == "TP2":
                event["tp2_actual_price"] = result.get("price")
        self._emit(event)
        self._persist(position)
        return event

    def modify_stop_loss(
        self,
        position: ManagedPosition,
        new_stop_loss: float,
        bid: float,
        ask: float,
        event_type: str,
        symbol_info: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        old_sl = position.stop_loss
        if not self._sl_improves(position, new_stop_loss):
            event = self._event(
                position, event_type, bid, ask, old_stop_loss=old_sl, new_stop_loss=old_sl,
                error_message="Refusing to move SL away from protection"
            )
            event["confirmed"] = False
            self._emit(event)
            return event
        attempts = 0
        candidate = new_stop_loss
        last_result: dict[str, Any] = {}
        while attempts < self.config.max_sl_modify_attempts:
            attempts += 1
            last_result = self._send_sl_modify(position, candidate)
            if self._retcode_done(last_result):
                position.stop_loss = candidate
                position.sl_modification_count += 1
                if event_type.startswith("BREAK_EVEN"):
                    position.break_even_done = True
                    position.state = PositionState.BREAK_EVEN
                elif event_type == "TRAILING_UPDATED":
                    position.state = PositionState.TRAILING
                else:
                    position.state = PositionState.RISK_REDUCED
                confirmed_type = event_type.replace("REQUESTED", "CONFIRMED")
                event = self._event(
                    position, confirmed_type, bid, ask, old_stop_loss=old_sl,
                    new_stop_loss=candidate, attempts=attempts,
                    mt5_retcode=last_result.get("retcode"), payload={"result": last_result}
                )
                event["confirmed"] = True
                self._emit(event)
                self._persist(position)
                return event
            candidate = self._recalculate_stop_for_broker(
                position, candidate, bid, ask, symbol_info or {}
            )
            if candidate is None:
                break
        rejected_type = event_type.replace("REQUESTED", "REJECTED")
        event = self._event(
            position, rejected_type, bid, ask, old_stop_loss=old_sl,
            new_stop_loss=new_stop_loss, attempts=attempts,
            mt5_retcode=last_result.get("retcode"),
            error_message=last_result.get("comment") or "SL protection could not be applied",
            payload={"result": last_result},
        )
        event["confirmed"] = False
        self._emit(event)
        return event

    def _send_close(self, position: ManagedPosition, volume: float, bid: float, ask: float) -> dict[str, Any]:
        if not position.mt5_position_id:
            return {"retcode": -1, "comment": "Missing MT5 position ticket"}
        order_type = (
            getattr(self.broker_api, "ORDER_TYPE_SELL", 1)
            if position.direction.upper() == "BUY"
            else getattr(self.broker_api, "ORDER_TYPE_BUY", 0)
        )
        payload = {
            "action": getattr(self.broker_api, "TRADE_ACTION_DEAL", 1),
            "symbol": position.symbol,
            "volume": volume,
            "type": order_type,
            "position": int(position.mt5_position_id),
            "price": bid if position.direction.upper() == "BUY" else ask,
            "deviation": int(position.metadata.get("deviation_points", 30)),
            "magic": int(position.metadata.get("magic", 260707)),
            "comment": "Tradam demo partial close",
        }
        check = self.broker_api.order_check(payload)
        if check is None:
            return {"retcode": -1, "comment": "order_check returned None"}
        check_data = self._as_dict(check)
        if not self._retcode_check_ok(check_data):
            return {**check_data, "comment": f"order_check rejected close: {check_data.get('comment', '')}"}
        return self._as_dict(self.broker_api.order_send(payload))

    def _send_sl_modify(self, position: ManagedPosition, new_stop_loss: float) -> dict[str, Any]:
        payload = {
            "action": getattr(self.broker_api, "TRADE_ACTION_SLTP", 6),
            "symbol": position.symbol,
            "position": int(position.mt5_position_id),
            "sl": new_stop_loss,
            "tp": position.tp2,
            "magic": int(position.metadata.get("magic", 260707)),
            "comment": "Tradam demo SL protect",
        }
        check = self.broker_api.order_check(payload)
        if check is None:
            return {"retcode": -1, "comment": "order_check returned None"}
        check_data = self._as_dict(check)
        if not self._retcode_check_ok(check_data):
            return check_data
        return self._as_dict(self.broker_api.order_send(payload))

    def _deal_confirmed(self, result: dict[str, Any], position: ManagedPosition) -> bool:
        if not self._retcode_done(result) or not result.get("deal"):
            return False
        getter = getattr(self.broker_api, "history_deals_get", None)
        if getter is None:
            return True
        try:
            deals = getter(ticket=int(result["deal"]))
        except TypeError:
            try:
                deals = getter(position=int(position.mt5_position_id))
            except TypeError:
                return True
        return any(str(self._as_dict(item).get("ticket")) == str(result["deal"]) for item in list(deals or []))

    def _retcode_done(self, result: dict[str, Any]) -> bool:
        return result.get("retcode") in {
            getattr(self.broker_api, "TRADE_RETCODE_DONE", 10009),
            getattr(self.broker_api, "TRADE_RETCODE_DONE_PARTIAL", 10010),
        }

    def _retcode_check_ok(self, result: dict[str, Any]) -> bool:
        return result.get("retcode") in {
            0,
            getattr(self.broker_api, "TRADE_RETCODE_DONE", 10009),
            getattr(self.broker_api, "TRADE_RETCODE_PLACED", 10008),
            getattr(self.broker_api, "TRADE_RETCODE_DONE_PARTIAL", 10010),
        }

    def _recalculate_stop_for_broker(
        self,
        position: ManagedPosition,
        requested: float,
        bid: float,
        ask: float,
        symbol_info: dict[str, Any],
    ) -> float | None:
        point = float(symbol_info.get("point") or symbol_info.get("trade_tick_size") or 0.0)
        distance = float(symbol_info.get("trade_stops_level") or 0.0) * point
        if point <= 0:
            return None
        if position.direction.upper() == "BUY":
            candidate = min(requested, bid - distance - point)
        else:
            candidate = max(requested, ask + distance + point)
        return candidate if self._sl_improves(position, candidate) else None

    def _trailing_candidate(
        self,
        position: ManagedPosition,
        current_price: float,
        symbol_info: dict[str, Any],
        context: dict[str, Any],
    ) -> float | None:
        atr = float(context.get("atr") or 0.0)
        ema9 = context.get("ema9")
        swing = context.get("recent_swing")
        candidates = [float(value) for value in (ema9, swing) if value is not None]
        if atr > 0:
            candidates.append(
                current_price - atr * self.config.trailing_atr_multiplier
                if position.direction.upper() == "BUY"
                else current_price + atr * self.config.trailing_atr_multiplier
            )
        if not candidates:
            return None
        point = float(symbol_info.get("point") or 0.0)
        if position.direction.upper() == "BUY":
            valid = [value for value in candidates if value < current_price - point]
            return max(valid) if valid else None
        valid = [value for value in candidates if value > current_price + point]
        return min(valid) if valid else None

    def _time_or_momentum_exit(
        self,
        position: ManagedPosition,
        now: datetime,
        current_r: float,
        context: dict[str, Any],
    ) -> str | None:
        opened = datetime.fromisoformat(position.opened_at_utc.replace("Z", "+00:00"))
        age = max((now - opened.astimezone(timezone.utc)).total_seconds(), 0.0)
        if age >= self.config.max_duration_seconds:
            return "TIME_EXIT"
        if age >= self.config.no_progress_timeout_seconds and position.max_favorable_r < self.config.no_progress_min_r:
            return "NO_PROGRESS_TIME_EXIT"
        if context.get("news_blocking"):
            return "NEWS_EXIT"
        if context.get("spread_abnormal"):
            return "SPREAD_EXIT"
        inverse_votes = sum(
            bool(context.get(key))
            for key in ("ema_cross_inverse", "structure_break_inverse", "rsi_inverse", "macd_inverse", "vwap_loss")
        )
        if inverse_votes >= int(context.get("minimum_inverse_votes", 2)):
            return "MOMENTUM_EXIT"
        return None

    def _update_excursions(self, position: ManagedPosition, price: float, current_r: float) -> None:
        position.max_favorable_r = max(position.max_favorable_r, current_r)
        position.max_adverse_r = min(position.max_adverse_r, current_r)
        if position.max_favorable_price is None or (
            position.direction.upper() == "BUY" and price > position.max_favorable_price
        ) or (position.direction.upper() == "SELL" and price < position.max_favorable_price):
            position.max_favorable_price = price
        if position.max_adverse_price is None or (
            position.direction.upper() == "BUY" and price < position.max_adverse_price
        ) or (position.direction.upper() == "SELL" and price > position.max_adverse_price):
            position.max_adverse_price = price

    def _confirmed(self, position: ManagedPosition, key: str, condition: bool) -> bool:
        if not condition:
            position.confirmation_counters[key] = 0
            return False
        position.confirmation_counters[key] = position.confirmation_counters.get(key, 0) + 1
        return position.confirmation_counters[key] >= max(self.config.action_confirmation_ticks, 1)

    def _event(self, position: ManagedPosition, event_type: str, bid: float, ask: float, **extra: Any) -> dict[str, Any]:
        current_price = bid if position.direction.upper() == "BUY" else ask
        profit = self.broker_api.order_calc_profit(
            getattr(self.broker_api, "ORDER_TYPE_BUY", 0) if position.direction.upper() == "BUY" else getattr(self.broker_api, "ORDER_TYPE_SELL", 1),
            position.symbol,
            position.volume_remaining,
            position.entry_price,
            current_price,
        )
        return {
            "event_id": new_id("evt"),
            "run_id": position.run_id,
            "session_id": position.session_id,
            "internal_trade_id": position.internal_trade_id,
            "mt5_position_id": position.mt5_position_id,
            "event_type": event_type,
            "timestamp_utc": utc_now_iso(),
            "bid": bid,
            "ask": ask,
            "spread": ask - bid,
            "unrealized_profit": float(profit) if profit is not None else None,
            "current_r": self.current_r(position, current_price),
            "volume": extra.get("volume"),
            "old_stop_loss": extra.get("old_stop_loss"),
            "new_stop_loss": extra.get("new_stop_loss"),
            "attempts": extra.get("attempts", 0),
            "mt5_retcode": extra.get("mt5_retcode"),
            "error_message": extra.get("error_message"),
            "payload": extra.get("payload", {}),
        }

    def _emit(self, event: dict[str, Any]) -> None:
        if hasattr(self.event_sink, "log_position_event"):
            self.event_sink.log_position_event(event)
        elif callable(self.event_sink):
            self.event_sink(event)

    def _persist(self, position: ManagedPosition, closed: bool = False) -> None:
        if self.database is None:
            return
        self.database.upsert_managed_position(position.to_dict(), closed=closed)
        self.database.update_trade_fields(
            position.internal_trade_id,
            {
                "remaining_volume": position.volume_remaining,
                "final_stop_loss": position.stop_loss,
                "sl_modification_count": position.sl_modification_count,
                "break_even_applied": int(position.break_even_done),
                "break_even_price": position.stop_loss if position.break_even_done else None,
                "management_state": position.state.value,
                "max_favorable_price": position.max_favorable_price,
                "max_adverse_price": position.max_adverse_price,
                "mfe_r": position.max_favorable_r,
                "mae_r": position.max_adverse_r,
                "status": "CLOSED" if closed else ("PARTIALLY_CLOSED" if position.tp1_done else "OPEN"),
            },
        )

    @staticmethod
    def _target_reached(direction: str, price: float, target: float) -> bool:
        return price >= target if direction.upper() == "BUY" else price <= target

    @staticmethod
    def _sl_improves(position: ManagedPosition, new_stop_loss: float) -> bool:
        return new_stop_loss >= position.stop_loss if position.direction.upper() == "BUY" else new_stop_loss <= position.stop_loss

    @staticmethod
    def _as_dict(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if hasattr(value, "_asdict"):
            return dict(value._asdict())
        if isinstance(value, dict):
            return dict(value)
        return {name: getattr(value, name) for name in dir(value) if not name.startswith("_")}

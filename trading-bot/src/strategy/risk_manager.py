"""Risk management and conservative position sizing."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SymbolTradingSpec:
    tick_value: float = 1.0
    tick_size: float = 0.01
    volume_min: float = 0.01
    volume_max: float = 1.0
    volume_step: float = 0.01
    point: float = 0.01
    stop_level_points: float = 0.0

    @classmethod
    def from_symbol_info(cls, info: dict[str, Any], fallback: dict[str, Any] | None = None) -> "SymbolTradingSpec":
        fallback = fallback or {}
        return cls(
            tick_value=float(info.get("trade_tick_value") or fallback.get("fallback_tick_value", 1.0)),
            tick_size=float(info.get("trade_tick_size") or fallback.get("fallback_tick_size", 0.01)),
            volume_min=float(info.get("volume_min") or fallback.get("fallback_volume_min", 0.01)),
            volume_max=float(info.get("volume_max") or fallback.get("fallback_volume_max", 1.0)),
            volume_step=float(info.get("volume_step") or fallback.get("fallback_volume_step", 0.01)),
            point=float(info.get("point") or fallback.get("fallback_tick_size", 0.01)),
            stop_level_points=float(info.get("trade_stops_level") or 0.0),
        )


@dataclass
class RiskState:
    equity: float
    balance: float | None = None
    session_pnl: float = 0.0
    daily_pnl: float = 0.0
    trades_this_session: int = 0
    consecutive_losses: int = 0
    current_drawdown_percent: float = 0.0
    open_positions_same_symbol_direction: int = 0
    last_loss_lot: float | None = None


@dataclass
class RiskCheck:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    risk_amount: float = 0.0
    risk_percent: float = 0.0
    reward_amount: float = 0.0
    risk_reward: float = 0.0


class RiskManager:
    def __init__(self, risk_config: dict[str, Any]) -> None:
        self.config = risk_config
        self.risk = risk_config.get("risk", risk_config)
        self.position_sizing = risk_config.get("position_sizing", {})

    def calculate_position_size(
        self,
        equity: float,
        entry_price: float,
        stop_loss: float,
        symbol_spec: SymbolTradingSpec,
        risk_percent: float | None = None,
    ) -> float:
        configured_risk = float(
            risk_percent
            if risk_percent is not None
            else self.risk.get("risk_per_trade_percent", 0.5)
        )
        risk_amount = equity * configured_risk / 100
        stop_distance = abs(entry_price - stop_loss)
        if stop_distance <= 0:
            return 0.0
        risk_per_lot = (stop_distance / symbol_spec.tick_size) * symbol_spec.tick_value
        if risk_per_lot <= 0:
            return 0.0
        raw_lot = risk_amount / risk_per_lot
        return normalize_volume(
            raw_lot,
            symbol_spec.volume_min,
            symbol_spec.volume_max,
            symbol_spec.volume_step,
            rounding=self.position_sizing.get("volume_rounding", "floor"),
        )

    def validate_trade(
        self,
        state: RiskState,
        direction: str,
        lot: float,
        entry_price: float,
        stop_loss: float | None,
        take_profit: float | None,
        symbol_spec: SymbolTradingSpec,
    ) -> RiskCheck:
        reasons: list[str] = []
        if self.risk.get("allow_martingale", False):
            reasons.append("Config error: martingale must remain disabled")
        if self.risk.get("allow_grid", False):
            reasons.append("Config error: aggressive grid trading must remain disabled")
        if self.risk.get("allow_loss_recovery", False):
            reasons.append("Config error: automatic loss recovery must remain disabled")
        if self.risk.get("require_stop_loss", True) and stop_loss is None:
            reasons.append("Stop loss is mandatory")
        if self.risk.get("require_take_profit", True) and take_profit is None:
            reasons.append("Take profit is mandatory")
        if stop_loss is None or take_profit is None:
            return RiskCheck(False, reasons)

        risk_per_lot = (abs(entry_price - stop_loss) / symbol_spec.tick_size) * symbol_spec.tick_value
        reward_per_lot = (abs(take_profit - entry_price) / symbol_spec.tick_size) * symbol_spec.tick_value
        risk_amount = risk_per_lot * lot
        reward_amount = reward_per_lot * lot
        risk_percent = (risk_amount / state.equity) * 100 if state.equity > 0 else 999.0
        risk_reward = reward_amount / risk_amount if risk_amount > 0 else 0.0

        if direction.upper() == "BUY" and not (stop_loss < entry_price < take_profit):
            reasons.append("BUY risk geometry invalid")
        if direction.upper() == "SELL" and not (take_profit < entry_price < stop_loss):
            reasons.append("SELL risk geometry invalid")
        if risk_percent > float(self.risk.get("max_risk_per_trade_percent", 0.5)):
            reasons.append(
                f"Risk per trade too high: {risk_percent:.3f}% > {self.risk.get('max_risk_per_trade_percent')}%"
            )
        if risk_reward < float(self.risk.get("min_risk_reward", 1.4)):
            reasons.append(f"Risk/reward too low: {risk_reward:.2f}")
        if state.trades_this_session >= int(self.risk.get("max_trades_per_session", 6)):
            reasons.append("Maximum trades per session reached")
        if state.consecutive_losses >= int(self.risk.get("max_consecutive_losses", 3)):
            reasons.append("Maximum consecutive losses reached")
        if state.current_drawdown_percent >= float(self.risk.get("max_drawdown_percent", 6.0)):
            reasons.append("Maximum drawdown reached")
        if abs(min(state.session_pnl, 0.0)) >= state.equity * float(self.risk.get("max_session_loss_percent", 1.5)) / 100:
            reasons.append("Maximum session loss reached")
        if abs(min(state.daily_pnl, 0.0)) >= state.equity * float(self.risk.get("max_daily_loss_percent", 3.0)) / 100:
            reasons.append("Maximum daily loss reached")
        if (
            not self.risk.get("increase_lot_after_loss", False)
            and state.last_loss_lot is not None
            and lot > state.last_loss_lot
        ):
            reasons.append("Lot increase after a loss is disabled")
        if state.open_positions_same_symbol_direction > 0:
            reasons.append("Existing same-direction position already open")

        return RiskCheck(
            ok=not reasons,
            reasons=reasons,
            risk_amount=risk_amount,
            risk_percent=risk_percent,
            reward_amount=reward_amount,
            risk_reward=risk_reward,
        )


def normalize_volume(
    volume: float,
    volume_min: float,
    volume_max: float,
    volume_step: float,
    rounding: str = "floor",
) -> float:
    if volume <= 0:
        return 0.0
    if volume_step <= 0:
        return round(min(max(volume, volume_min), volume_max), 8)
    bounded = min(max(volume, volume_min), volume_max)
    steps_float = (bounded - volume_min) / volume_step
    if rounding == "nearest":
        steps = round(steps_float)
    else:
        steps = math.floor(steps_float + 1e-12)
    normalized = volume_min + steps * volume_step
    return round(min(max(normalized, volume_min), volume_max), 8)


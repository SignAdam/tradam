"""Risk management and conservative position sizing."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SymbolTradingSpec:
    tick_value: float = 0.0
    tick_size: float = 0.0
    volume_min: float = 0.0
    volume_max: float = 0.0
    volume_step: float = 0.0
    point: float = 0.0
    stop_level_points: float = 0.0
    broker_validated: bool = False

    @classmethod
    def from_symbol_info(
        cls,
        info: dict[str, Any],
        fallback: dict[str, Any] | None = None,
        strict: bool = False,
    ) -> "SymbolTradingSpec":
        fallback = fallback or {}
        spec = cls(
            tick_value=float(info.get("trade_tick_value") or (0.0 if strict else fallback.get("backtest_tick_value", 0.0))),
            tick_size=float(info.get("trade_tick_size") or (0.0 if strict else fallback.get("backtest_tick_size", 0.0))),
            volume_min=float(info.get("volume_min") or (0.0 if strict else fallback.get("backtest_volume_min", 0.0))),
            volume_max=float(info.get("volume_max") or (0.0 if strict else fallback.get("backtest_volume_max", 0.0))),
            volume_step=float(info.get("volume_step") or (0.0 if strict else fallback.get("backtest_volume_step", 0.0))),
            point=float(info.get("point") or (0.0 if strict else fallback.get("backtest_tick_size", 0.0))),
            stop_level_points=float(info.get("trade_stops_level") or 0.0),
            broker_validated=bool(info),
        )
        if strict:
            RiskManager.require_demo_live_symbol_spec(spec)
        return spec


@dataclass
class RiskState:
    equity: float
    balance: float | None = None
    session_pnl: float = 0.0
    daily_pnl: float = 0.0
    trades_this_session: int = 0
    consecutive_losses: int = 0
    consecutive_losses_symbol: int = 0
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

    def calculate_position_size_with_broker(
        self,
        broker_api: Any,
        symbol: str,
        direction: str,
        equity: float,
        entry_price: float,
        stop_loss: float,
        symbol_spec: SymbolTradingSpec,
        risk_percent: float | None = None,
        tolerance_percent: float | None = None,
    ) -> tuple[float, dict[str, Any]]:
        self.require_demo_live_symbol_spec(symbol_spec)
        configured_risk = float(
            risk_percent
            if risk_percent is not None
            else self.risk.get("risk_per_trade_percent", 0.25)
        )
        risk_amount = equity * configured_risk / 100
        tolerance_percent = float(
            self.position_sizing.get("risk_tolerance_percent", 0.02)
            if tolerance_percent is None
            else tolerance_percent
        )
        order_type = getattr(broker_api, "ORDER_TYPE_BUY", 0) if direction.upper() == "BUY" else getattr(broker_api, "ORDER_TYPE_SELL", 1)
        loss_one_lot = broker_api.order_calc_profit(order_type, symbol, 1.0, entry_price, stop_loss)
        if loss_one_lot is None or abs(float(loss_one_lot)) <= 0:
            raise ValueError("order_calc_profit could not validate monetary risk")
        raw_volume = risk_amount / abs(float(loss_one_lot))
        rounded = normalize_volume(
            raw_volume,
            symbol_spec.volume_min,
            symbol_spec.volume_max,
            symbol_spec.volume_step,
            rounding=self.position_sizing.get("volume_rounding", "floor"),
        )
        if raw_volume < symbol_spec.volume_min:
            minimum_loss = broker_api.order_calc_profit(
                order_type, symbol, symbol_spec.volume_min, entry_price, stop_loss
            )
            if minimum_loss is None or abs(float(minimum_loss)) > risk_amount * (1 + tolerance_percent):
                raise ValueError("Broker minimum volume exceeds the permitted monetary risk")
        recalculated = broker_api.order_calc_profit(order_type, symbol, rounded, entry_price, stop_loss)
        if recalculated is None:
            raise ValueError("order_calc_profit failed after volume rounding")
        loss_after_rounding = abs(float(recalculated))
        while rounded > symbol_spec.volume_min and loss_after_rounding > risk_amount * (1 + tolerance_percent):
            rounded = normalize_volume(
                rounded - symbol_spec.volume_step,
                symbol_spec.volume_min,
                symbol_spec.volume_max,
                symbol_spec.volume_step,
                rounding="floor",
            )
            recalculated = broker_api.order_calc_profit(order_type, symbol, rounded, entry_price, stop_loss)
            if recalculated is None:
                raise ValueError("order_calc_profit failed while reducing rounded volume")
            loss_after_rounding = abs(float(recalculated))
        if loss_after_rounding > risk_amount * (1 + tolerance_percent):
            raise ValueError("Rounded volume exceeds the permitted monetary risk")
        margin = broker_api.order_calc_margin(order_type, symbol, rounded, entry_price)
        if margin is None and self.position_sizing.get("require_order_calc_margin", True):
            raise ValueError("order_calc_margin could not validate required margin")
        diagnostics = {
            "equity": equity,
            "risk_target_percent": configured_risk,
            "risk_target_amount": risk_amount,
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "stop_distance": abs(entry_price - stop_loss),
            "loss_per_lot": abs(float(loss_one_lot)),
            "volume_raw": raw_volume,
            "volume_rounded": rounded,
            "loss_after_rounding": loss_after_rounding,
            "estimated_margin": float(margin) if margin is not None else None,
            "tolerance_percent": tolerance_percent,
        }
        return rounded, diagnostics

    @staticmethod
    def require_demo_live_symbol_spec(symbol_spec: SymbolTradingSpec) -> None:
        missing: list[str] = []
        if symbol_spec.tick_size <= 0:
            missing.append("tick_size")
        if symbol_spec.tick_value <= 0:
            missing.append("tick_value")
        if symbol_spec.volume_min <= 0:
            missing.append("volume_min")
        if symbol_spec.volume_step <= 0:
            missing.append("volume_step")
        if symbol_spec.volume_max < symbol_spec.volume_min:
            missing.append("volume_max")
        if symbol_spec.point <= 0:
            missing.append("point")
        if not symbol_spec.broker_validated:
            missing.append("symbol_info")
        if missing:
            raise ValueError(f"Missing broker symbol fields required in demo_live: {', '.join(missing)}")

    def validate_trade(
        self,
        state: RiskState,
        direction: str,
        lot: float,
        entry_price: float,
        stop_loss: float | None,
        take_profit: float | None,
        symbol_spec: SymbolTradingSpec,
        max_risk_percent: float | None = None,
        min_risk_reward: float | None = None,
        max_trades_per_session: int | None = None,
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
        risk_limit = float(max_risk_percent if max_risk_percent is not None else self.risk.get("max_risk_per_trade_percent", 0.5))
        if risk_percent > risk_limit:
            reasons.append(
                f"Risk per trade too high: {risk_percent:.3f}% > {risk_limit}%"
            )
        rr_limit = float(min_risk_reward if min_risk_reward is not None else self.risk.get("min_risk_reward", 0.8))
        if risk_reward < rr_limit:
            reasons.append(f"Risk/reward too low: {risk_reward:.2f}")
        trade_limit = int(max_trades_per_session if max_trades_per_session is not None else self.risk.get("max_trades_per_session", 0))
        if trade_limit > 0 and state.trades_this_session >= trade_limit:
            reasons.append("Maximum trades per session reached")
        if state.consecutive_losses >= int(self.risk.get("max_consecutive_losses_global", 4)):
            reasons.append("Maximum consecutive losses reached")
        if state.consecutive_losses_symbol >= int(self.risk.get("max_consecutive_losses_per_symbol", 2)):
            reasons.append("Maximum consecutive losses for symbol reached")
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

    def adjusted_risk_after_losses(self, base_risk_percent: float, consecutive_losses_symbol: int) -> float:
        if consecutive_losses_symbol < 2:
            return base_risk_percent
        multiplier = float(self.risk.get("risk_multiplier_after_two_losses", 0.5))
        return max(base_risk_percent * min(multiplier, 1.0), 0.0)


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

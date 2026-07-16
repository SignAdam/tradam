"""Active trade-management rules: trailing stop, break-even, inverse signal, session end."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TradeAdjustment:
    action: str
    reasons: list[str] = field(default_factory=list)
    new_stop_loss: float | None = None
    close_price: float | None = None


class TradeManager:
    def __init__(self, trade_management_config: dict[str, Any]) -> None:
        self.config = trade_management_config

    def evaluate_open_trade(
        self,
        trade: dict[str, Any],
        current_price: float,
        atr_value: float,
        inverse_signal: bool = False,
        session_close_required: bool = False,
    ) -> TradeAdjustment:
        reasons: list[str] = []
        direction = str(trade["direction"]).upper()
        entry = float(trade["entry_price"])
        stop_loss = float(trade["stop_loss"])
        risk = abs(entry - stop_loss)
        if risk <= 0:
            return TradeAdjustment("CLOSE", ["Invalid trade risk geometry"], close_price=current_price)

        if session_close_required and self.config.get("close_before_session_end", True):
            return TradeAdjustment("CLOSE", ["Session is ending"], close_price=current_price)
        if inverse_signal and self.config.get("close_on_inverse_signal", True):
            return TradeAdjustment("CLOSE", ["Inverse signal detected"], close_price=current_price)

        new_stop = stop_loss
        if self.config.get("break_even_enabled", True):
            if direction == "BUY" and current_price - entry >= risk * float(self.config.get("break_even_after_r", 0.8)):
                new_stop = max(new_stop, entry)
                reasons.append("Break-even activated")
            if direction == "SELL" and entry - current_price >= risk * float(self.config.get("break_even_after_r", 0.8)):
                new_stop = min(new_stop, entry)
                reasons.append("Break-even activated")

        if self.config.get("trailing_stop_enabled", True):
            trailing_distance = atr_value * float(self.config.get("trailing_atr_multiplier", 1.0))
            if direction == "BUY":
                candidate = current_price - trailing_distance
                if candidate > new_stop:
                    new_stop = candidate
                    reasons.append("Trailing stop moved up")
            else:
                candidate = current_price + trailing_distance
                if candidate < new_stop:
                    new_stop = candidate
                    reasons.append("Trailing stop moved down")

        if reasons and new_stop != stop_loss:
            return TradeAdjustment("MODIFY_SL", reasons, new_stop_loss=round(new_stop, 5))
        return TradeAdjustment("HOLD", ["No management action required"])


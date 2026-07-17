"""Adaptive spread and TP1 net-cost validation."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from statistics import mean
from typing import Any


@dataclass
class SpreadCostDecision:
    allowed: bool
    reason: str | None
    spread_points: float
    recent_average_points: float
    spread_percentile: float
    spread_to_atr: float
    spread_cost: float
    gross_tp1_profit: float
    expected_tp1_net_profit: float
    spread_cost_ratio: float
    recent_slippage_points: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SpreadCostFilter:
    def __init__(self, broker_api: Any, history_size: int = 120) -> None:
        self.broker = broker_api
        self.spreads: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=history_size))
        self.slippage: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=history_size))

    def record_slippage(self, symbol: str, slippage_points: float) -> None:
        self.slippage[symbol].append(abs(float(slippage_points)))

    def evaluate(
        self,
        symbol: str,
        direction: str,
        volume: float,
        bid: float,
        ask: float,
        point: float,
        atr_value: float,
        tp1: float,
        profile: dict[str, Any],
        commission_estimate: float = 0.0,
    ) -> SpreadCostDecision:
        spread_points = (ask - bid) / point if point > 0 else float("inf")
        history = self.spreads[symbol]
        history.append(spread_points)
        recent_average = mean(history) if history else spread_points
        percentile = self._percentile_rank(list(history), spread_points)
        spread_to_atr = (ask - bid) / atr_value if atr_value > 0 else float("inf")
        order_type = (
            getattr(self.broker, "ORDER_TYPE_BUY", 0)
            if direction.upper() == "BUY"
            else getattr(self.broker, "ORDER_TYPE_SELL", 1)
        )
        entry = ask if direction.upper() == "BUY" else bid
        gross = self.broker.order_calc_profit(order_type, symbol, volume, entry, tp1)
        if gross is None:
            return self._rejected(
                "ORDER_CALC_PROFIT_UNAVAILABLE", spread_points, recent_average, percentile, spread_to_atr
            )
        gross_profit = max(float(gross), 0.0)
        spread_reference_exit = bid if direction.upper() == "BUY" else ask
        spread_value = self.broker.order_calc_profit(order_type, symbol, volume, entry, spread_reference_exit)
        spread_cost = abs(float(spread_value or 0.0))
        slippage_points = mean(self.slippage[symbol]) if self.slippage[symbol] else 0.0
        slippage_price = slippage_points * point
        slippage_exit = tp1 - slippage_price if direction.upper() == "BUY" else tp1 + slippage_price
        after_slippage = self.broker.order_calc_profit(order_type, symbol, volume, entry, slippage_exit)
        expected_net = float(after_slippage if after_slippage is not None else gross) - abs(commission_estimate)
        ratio = spread_cost / gross_profit if gross_profit > 0 else float("inf")
        max_spread = float(profile.get("max_spread_points", float("inf")))
        max_ratio = float(profile.get("max_spread_cost_ratio", 0.25))
        minimum_net = float(profile.get("minimum_net_profit", 0.0))
        reason = None
        if spread_points > max_spread:
            reason = "SPREAD_TOO_HIGH"
        elif ratio >= max_ratio:
            reason = "COST_TOO_HIGH_FOR_TIGHT_TP"
        elif expected_net <= minimum_net:
            reason = "COST_TOO_HIGH_FOR_TIGHT_TP"
        return SpreadCostDecision(
            allowed=reason is None,
            reason=reason,
            spread_points=round(spread_points, 6),
            recent_average_points=round(recent_average, 6),
            spread_percentile=round(percentile, 4),
            spread_to_atr=round(spread_to_atr, 6),
            spread_cost=round(spread_cost, 4),
            gross_tp1_profit=round(gross_profit, 4),
            expected_tp1_net_profit=round(expected_net, 4),
            spread_cost_ratio=round(ratio, 4) if ratio != float("inf") else float("inf"),
            recent_slippage_points=round(slippage_points, 4),
        )

    @staticmethod
    def _percentile_rank(values: list[float], value: float) -> float:
        if not values:
            return 0.0
        return len([item for item in values if item <= value]) / len(values)

    @staticmethod
    def _rejected(
        reason: str,
        spread_points: float,
        average: float,
        percentile: float,
        spread_to_atr: float,
    ) -> SpreadCostDecision:
        return SpreadCostDecision(
            False,
            reason,
            spread_points,
            average,
            percentile,
            spread_to_atr,
            0.0,
            0.0,
            0.0,
            float("inf"),
            0.0,
        )

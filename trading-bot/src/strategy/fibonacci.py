"""Fibonacci swing detection and confluence helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


RETRACEMENTS = {
    "23.6%": 0.236,
    "38.2%": 0.382,
    "50.0%": 0.500,
    "61.8%": 0.618,
    "78.6%": 0.786,
}
EXTENSIONS = {
    "127.2%": 1.272,
    "161.8%": 1.618,
}


@dataclass(frozen=True)
class Swing:
    direction: str
    low_price: float
    high_price: float
    low_index: int
    high_index: int

    @property
    def size(self) -> float:
        return abs(self.high_price - self.low_price)


def detect_last_significant_swing(
    df: pd.DataFrame,
    lookback: int = 120,
    pivot_window: int = 3,
    min_atr_multiplier: float = 0.8,
    atr_column: str = "atr_14",
) -> Swing | None:
    if len(df) < max(pivot_window * 2 + 2, 10):
        return None
    recent = df.tail(lookback).reset_index(drop=True)
    high = recent["high"].astype(float)
    low = recent["low"].astype(float)
    pivot_highs: list[tuple[int, float]] = []
    pivot_lows: list[tuple[int, float]] = []

    for idx in range(pivot_window, len(recent) - pivot_window):
        high_slice = high.iloc[idx - pivot_window : idx + pivot_window + 1]
        low_slice = low.iloc[idx - pivot_window : idx + pivot_window + 1]
        if high.iloc[idx] == high_slice.max():
            pivot_highs.append((idx, float(high.iloc[idx])))
        if low.iloc[idx] == low_slice.min():
            pivot_lows.append((idx, float(low.iloc[idx])))

    if atr_column in recent.columns:
        atr_value = float(recent[atr_column].tail(30).median())
    else:
        atr_value = float((high - low).tail(30).median())
    min_move = max(atr_value * min_atr_multiplier, (high.max() - low.min()) * 0.05)

    candidates: list[tuple[int, str, float]] = [
        *[(idx, "high", price) for idx, price in pivot_highs],
        *[(idx, "low", price) for idx, price in pivot_lows],
    ]
    candidates.sort(key=lambda item: item[0])
    for current_idx, current_type, current_price in reversed(candidates):
        for previous_idx, previous_type, previous_price in reversed(candidates):
            if previous_idx >= current_idx or previous_type == current_type:
                continue
            if abs(current_price - previous_price) < min_move:
                continue
            if previous_type == "low" and current_type == "high":
                return Swing("up", previous_price, current_price, previous_idx, current_idx)
            if previous_type == "high" and current_type == "low":
                return Swing("down", current_price, previous_price, current_idx, previous_idx)

    high_idx = int(high.idxmax())
    low_idx = int(low.idxmin())
    if abs(float(high.max()) - float(low.min())) < min_move:
        return None
    direction = "up" if low_idx < high_idx else "down"
    return Swing(direction, float(low.min()), float(high.max()), low_idx, high_idx)


def calculate_fibonacci_levels(swing: Swing) -> dict[str, float]:
    price_range = swing.high_price - swing.low_price
    levels: dict[str, float] = {}
    if swing.direction == "up":
        for name, ratio in RETRACEMENTS.items():
            levels[name] = swing.high_price - price_range * ratio
        for name, ratio in EXTENSIONS.items():
            levels[name] = swing.high_price + price_range * (ratio - 1)
    else:
        for name, ratio in RETRACEMENTS.items():
            levels[name] = swing.low_price + price_range * ratio
        for name, ratio in EXTENSIONS.items():
            levels[name] = swing.low_price - price_range * (ratio - 1)
    return levels


def nearest_fibonacci_level(
    price: float,
    levels: dict[str, float],
    tolerance: float,
    preferred: set[str] | None = None,
) -> dict[str, Any] | None:
    candidates = levels.items()
    if preferred:
        preferred_matches = [(name, value) for name, value in levels.items() if name in preferred]
        candidates = preferred_matches or levels.items()
    nearest_name, nearest_value = min(candidates, key=lambda item: abs(price - item[1]))
    distance = abs(price - nearest_value)
    if distance <= tolerance:
        return {"level": nearest_name, "price": nearest_value, "distance": distance}
    return None


from __future__ import annotations

from src.strategy.fibonacci import Swing, calculate_fibonacci_levels, nearest_fibonacci_level


def test_fibonacci_retracement_and_extension_for_up_swing() -> None:
    levels = calculate_fibonacci_levels(Swing("up", low_price=100, high_price=120, low_index=1, high_index=10))
    assert round(levels["50.0%"], 2) == 110.00
    assert round(levels["61.8%"], 2) == 107.64
    assert round(levels["127.2%"], 2) == 125.44


def test_nearest_fibonacci_level_respects_tolerance() -> None:
    levels = {"38.2%": 112.36, "50.0%": 110.0, "61.8%": 107.64}
    match = nearest_fibonacci_level(110.05, levels, tolerance=0.1, preferred={"50.0%"})
    assert match is not None
    assert match["level"] == "50.0%"
    assert nearest_fibonacci_level(111.0, levels, tolerance=0.1) is None


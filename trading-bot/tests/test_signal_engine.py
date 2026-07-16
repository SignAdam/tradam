from __future__ import annotations

import pandas as pd

from src.strategy.signal_engine import SignalEngine


def frame_for_entry() -> pd.DataFrame:
    up = [90 + index * (20 / 60) for index in range(61)]
    down = [110 - index * (10.35 / 68) for index in range(69)]
    closes = up + down
    frame = pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=len(closes), freq="5min", tz="UTC"),
            "open": closes,
            "high": [price + 0.2 for price in closes],
            "low": [price - 0.2 for price in closes],
            "close": closes,
            "volume": [100] * len(closes),
        }
    )
    frame["ema_20"] = frame["close"].iloc[-1] + 0.02
    frame["ema_50"] = frame["close"].iloc[-1] - 2
    frame["ema_200"] = frame["close"].iloc[-1] - 5
    frame["rsi_14"] = 55
    frame["atr_14"] = 0.35
    frame["macd_hist"] = 0.2
    frame["adx_14"] = 25
    frame["bb_width"] = 2.0
    frame["recent_support"] = frame["close"].iloc[-1] - 0.05
    frame["recent_resistance"] = frame["close"].iloc[-1] + 4
    return frame


def test_signal_engine_accepts_confluence_not_single_indicator() -> None:
    strategy = {
        "entry_timeframe": "M5",
        "confirmation_timeframe": "M15",
        "trend_timeframe": "H1",
        "score_threshold": 7,
        "min_risk_reward": 1.4,
        "filters": {
            "min_adx": 18,
            "min_atr_to_price": 0.0001,
            "max_atr_to_price": 0.02,
            "min_bollinger_width_to_price": 0.0001,
            "avoid_range_enabled": True,
            "fibonacci_tolerance_atr": 0.4,
            "pullback_ema_tolerance_atr": 0.3,
        },
        "exits": {"atr_stop_multiplier": 1.2, "atr_take_profit_multiplier": 1.8},
    }
    entry = frame_for_entry()
    confirm = pd.DataFrame({"close": [101], "ema_20": [100]})
    trend = pd.DataFrame({"close": [120], "ema_20": [118], "ema_50": [115], "ema_200": [100]})
    decision = SignalEngine(strategy, {"symbols": {"XAUUSD": {"max_spread_points": 50}}}).evaluate(
        "XAUUSD",
        "XAUUSD",
        "US",
        {"M5": entry, "M15": confirm, "H1": trend},
        news_context={"blocked": False, "sentiment": "neutral", "score": 0.0},
        market_context={"spread_points": 10},
    )
    assert decision.decision == "ACCEPTED"
    assert decision.score >= 7
    assert any("Fibonacci" in reason for reason in decision.reasons)

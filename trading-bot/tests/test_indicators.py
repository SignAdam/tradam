from __future__ import annotations

import pandas as pd

from src.strategy.indicators import add_indicators, rsi


def make_frame(rows: int = 240) -> pd.DataFrame:
    prices = [100 + index * 0.05 for index in range(rows)]
    return pd.DataFrame(
        {
            "time": pd.date_range("2026-01-01", periods=rows, freq="5min", tz="UTC"),
            "open": prices,
            "high": [price + 0.2 for price in prices],
            "low": [price - 0.2 for price in prices],
            "close": prices,
            "volume": [100 + index for index in range(rows)],
        }
    )


def test_add_indicators_contains_required_columns() -> None:
    frame = add_indicators(make_frame())
    for column in ["ema_20", "ema_50", "ema_200", "rsi_14", "atr_14", "macd_hist", "adx_14", "bb_width", "vwap"]:
        assert column in frame.columns
    assert frame["ema_20"].iloc[-1] > frame["ema_50"].iloc[-1]
    assert frame["atr_14"].iloc[-1] > 0


def test_rsi_stays_between_zero_and_hundred() -> None:
    values = rsi(pd.Series([1, 2, 3, 2, 4, 5, 4, 6, 7, 8, 7, 9, 10, 11, 10, 12]), 14)
    assert values.between(0, 100).all()


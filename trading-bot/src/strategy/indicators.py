"""Technical indicators for multi-timeframe scalping analysis."""

from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.astype(float).diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    value = 100 - (100 / (1 + rs))
    return value.fillna(50.0)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    macd_line = ema(series, fast) - ema(series, slow)
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=df.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=df.index,
    )
    true_range = pd.concat(
        [
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr_value = true_range.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_value
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_value
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    adx_value = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return pd.DataFrame(
        {
            f"adx_{period}": adx_value.fillna(0.0),
            f"plus_di_{period}": plus_di.fillna(0.0),
            f"minus_di_{period}": minus_di.fillna(0.0),
        }
    )


def bollinger_bands(
    series: pd.Series, period: int = 20, std_multiplier: float = 2.0
) -> pd.DataFrame:
    middle = series.astype(float).rolling(period, min_periods=period).mean()
    std = series.astype(float).rolling(period, min_periods=period).std(ddof=0)
    upper = middle + std_multiplier * std
    lower = middle - std_multiplier * std
    width = upper - lower
    return pd.DataFrame(
        {
            "bb_middle": middle,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_width": width,
        }
    )


def vwap(df: pd.DataFrame) -> pd.Series:
    volume = df.get("volume", pd.Series(1.0, index=df.index)).astype(float).replace(0, np.nan)
    typical_price = (df["high"].astype(float) + df["low"].astype(float) + df["close"].astype(float)) / 3
    cumulative_value = (typical_price * volume).cumsum()
    cumulative_volume = volume.cumsum()
    return (cumulative_value / cumulative_volume).ffill()


def support_resistance(df: pd.DataFrame, lookback: int = 60) -> pd.DataFrame:
    resistance = df["high"].astype(float).rolling(lookback, min_periods=2).max().shift(1)
    support = df["low"].astype(float).rolling(lookback, min_periods=2).min().shift(1)
    return pd.DataFrame({"recent_support": support, "recent_resistance": resistance})


def daily_pivots(df: pd.DataFrame) -> pd.DataFrame:
    if "time" not in df.columns:
        return pd.DataFrame(index=df.index, data={"pivot": np.nan, "pivot_r1": np.nan, "pivot_s1": np.nan})
    frame = df[["time", "high", "low", "close"]].copy()
    frame["date"] = pd.to_datetime(frame["time"]).dt.date
    daily = frame.groupby("date").agg({"high": "max", "low": "min", "close": "last"}).shift(1)
    pivots = (daily["high"] + daily["low"] + daily["close"]) / 3
    daily["pivot"] = pivots
    daily["pivot_r1"] = (2 * pivots) - daily["low"]
    daily["pivot_s1"] = (2 * pivots) - daily["high"]
    merged = frame[["date"]].merge(
        daily[["pivot", "pivot_r1", "pivot_s1"]],
        left_on="date",
        right_index=True,
        how="left",
    )
    return merged[["pivot", "pivot_r1", "pivot_s1"]].set_index(df.index)


def add_indicators(df: pd.DataFrame, config: dict | None = None) -> pd.DataFrame:
    cfg = config or {}
    ema_fast = int(cfg.get("ema_fast", 20))
    ema_mid = int(cfg.get("ema_mid", 50))
    ema_slow = int(cfg.get("ema_slow", 200))
    rsi_period = int(cfg.get("rsi_period", 14))
    atr_period = int(cfg.get("atr_period", 14))
    adx_period = int(cfg.get("adx_period", 14))
    bollinger_period = int(cfg.get("bollinger_period", 20))
    bollinger_std = float(cfg.get("bollinger_std", 2.0))
    sr_lookback = int(cfg.get("support_resistance_lookback", 60))
    macd_fast = int(cfg.get("macd_fast", 12))
    macd_slow = int(cfg.get("macd_slow", 26))
    macd_signal = int(cfg.get("macd_signal", 9))

    frame = df.copy()
    close = frame["close"].astype(float)
    frame[f"ema_{ema_fast}"] = ema(close, ema_fast)
    frame[f"ema_{ema_mid}"] = ema(close, ema_mid)
    frame[f"ema_{ema_slow}"] = ema(close, ema_slow)
    frame[f"rsi_{rsi_period}"] = rsi(close, rsi_period)
    frame[f"atr_{atr_period}"] = atr(frame, atr_period)
    macd_line, signal_line, histogram = macd(close, macd_fast, macd_slow, macd_signal)
    frame["macd"] = macd_line
    frame["macd_signal"] = signal_line
    frame["macd_hist"] = histogram
    frame = pd.concat([frame, adx(frame, adx_period)], axis=1)
    frame = pd.concat([frame, bollinger_bands(close, bollinger_period, bollinger_std)], axis=1)
    if cfg.get("vwap_enabled", True):
        frame["vwap"] = vwap(frame)
    frame = pd.concat([frame, support_resistance(frame, sr_lookback)], axis=1)
    frame = pd.concat([frame, daily_pivots(frame)], axis=1)
    return frame


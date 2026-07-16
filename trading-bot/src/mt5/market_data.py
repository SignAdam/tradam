"""Market-data access through the official MetaTrader5 Python package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.mt5.connection import mt5
from src.utils.exceptions import DataUnavailableError, MT5ConnectionError


TIMEFRAME_NAMES = {
    "M1": "TIMEFRAME_M1",
    "M5": "TIMEFRAME_M5",
    "M15": "TIMEFRAME_M15",
    "H1": "TIMEFRAME_H1",
    "H4": "TIMEFRAME_H4",
    "D1": "TIMEFRAME_D1",
}


@dataclass
class TickSnapshot:
    bid: float
    ask: float
    last: float | None
    spread_points: float


def _timeframe_constant(name: str) -> int:
    if mt5 is None:
        raise MT5ConnectionError("MetaTrader5 package is not available.")
    attr = TIMEFRAME_NAMES.get(name.upper())
    if not attr or not hasattr(mt5, attr):
        raise DataUnavailableError(f"Unsupported MT5 timeframe: {name}")
    return int(getattr(mt5, attr))


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "_asdict"):
        return dict(value._asdict())
    if isinstance(value, dict):
        return dict(value)
    return {name: getattr(value, name) for name in dir(value) if not name.startswith("_")}


class MT5MarketData:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def get_available_symbols(self) -> list[str]:
        if mt5 is None:
            raise MT5ConnectionError("MetaTrader5 package is not available.")
        symbols = mt5.symbols_get()
        if symbols is None:
            raise MT5ConnectionError("MT5 symbols_get() returned None.")
        return [symbol.name for symbol in symbols]

    def ensure_symbol_selected(self, symbol: str) -> None:
        if mt5 is None:
            raise MT5ConnectionError("MetaTrader5 package is not available.")
        if not mt5.symbol_select(symbol, True):
            code, message = mt5.last_error()
            raise DataUnavailableError(f"Cannot select symbol {symbol}: {code} {message}")

    def get_rates(self, symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
        if mt5 is None:
            raise MT5ConnectionError("MetaTrader5 package is not available.")
        self.ensure_symbol_selected(symbol)
        rates = mt5.copy_rates_from_pos(symbol, _timeframe_constant(timeframe), 0, int(bars))
        if rates is None or len(rates) == 0:
            code, message = mt5.last_error()
            raise DataUnavailableError(
                f"No rates for {symbol} {timeframe}; MT5 last_error={code} {message}"
            )
        return normalize_rates_dataframe(pd.DataFrame(rates))

    def get_multi_timeframe_rates(
        self, symbol: str, timeframes: list[str], bars_by_timeframe: dict[str, int]
    ) -> dict[str, pd.DataFrame]:
        return {
            timeframe: self.get_rates(symbol, timeframe, bars_by_timeframe.get(timeframe, 300))
            for timeframe in timeframes
        }

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        if mt5 is None:
            raise MT5ConnectionError("MetaTrader5 package is not available.")
        info = _as_dict(mt5.symbol_info(symbol))
        if not info:
            raise DataUnavailableError(f"MT5 symbol_info() returned no data for {symbol}")
        return info

    def tick(self, symbol: str) -> TickSnapshot:
        if mt5 is None:
            raise MT5ConnectionError("MetaTrader5 package is not available.")
        tick = _as_dict(mt5.symbol_info_tick(symbol))
        info = self.symbol_info(symbol)
        if not tick:
            raise DataUnavailableError(f"MT5 symbol_info_tick() returned no data for {symbol}")
        bid = float(tick.get("bid", 0.0))
        ask = float(tick.get("ask", 0.0))
        point = float(info.get("point") or 0.00001)
        spread_points = (ask - bid) / point if point else 0.0
        return TickSnapshot(
            bid=bid,
            ask=ask,
            last=tick.get("last"),
            spread_points=spread_points,
        )

    def market_is_open(self, symbol: str) -> bool:
        info = self.symbol_info(symbol)
        trade_mode = int(info.get("trade_mode", 0) or 0)
        disabled = getattr(mt5, "SYMBOL_TRADE_MODE_DISABLED", -1) if mt5 is not None else -1
        return trade_mode != disabled


def normalize_rates_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    frame = df.copy()
    if "time" in frame.columns:
        if pd.api.types.is_numeric_dtype(frame["time"]):
            frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        else:
            frame["time"] = pd.to_datetime(frame["time"], utc=True)
    rename = {"tick_volume": "volume"}
    for source, target in rename.items():
        if source in frame.columns and target not in frame.columns:
            frame[target] = frame[source]
    required = {"time", "open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise DataUnavailableError(f"Rates dataframe missing columns: {sorted(missing)}")
    if "volume" not in frame.columns:
        frame["volume"] = 1.0
    return frame.sort_values("time").reset_index(drop=True)

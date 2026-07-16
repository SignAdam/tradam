"""Explainable confluence-based signal engine."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from src.strategy.fibonacci import (
    calculate_fibonacci_levels,
    detect_last_significant_swing,
    nearest_fibonacci_level,
)
from src.strategy.indicators import add_indicators
from src.storage.models import SignalDecisionRecord


KEY_FIB_LEVELS = {"38.2%", "50.0%", "61.8%"}


class SignalEngine:
    def __init__(
        self,
        strategy_config: dict[str, Any],
        symbols_config: dict[str, Any] | None = None,
    ) -> None:
        self.config = strategy_config
        self.indicator_config = strategy_config.get("indicators", {})
        self.filters = strategy_config.get("filters", {})
        self.exits = strategy_config.get("exits", {})
        self.symbols_config = (symbols_config or {}).get("symbols", symbols_config or {})

    def evaluate(
        self,
        logical_symbol: str,
        broker_symbol: str,
        session: str | None,
        frames: dict[str, pd.DataFrame],
        news_context: dict[str, Any] | None = None,
        market_context: dict[str, Any] | None = None,
    ) -> SignalDecisionRecord:
        news_context = news_context or {"blocked": False, "sentiment": "neutral", "score": 0.0}
        market_context = market_context or {}
        prepared = self._prepare_frames(frames)
        entry_tf = self.config.get("entry_timeframe", "M5")
        confirm_tf = self.config.get("confirmation_timeframe", "M15")
        trend_tf = self.config.get("trend_timeframe", "H1")

        missing = [tf for tf in (entry_tf, confirm_tf, trend_tf) if tf not in prepared or prepared[tf].empty]
        if missing:
            return self._reject(logical_symbol, session, "DATA_MISSING", [f"Missing frames: {missing}"])

        entry = prepared[entry_tf]
        confirm = prepared[confirm_tf]
        trend = prepared[trend_tf]
        latest = entry.iloc[-1]
        previous = entry.iloc[-2] if len(entry) > 1 else latest
        h1 = trend.iloc[-1]
        m15 = confirm.iloc[-1]

        automatic_rejection = self._automatic_filters(
            logical_symbol, latest, news_context, market_context
        )
        if automatic_rejection:
            return self._reject(logical_symbol, session, automatic_rejection[0], automatic_rejection[1])

        buy = self._score_direction("BUY", entry, latest, previous, m15, h1, news_context)
        sell = self._score_direction("SELL", entry, latest, previous, m15, h1, news_context)
        selected = buy if buy["score"] >= sell["score"] else sell
        threshold = float(self.config.get("score_threshold", 7))
        if selected["score"] < threshold:
            reasons = selected["reasons"] + [f"Score {selected['score']} below threshold {threshold}"]
            return self._reject(
                logical_symbol,
                session,
                "SCORE_BELOW_THRESHOLD",
                reasons,
                direction=selected["direction"],
                score=selected["score"],
                indicators=selected["indicators"],
                news=news_context,
            )

        entry_price = float(latest["close"])
        atr_value = self._number(latest.get("atr_14"), 0.0)
        min_rr = float(self.config.get("min_risk_reward", 1.4))
        stop_multiplier = float(self.exits.get("atr_stop_multiplier", 1.2))
        tp_multiplier = float(self.exits.get("atr_take_profit_multiplier", 1.8))
        stop_distance = max(atr_value * stop_multiplier, abs(entry_price) * 0.0005)
        reward_distance = max(atr_value * tp_multiplier, stop_distance * min_rr)
        if selected["direction"] == "BUY":
            stop_loss = entry_price - stop_distance
            take_profit = entry_price + reward_distance
        else:
            stop_loss = entry_price + stop_distance
            take_profit = entry_price - reward_distance

        risk_payload = {
            "risk_percent": None,
            "lot_size": None,
            "stop_loss_price": round(stop_loss, 5),
            "take_profit_price": round(take_profit, 5),
            "stop_loss_distance": round(stop_distance, 5),
            "take_profit_distance": round(reward_distance, 5),
            "risk_reward": round(reward_distance / stop_distance, 3) if stop_distance else 0.0,
        }
        raw = {
            "symbol": logical_symbol,
            "broker_symbol": broker_symbol,
            "session": session,
            "direction": selected["direction"],
            "score": selected["score"],
            "decision": "ACCEPTED",
            "reasons": selected["reasons"],
            "risk": risk_payload,
            "indicators": selected["indicators"],
            "news": news_context,
            "created_at": datetime.utcnow().isoformat(),
        }
        return SignalDecisionRecord(
            symbol=logical_symbol,
            session=session,
            direction=selected["direction"],
            score=selected["score"],
            decision="ACCEPTED",
            reasons=selected["reasons"],
            risk=risk_payload,
            indicators=selected["indicators"],
            news=news_context,
            raw=raw,
            created_at=raw["created_at"],
        )

    def _prepare_frames(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        prepared = {}
        for timeframe, frame in frames.items():
            if frame is None or frame.empty:
                continue
            has_ohlc = {"open", "high", "low", "close"}.issubset(frame.columns)
            has_core_indicators = {"ema_20", "close"}.issubset(frame.columns)
            if has_ohlc and ("ema_20" not in frame.columns or "rsi_14" not in frame.columns):
                prepared[timeframe] = add_indicators(frame, self.indicator_config)
            elif has_core_indicators:
                prepared[timeframe] = frame.copy()
            else:
                prepared[timeframe] = frame.copy()
        return prepared

    def _automatic_filters(
        self,
        logical_symbol: str,
        latest: pd.Series,
        news_context: dict[str, Any],
        market_context: dict[str, Any],
    ) -> tuple[str, list[str]] | None:
        if news_context.get("blocked"):
            return "NEWS_BLOCK", list(news_context.get("reasons", ["High-impact news window"]))
        spread = market_context.get("spread_points")
        symbol_limit = self.symbols_config.get(logical_symbol, {}).get("max_spread_points")
        if spread is not None and symbol_limit is not None and float(spread) > float(symbol_limit):
            return "SPREAD_TOO_HIGH", [f"Spread {spread} > max {symbol_limit} points"]

        close = self._number(latest.get("close"), 0.0)
        atr_value = self._number(latest.get("atr_14"), 0.0)
        atr_to_price = atr_value / close if close else 0.0
        if atr_to_price < float(self.filters.get("min_atr_to_price", 0.0)):
            return "VOLATILITY_TOO_LOW", [f"ATR/price {atr_to_price:.6f} below threshold"]
        if atr_to_price > float(self.filters.get("max_atr_to_price", 999.0)):
            return "VOLATILITY_ABNORMAL", [f"ATR/price {atr_to_price:.6f} above threshold"]

        adx_value = self._number(latest.get("adx_14"), 0.0)
        if adx_value < float(self.filters.get("min_adx", 0.0)):
            return "ADX_TOO_LOW", [f"ADX {adx_value:.2f} below minimum"]

        if self.filters.get("avoid_range_enabled", True):
            bb_width = self._number(latest.get("bb_width"), 0.0)
            width_to_price = bb_width / close if close else 0.0
            if width_to_price < float(self.filters.get("min_bollinger_width_to_price", 0.0)):
                return "RANGE_TOO_TIGHT", [f"Bollinger width/price {width_to_price:.6f} too low"]
        return None

    def _score_direction(
        self,
        direction: str,
        entry: pd.DataFrame,
        latest: pd.Series,
        previous: pd.Series,
        m15: pd.Series,
        h1: pd.Series,
        news_context: dict[str, Any],
    ) -> dict[str, Any]:
        score = 0.0
        reasons: list[str] = []
        close = self._number(latest.get("close"), 0.0)
        atr_value = self._number(latest.get("atr_14"), 0.0)
        ema20 = self._number(latest.get("ema_20"), close)
        ema50_h1 = self._number(h1.get("ema_50"), close)
        ema200_h1 = self._number(h1.get("ema_200"), close)
        close_h1 = self._number(h1.get("close"), close)
        ema20_m15 = self._number(m15.get("ema_20"), close)
        close_m15 = self._number(m15.get("close"), close)
        rsi_value = self._number(latest.get("rsi_14"), 50.0)
        prev_rsi = self._number(previous.get("rsi_14"), rsi_value)
        macd_hist = self._number(latest.get("macd_hist"), 0.0)
        adx_value = self._number(latest.get("adx_14"), 0.0)

        bullish_h1 = close_h1 > ema200_h1 and ema50_h1 > ema200_h1
        bearish_h1 = close_h1 < ema200_h1 and ema50_h1 < ema200_h1
        if direction == "BUY" and bullish_h1:
            score += 2
            reasons.append("H1 bullish trend: price above EMA200 and EMA50 above EMA200")
        if direction == "SELL" and bearish_h1:
            score += 2
            reasons.append("H1 bearish trend: price below EMA200 and EMA50 below EMA200")

        if direction == "BUY" and close_m15 >= ema20_m15:
            score += 1
            reasons.append("M15 confirms above EMA20")
        if direction == "SELL" and close_m15 <= ema20_m15:
            score += 1
            reasons.append("M15 confirms below EMA20")

        ema_tolerance = atr_value * float(self.filters.get("pullback_ema_tolerance_atr", 0.30))
        if abs(close - ema20) <= ema_tolerance or (
            direction == "BUY" and previous.get("close", close) < previous.get("ema_20", ema20) <= close
        ) or (
            direction == "SELL" and previous.get("close", close) > previous.get("ema_20", ema20) >= close
        ):
            score += 1
            reasons.append(f"{direction} pullback/reclaim around EMA20")

        if direction == "BUY" and (rsi_value >= 50 or (prev_rsi < 50 <= rsi_value)):
            score += 1
            reasons.append("RSI confirms bullish momentum around 50")
        if direction == "SELL" and (rsi_value <= 50 or (prev_rsi > 50 >= rsi_value)):
            score += 1
            reasons.append("RSI confirms bearish momentum around 50")

        if direction == "BUY" and macd_hist > 0:
            score += 1
            reasons.append("MACD histogram confirms bullish momentum")
        if direction == "SELL" and macd_hist < 0:
            score += 1
            reasons.append("MACD histogram confirms bearish momentum")

        fib_match = self._fibonacci_match(entry, close, atr_value)
        if fib_match:
            score += 2
            reasons.append(f"Price near Fibonacci {fib_match['level']} confluence")

        sr_match = self._support_resistance_match(direction, latest, close, atr_value)
        if sr_match:
            score += 1
            reasons.append(sr_match)

        breakout = self._breakout_match(direction, latest, close, atr_value)
        if breakout:
            score += 1
            reasons.append(breakout)

        if adx_value >= float(self.filters.get("min_adx", 18)):
            score += 1
            reasons.append("ADX confirms trend strength")

        sentiment = str(news_context.get("sentiment", "neutral")).lower()
        sentiment_score = float(news_context.get("score", 0.0) or 0.0)
        if direction == "BUY" and sentiment in {"bullish", "positive"}:
            score += 1
            reasons.append("News sentiment supports long bias")
        if direction == "SELL" and sentiment in {"bearish", "negative"}:
            score += 1
            reasons.append("News sentiment supports short bias")
        if (direction == "BUY" and sentiment_score < -0.25) or (
            direction == "SELL" and sentiment_score > 0.25
        ):
            score -= 2
            reasons.append("News sentiment conflicts with direction")

        indicators = {
            "close": round(close, 5),
            "ema20": round(ema20, 5),
            "ema50_h1": round(ema50_h1, 5),
            "ema200_h1": round(ema200_h1, 5),
            "rsi": round(rsi_value, 2),
            "atr": round(atr_value, 5),
            "macd_hist": round(macd_hist, 5),
            "adx": round(adx_value, 2),
            "fibonacci": fib_match,
        }
        return {"direction": direction, "score": score, "reasons": reasons, "indicators": indicators}

    def _fibonacci_match(self, entry: pd.DataFrame, close: float, atr_value: float) -> dict[str, Any] | None:
        swing = detect_last_significant_swing(entry, atr_column="atr_14")
        if not swing:
            return None
        levels = calculate_fibonacci_levels(swing)
        tolerance = max(atr_value * float(self.filters.get("fibonacci_tolerance_atr", 0.35)), 1e-9)
        return nearest_fibonacci_level(close, levels, tolerance, preferred=KEY_FIB_LEVELS)

    @staticmethod
    def _support_resistance_match(direction: str, latest: pd.Series, close: float, atr_value: float) -> str | None:
        tolerance = max(atr_value * 0.35, 1e-9)
        support = latest.get("recent_support")
        resistance = latest.get("recent_resistance")
        if direction == "BUY" and pd.notna(support) and abs(close - float(support)) <= tolerance:
            return "Price rejects recent support"
        if direction == "SELL" and pd.notna(resistance) and abs(close - float(resistance)) <= tolerance:
            return "Price rejects recent resistance"
        return None

    def _breakout_match(self, direction: str, latest: pd.Series, close: float, atr_value: float) -> str | None:
        buffer = atr_value * float(self.filters.get("breakout_buffer_atr", 0.15))
        support = latest.get("recent_support")
        resistance = latest.get("recent_resistance")
        if direction == "BUY" and pd.notna(resistance) and close > float(resistance) + buffer:
            return "Confirmed bullish breakout above recent resistance"
        if direction == "SELL" and pd.notna(support) and close < float(support) - buffer:
            return "Confirmed bearish breakout below recent support"
        return None

    @staticmethod
    def _number(value: Any, default: float) -> float:
        try:
            if value is None or pd.isna(value):
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _reject(
        symbol: str,
        session: str | None,
        reason_code: str,
        reasons: list[str],
        direction: str | None = None,
        score: float = 0.0,
        indicators: dict[str, Any] | None = None,
        news: dict[str, Any] | None = None,
    ) -> SignalDecisionRecord:
        return SignalDecisionRecord(
            symbol=symbol,
            session=session,
            direction=direction,
            score=score,
            decision="REJECTED",
            reasons=reasons,
            indicators=indicators or {},
            news=news or {},
            rejected_reason=reason_code,
            created_at=datetime.utcnow().isoformat(),
            raw={"reason_code": reason_code, "reasons": reasons},
        )

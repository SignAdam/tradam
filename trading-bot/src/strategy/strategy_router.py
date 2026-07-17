"""Market-regime router for the demo_live scalping strategy families."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from src.strategy.fibonacci import calculate_fibonacci_levels, detect_last_significant_swing, nearest_fibonacci_level
from src.strategy.indicators import add_indicators
from src.strategy.profiles import SymbolProfile, load_symbol_profile


STRATEGIES = {
    "FAST_TREND_PULLBACK",
    "VWAP_RECLAIM",
    "EMA9_EMA20_MOMENTUM",
    "BREAKOUT_RETEST",
    "LIQUIDITY_SWEEP_REVERSAL",
    "ASIAN_RANGE_SCALP",
    "OPENING_RANGE_BREAKOUT",
}


@dataclass
class ScoreComponent:
    points: float
    reason: str


@dataclass
class RoutedSignal:
    symbol: str
    broker_symbol: str
    session: str
    profile: str
    strategy: str
    direction: str
    raw_score: float
    bonuses: list[ScoreComponent]
    penalties: list[ScoreComponent]
    final_score: float
    required_score: float
    accepted: bool
    rejection_code: str | None
    reasons: list[str]
    indicators: dict[str, Any]
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    source_candle: str
    structure_id: str
    created_at_utc: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["bonuses"] = [asdict(item) for item in self.bonuses]
        data["penalties"] = [asdict(item) for item in self.penalties]
        return data


class StrategyRouter:
    def __init__(
        self,
        strategy_config: dict[str, Any],
        symbols_config: dict[str, Any],
        sessions_config: dict[str, Any] | None = None,
    ) -> None:
        self.config = strategy_config
        self.symbols_config = symbols_config
        self.symbols = symbols_config.get("symbols", symbols_config)
        self.sessions = (sessions_config or {}).get("sessions", {})
        self.indicators = strategy_config.get("indicators", {})
        self.filters = strategy_config.get("filters", {})
        self._evaluators: dict[str, Callable[..., tuple[float, list[str], list[str]]]] = {
            "FAST_TREND_PULLBACK": self._fast_trend_pullback,
            "VWAP_RECLAIM": self._vwap_reclaim,
            "EMA9_EMA20_MOMENTUM": self._ema_momentum,
            "BREAKOUT_RETEST": self._breakout_retest,
            "LIQUIDITY_SWEEP_REVERSAL": self._liquidity_sweep,
            "ASIAN_RANGE_SCALP": self._asian_range,
            "OPENING_RANGE_BREAKOUT": self._opening_range,
        }

    def route(
        self,
        logical_symbol: str,
        broker_symbol: str,
        session: str | None,
        frames: dict[str, pd.DataFrame],
        news_context: dict[str, Any],
        market_context: dict[str, Any] | None = None,
    ) -> RoutedSignal:
        market_context = market_context or {}
        session_name = session or "OUTSIDE_SESSION"
        profile = load_symbol_profile(logical_symbol, self.symbols_config)
        prepared = self.prepare_frames(frames)
        missing = [name for name in ("M1", "M5", "M15", "H1") if name not in prepared or len(prepared[name]) < 3]
        if missing:
            return self._empty_rejection(
                logical_symbol, broker_symbol, session_name, profile, "BOT_NOT_RECEIVING_DATA", f"Missing data: {missing}"
            )
        if session is None or session not in profile.sessions:
            return self._empty_rejection(
                logical_symbol, broker_symbol, session_name, profile, "OUTSIDE_SESSION", "Symbol is outside its configured session"
            )
        if news_context.get("blocked"):
            reasons = list(news_context.get("reasons") or ["High-impact news protection active"])
            return self._empty_rejection(
                logical_symbol, broker_symbol, session_name, profile, "NEWS_BLOCKING", "; ".join(reasons)
            )

        m1, m5, m15, h1 = prepared["M1"], prepared["M5"], prepared["M15"], prepared["H1"]
        latest = m1.iloc[-1]
        close = self._num(latest.get("close"))
        atr_value = self._num(latest.get("atr_14"))
        if close <= 0 or atr_value <= 0:
            return self._empty_rejection(
                logical_symbol, broker_symbol, session_name, profile, "BOT_NOT_RECEIVING_DATA", "Invalid M1 close or ATR"
            )
        atr_ratio = atr_value / close
        if atr_ratio < float(self.filters.get("min_atr_to_price", 0.0)):
            return self._empty_rejection(
                logical_symbol, broker_symbol, session_name, profile, "NO_VALID_SETUP", "Volatility below configured minimum"
            )
        if atr_ratio > float(self.filters.get("max_atr_to_price", 1.0)):
            return self._empty_rejection(
                logical_symbol, broker_symbol, session_name, profile, "NO_VALID_SETUP", "Abnormal volatility"
            )
        symbol_cfg = self.symbols[logical_symbol]
        spread = market_context.get("spread_points")
        if spread is not None and float(spread) > float(symbol_cfg.get("max_spread_points", float("inf"))):
            return self._empty_rejection(
                logical_symbol, broker_symbol, session_name, profile, "SPREAD_TOO_HIGH", f"Spread {spread} exceeds symbol limit"
            )

        strategies = [name for name in profile.strategies if name in STRATEGIES]
        preferred = self.sessions.get(session_name, {}).get("preferred_strategies", [])
        strategies.sort(key=lambda item: (item not in preferred, profile.strategies.index(item)))
        candidates: list[RoutedSignal] = []
        for strategy in strategies:
            for direction in ("BUY", "SELL"):
                raw_score, reasons, hard_rejections = self._evaluators[strategy](
                    direction, m1, m5, m15, h1, session_name
                )
                bonuses, penalties, adx_reject = self._adaptive_context(
                    strategy, direction, latest, news_context
                )
                if adx_reject:
                    hard_rejections.append(adx_reject)
                final_score = raw_score + sum(item.points for item in bonuses) - sum(item.points for item in penalties)
                stop, tp1, tp2, target_meta = self._build_targets(direction, m1, m5, close, atr_value, profile)
                source_candle = self._time_value(latest.get("time"))
                structure_id = self._structure_id(logical_symbol, direction, m5, h1)
                rejection = hard_rejections[0] if hard_rejections else None
                if rejection is None and final_score < profile.minimum_score:
                    rejection = "SCORE_TOO_LOW"
                candidates.append(
                    RoutedSignal(
                        symbol=logical_symbol,
                        broker_symbol=broker_symbol,
                        session=session_name,
                        profile=profile.profile,
                        strategy=strategy,
                        direction=direction,
                        raw_score=round(raw_score, 3),
                        bonuses=bonuses,
                        penalties=penalties,
                        final_score=round(final_score, 3),
                        required_score=profile.minimum_score,
                        accepted=rejection is None,
                        rejection_code=rejection,
                        reasons=[*reasons, *[item.reason for item in bonuses], *[item.reason for item in penalties]],
                        indicators=self._indicator_snapshot(latest, m5.iloc[-1], m15.iloc[-1], h1.iloc[-1]),
                        entry_price=close,
                        stop_loss=stop,
                        tp1=tp1,
                        tp2=tp2,
                        source_candle=source_candle,
                        structure_id=structure_id,
                        created_at_utc=datetime.now(timezone.utc).isoformat(),
                        metadata={"hard_rejections": hard_rejections, "targets": target_meta, "news": news_context},
                    )
                )
        if not candidates:
            return self._empty_rejection(
                logical_symbol, broker_symbol, session_name, profile, "NO_VALID_SETUP", "No enabled strategy for this session"
            )
        accepted = [candidate for candidate in candidates if candidate.accepted]
        pool = accepted or candidates
        return max(pool, key=lambda item: (item.final_score, item.raw_score))

    def prepare_frames(self, frames: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        prepared: dict[str, pd.DataFrame] = {}
        for timeframe, frame in frames.items():
            if frame is None or frame.empty:
                continue
            required = {"ema_9", "ema_20", "rsi_14", "atr_14", "adx_14", "vwap"}
            prepared[timeframe] = frame.copy() if required.issubset(frame.columns) else add_indicators(frame, self.indicators)
        return prepared

    def _fast_trend_pullback(self, direction: str, m1: pd.DataFrame, m5: pd.DataFrame, m15: pd.DataFrame, h1: pd.DataFrame, session: str) -> tuple[float, list[str], list[str]]:
        del session
        score, reasons, rejects = 0.0, [], []
        if self._trend_aligned(direction, h1.iloc[-1]):
            score += 2.0; reasons.append("H1 trend aligned")
        else:
            rejects.append("H1_TREND_NOT_ALIGNED")
        if self._trend_aligned(direction, m15.iloc[-1], slow=False):
            score += 1.0; reasons.append("M15 structure aligned")
        if self._trend_aligned(direction, m5.iloc[-1], slow=False):
            score += 0.75; reasons.append("M5 confirms direction")
        latest = m1.iloc[-1]
        close, atr = self._num(latest.close), self._num(latest.get("atr_14"))
        anchors = [self._num(latest.get("ema_9")), self._num(latest.get("ema_20")), self._num(latest.get("vwap"))]
        if atr > 0 and min(abs(close - value) for value in anchors if value > 0) <= atr * 0.35:
            score += 1.5; reasons.append("Short pullback to EMA9/EMA20/VWAP")
        if self._momentum_candle(direction, latest):
            score += 0.75; reasons.append("M1 momentum resumed")
        score += self._momentum_confirmation(direction, latest, reasons)
        fib = self._fibonacci_near(m1, close, atr)
        if fib:
            score += 0.5; reasons.append(f"Optional Fibonacci confluence {fib}")
        return score, reasons, rejects

    def _vwap_reclaim(self, direction: str, m1: pd.DataFrame, m5: pd.DataFrame, m15: pd.DataFrame, h1: pd.DataFrame, session: str) -> tuple[float, list[str], list[str]]:
        del session
        score, reasons, rejects = 0.0, [], []
        latest, previous = m1.iloc[-1], m1.iloc[-2]
        if self._trend_aligned(direction, h1.iloc[-1]):
            score += 1.5; reasons.append("H1 context supports VWAP reclaim")
        if self._trend_aligned(direction, m15.iloc[-1], slow=False):
            score += 1.0; reasons.append("M15 context aligned")
        vwap_now, vwap_prev = self._num(latest.get("vwap")), self._num(previous.get("vwap"))
        reclaimed = (
            direction == "BUY" and self._num(previous.close) <= vwap_prev and self._num(latest.close) > vwap_now and self._num(latest.low) <= vwap_now
        ) or (
            direction == "SELL" and self._num(previous.close) >= vwap_prev and self._num(latest.close) < vwap_now and self._num(latest.high) >= vwap_now
        )
        if reclaimed:
            score += 2.5; reasons.append("VWAP reclaimed and retested on M1")
        else:
            rejects.append("VWAP_RECLAIM_NOT_CONFIRMED")
        if self._price_side(direction, m5.iloc[-1], "vwap"):
            score += 1.0; reasons.append("M5 confirms VWAP side")
        score += self._momentum_confirmation(direction, latest, reasons)
        return score, reasons, rejects

    def _ema_momentum(self, direction: str, m1: pd.DataFrame, m5: pd.DataFrame, m15: pd.DataFrame, h1: pd.DataFrame, session: str) -> tuple[float, list[str], list[str]]:
        del h1, session
        score, reasons, rejects = 0.0, [], []
        latest, previous = m1.iloc[-1], m1.iloc[-2]
        ema9, ema20 = self._num(latest.get("ema_9")), self._num(latest.get("ema_20"))
        prev9, prev20 = self._num(previous.get("ema_9")), self._num(previous.get("ema_20"))
        aligned = ema9 > ema20 if direction == "BUY" else ema9 < ema20
        crossed = prev9 <= prev20 and ema9 > ema20 if direction == "BUY" else prev9 >= prev20 and ema9 < ema20
        if aligned:
            score += 1.5; reasons.append("EMA9/EMA20 aligned")
        if crossed:
            score += 0.75; reasons.append("Fresh EMA9/EMA20 cross")
        slope_ok = self._num(latest.get("ema9_slope")) > 0 and self._num(latest.get("ema20_slope")) >= 0 if direction == "BUY" else self._num(latest.get("ema9_slope")) < 0 and self._num(latest.get("ema20_slope")) <= 0
        if slope_ok:
            score += 1.25; reasons.append("EMA slopes confirm momentum")
        atr = self._num(latest.get("atr_14"))
        distance = abs(self._num(latest.close) - ema20) / atr if atr > 0 else float("inf")
        if distance <= float(self.filters.get("max_price_distance_from_ema20_atr", 1.2)):
            score += 1.0; reasons.append("Price is not overextended from EMA20")
        else:
            rejects.append("PRICE_OVEREXTENDED")
        if self._trend_aligned(direction, m5.iloc[-1], slow=False):
            score += 1.25; reasons.append("M5 structure is not opposed")
        elif self._trend_aligned(self._opposite(direction), m15.iloc[-1], slow=False):
            rejects.append("M5_M15_STRUCTURE_OPPOSED")
        score += self._momentum_confirmation(direction, latest, reasons)
        return score, reasons, rejects

    def _breakout_retest(self, direction: str, m1: pd.DataFrame, m5: pd.DataFrame, m15: pd.DataFrame, h1: pd.DataFrame, session: str) -> tuple[float, list[str], list[str]]:
        del m15, h1, session
        score, reasons, rejects = 0.0, [], []
        if len(m1) < 25:
            return score, reasons, ["INSUFFICIENT_CONSOLIDATION_DATA"]
        latest = m1.iloc[-1]
        base = m1.iloc[-22:-2]
        upper, lower = float(base.high.max()), float(base.low.min())
        atr = self._num(latest.get("atr_14"))
        width = upper - lower
        if atr > 0 and width <= atr * 4.5:
            score += 1.0; reasons.append("M1 consolidation detected")
        breakout = (
            direction == "BUY" and self._num(latest.close) > upper and self._num(latest.low) <= upper + atr * 0.2
        ) or (
            direction == "SELL" and self._num(latest.close) < lower and self._num(latest.high) >= lower - atr * 0.2
        )
        if breakout:
            score += 2.5; reasons.append("Breakout and retest confirmed")
        else:
            rejects.append("BREAKOUT_RETEST_NOT_CONFIRMED")
        if self._num(latest.get("atr_change")) > 0 or self._num(latest.get("bb_width_change")) > 0:
            score += 1.0; reasons.append("ATR/Bollinger expansion")
        if self._num(latest.get("adx_change")) > 0:
            score += 0.75; reasons.append("ADX is rising")
        if self._trend_aligned(direction, m5.iloc[-1], slow=False):
            score += 1.25; reasons.append("M5 confirms breakout direction")
        if self._momentum_candle(direction, latest):
            score += 0.75; reasons.append("Breakout candle closes with momentum")
        return score, reasons, rejects

    def _liquidity_sweep(self, direction: str, m1: pd.DataFrame, m5: pd.DataFrame, m15: pd.DataFrame, h1: pd.DataFrame, session: str) -> tuple[float, list[str], list[str]]:
        del m15, h1, session
        score, reasons, rejects = 0.0, [], []
        if len(m1) < 25:
            return score, reasons, ["INSUFFICIENT_SWEEP_DATA"]
        latest = m1.iloc[-1]
        prior = m1.iloc[-22:-1]
        prior_high, prior_low = float(prior.high.max()), float(prior.low.min())
        swept = (
            direction == "BUY" and self._num(latest.low) < prior_low and self._num(latest.close) > prior_low
        ) or (
            direction == "SELL" and self._num(latest.high) > prior_high and self._num(latest.close) < prior_high
        )
        if swept:
            score += 2.75; reasons.append("Liquidity sweep followed by fast reintegration")
        else:
            rejects.append("LIQUIDITY_SWEEP_NOT_CONFIRMED")
        if self._rejection_candle(direction, latest):
            score += 1.25; reasons.append("Visible rejection candle")
        rsi_now = self._num(latest.get("rsi_14"), 50)
        rsi_prior = self._num(prior.iloc[-1].get("rsi_14"), 50)
        divergence = rsi_now > rsi_prior if direction == "BUY" else rsi_now < rsi_prior
        if divergence:
            score += 1.0; reasons.append("RSI divergence/loss of prior momentum")
        if not self._trend_aligned(self._opposite(direction), m5.iloc[-1], slow=False):
            score += 1.0; reasons.append("M5 does not strongly oppose reversal")
        score += self._momentum_confirmation(direction, latest, reasons)
        return score, reasons, rejects

    def _asian_range(self, direction: str, m1: pd.DataFrame, m5: pd.DataFrame, m15: pd.DataFrame, h1: pd.DataFrame, session: str) -> tuple[float, list[str], list[str]]:
        del m15, h1
        score, reasons, rejects = 0.0, [], []
        if session != "Asian" or len(m1) < 35:
            return score, reasons, ["ASIAN_RANGE_SESSION_REQUIRED"]
        latest = m1.iloc[-1]
        range_data = m1.iloc[-32:-2]
        upper, lower = float(range_data.high.max()), float(range_data.low.min())
        width, atr = upper - lower, self._num(latest.get("atr_14"))
        if atr > 0 and 2.0 <= width / atr <= 10.0:
            score += 1.25; reasons.append("Asian range width is tradable")
        else:
            rejects.append("ASIAN_RANGE_INVALID_WIDTH")
        tolerance = atr * 0.45
        near_edge = self._num(latest.low) <= lower + tolerance if direction == "BUY" else self._num(latest.high) >= upper - tolerance
        if near_edge:
            score += 2.0; reasons.append("Entry is near the relevant range boundary")
        else:
            rejects.append("NOT_NEAR_RANGE_BOUNDARY")
        if self._rejection_candle(direction, latest):
            score += 1.25; reasons.append("M1 boundary rejection confirmed")
        rsi = self._num(latest.get("rsi_14"), 50)
        if (direction == "BUY" and rsi <= 48) or (direction == "SELL" and rsi >= 52):
            score += 0.75; reasons.append("RSI supports range mean reversion")
        if self._price_side(direction, latest, "vwap") or self._rejection_candle(direction, latest):
            score += 0.75; reasons.append("VWAP/Bollinger context supports range scalp")
        if not self._trend_aligned(self._opposite(direction), m5.iloc[-1], slow=False):
            score += 0.75; reasons.append("M5 does not invalidate the range entry")
        return score, reasons, rejects

    def _opening_range(self, direction: str, m1: pd.DataFrame, m5: pd.DataFrame, m15: pd.DataFrame, h1: pd.DataFrame, session: str) -> tuple[float, list[str], list[str]]:
        del m15
        score, reasons, rejects = 0.0, [], []
        if session != "US" or "time" not in m1.columns:
            return score, reasons, ["US_OPENING_RANGE_REQUIRED"]
        session_cfg = self.sessions.get("US", {})
        zone = ZoneInfo(session_cfg.get("timezone", "America/New_York"))
        opening = self._parse_time(session_cfg.get("opening_range_start", "09:30"))
        duration = int(session_cfg.get("opening_range_minutes", 15))
        localized = pd.to_datetime(m1.time, utc=True).dt.tz_convert(zone)
        latest_local = localized.iloc[-1]
        start = latest_local.replace(hour=opening.hour, minute=opening.minute, second=0, microsecond=0)
        end = start + pd.Timedelta(minutes=duration)
        mask = (localized >= start) & (localized < end)
        opening_rows = m1.loc[mask]
        if opening_rows.empty or latest_local < end:
            return score, reasons, ["OPENING_RANGE_NOT_COMPLETE"]
        high, low = float(opening_rows.high.max()), float(opening_rows.low.min())
        latest = m1.iloc[-1]
        atr = self._num(latest.get("atr_14"))
        breakout = (
            direction == "BUY" and self._num(latest.close) > high and self._num(latest.low) <= high + atr * 0.25
        ) or (
            direction == "SELL" and self._num(latest.close) < low and self._num(latest.high) >= low - atr * 0.25
        )
        if breakout:
            score += 2.75; reasons.append("US opening range breakout and retest")
        else:
            rejects.append("OPENING_RANGE_BREAKOUT_NOT_CONFIRMED")
        boundary = high if direction == "BUY" else low
        extension = abs(self._num(latest.close) - boundary) / atr if atr > 0 else float("inf")
        if extension <= float(self.config.get("opening_range", {}).get("max_extension_atr", 0.75)):
            score += 1.0; reasons.append("Breakout is not overextended")
        else:
            rejects.append("OPENING_RANGE_OVEREXTENDED")
        if self._momentum_candle(direction, latest):
            score += 1.0; reasons.append("M1 opening momentum confirmed")
        if self._trend_aligned(direction, m5.iloc[-1], slow=False):
            score += 1.25; reasons.append("M5 confirms opening direction")
        if self._trend_aligned(direction, h1.iloc[-1]):
            score += 1.0; reasons.append("H1 context aligned")
        return score, reasons, rejects

    def _adaptive_context(self, strategy: str, direction: str, latest: pd.Series, news: dict[str, Any]) -> tuple[list[ScoreComponent], list[ScoreComponent], str | None]:
        bonuses: list[ScoreComponent] = []
        penalties: list[ScoreComponent] = []
        adx = self._num(latest.get("adx_14"))
        change = self._num(latest.get("adx_change"))
        hard = float(self.filters.get("adx_hard_reject", 10))
        soft = float(self.filters.get("adx_soft_penalty", 15))
        full = float(self.filters.get("adx_full_confirmation", 20))
        range_strategy = strategy == "ASIAN_RANGE_SCALP"
        if range_strategy:
            if adx < soft:
                bonuses.append(ScoreComponent(0.5, "Low ADX is compatible with a clean range"))
            elif adx > full + 8:
                penalties.append(ScoreComponent(1.0, "ADX is high for a range-reversion setup"))
        else:
            if adx < hard:
                return bonuses, penalties, "ADX_HARD_REJECT"
            if adx < soft:
                penalties.append(ScoreComponent(1.0, "ADX soft penalty"))
            elif adx < full:
                bonuses.append(ScoreComponent(0.5, "ADX partial confirmation"))
            else:
                bonuses.append(ScoreComponent(1.0, "ADX full confirmation"))
            if strategy in {"BREAKOUT_RETEST", "OPENING_RANGE_BREAKOUT"} and change >= float(self.filters.get("adx_rising_bonus", 2.5)):
                bonuses.append(ScoreComponent(0.5, "ADX rising bonus for breakout"))
        sentiment = str(news.get("sentiment", "unknown")).lower()
        sentiment_score = self._num(news.get("score"))
        if sentiment not in {"unknown", "neutral"}:
            supports = (direction == "BUY" and sentiment_score > 0) or (direction == "SELL" and sentiment_score < 0)
            if supports:
                bonuses.append(ScoreComponent(0.25, "Usable news sentiment supports direction"))
            else:
                penalties.append(ScoreComponent(0.5, "Usable news sentiment conflicts with direction"))
        if str(news.get("impact", "")).lower() == "medium":
            penalties.append(ScoreComponent(0.5, "Medium-impact news penalty"))
        return bonuses, penalties, None

    def _build_targets(self, direction: str, m1: pd.DataFrame, m5: pd.DataFrame, entry: float, atr: float, profile: SymbolProfile) -> tuple[float, float, float, dict[str, Any]]:
        recent = m1.iloc[-20:]
        if direction == "BUY":
            structural_stop = float(recent.low.min()) - atr * 0.12
            stop = min(structural_stop, entry - atr * 0.45)
        else:
            structural_stop = float(recent.high.max()) + atr * 0.12
            stop = max(structural_stop, entry + atr * 0.45)
        risk = abs(entry - stop)
        if risk <= 0:
            risk = atr
            stop = entry - risk if direction == "BUY" else entry + risk
        latest = m1.iloc[-1]
        candidates = [
            latest.get("recent_resistance") if direction == "BUY" else latest.get("recent_support"),
            latest.get("pivot_r1") if direction == "BUY" else latest.get("pivot_s1"),
            float(m5.iloc[-20:].high.max()) if direction == "BUY" else float(m5.iloc[-20:].low.min()),
            latest.get("vwap"),
        ]
        valid: list[tuple[float, float, str]] = []
        labels = ["support_resistance", "daily_pivot", "m5_swing", "vwap"]
        for label, value in zip(labels, candidates):
            if value is None or pd.isna(value):
                continue
            target = float(value)
            distance = target - entry if direction == "BUY" else entry - target
            r_value = distance / risk if risk > 0 else 0.0
            if distance > 0 and profile.tp1_target_r_min <= r_value <= profile.tp1_target_r_max:
                valid.append((distance, target, label))
        if valid:
            _, tp1, tp1_source = min(valid, key=lambda item: item[0])
        else:
            fallback_r = min(max(atr * 0.65 / risk, profile.tp1_target_r_min), profile.tp1_target_r_max)
            tp1 = entry + risk * fallback_r if direction == "BUY" else entry - risk * fallback_r
            tp1_source = "atr_and_r_fallback"
        tp2_candidates: list[tuple[float, float, str]] = []
        for label, value in zip(labels, candidates):
            if value is None or pd.isna(value):
                continue
            target = float(value)
            distance = target - entry if direction == "BUY" else entry - target
            r_value = distance / risk if risk > 0 else 0.0
            if distance > 0 and r_value >= profile.tp2_target_r_min:
                tp2_candidates.append((distance, target, label))
        max_tp2_distance = risk * profile.tp2_target_r_max
        if tp2_candidates:
            _, structural_tp2, tp2_source = min(tp2_candidates, key=lambda item: item[0])
            distance = min(abs(structural_tp2 - entry), max_tp2_distance)
            tp2 = entry + distance if direction == "BUY" else entry - distance
        else:
            target_r = min(max(atr * 1.2 / risk, profile.tp2_target_r_min), profile.tp2_target_r_max)
            tp2 = entry + risk * target_r if direction == "BUY" else entry - risk * target_r
            tp2_source = "atr_and_r_fallback"
        return stop, tp1, tp2, {
            "stop_source": "m1_structure_with_atr_buffer",
            "tp1_source": tp1_source,
            "tp2_source": tp2_source,
            "risk_price": risk,
            "tp1_r": abs(tp1 - entry) / risk,
            "tp2_r": abs(tp2 - entry) / risk,
        }

    def _momentum_confirmation(self, direction: str, row: pd.Series, reasons: list[str]) -> float:
        score = 0.0
        rsi, macd = self._num(row.get("rsi_14"), 50), self._num(row.get("macd_hist"))
        if (direction == "BUY" and 50 <= rsi <= 72) or (direction == "SELL" and 28 <= rsi <= 50):
            score += 0.5; reasons.append("RSI confirms direction without extreme extension")
        if (direction == "BUY" and macd > 0) or (direction == "SELL" and macd < 0):
            score += 0.5; reasons.append("MACD histogram confirms momentum")
        return score

    @staticmethod
    def _trend_aligned(direction: str, row: pd.Series, slow: bool = True) -> bool:
        close = float(row.get("close", 0.0) or 0.0)
        ema20 = float(row.get("ema_20", close) or close)
        ema50 = float(row.get("ema_50", ema20) or ema20)
        ema200 = float(row.get("ema_200", ema50) or ema50)
        if direction == "BUY":
            return close >= ema20 and ema20 >= ema50 and (ema50 >= ema200 if slow else True)
        return close <= ema20 and ema20 <= ema50 and (ema50 <= ema200 if slow else True)

    @staticmethod
    def _price_side(direction: str, row: pd.Series, column: str) -> bool:
        close = float(row.get("close", 0.0) or 0.0)
        level = float(row.get(column, close) or close)
        return close >= level if direction == "BUY" else close <= level

    @staticmethod
    def _momentum_candle(direction: str, row: pd.Series) -> bool:
        open_price, close = float(row.get("open", 0.0)), float(row.get("close", 0.0))
        return close > open_price if direction == "BUY" else close < open_price

    @staticmethod
    def _rejection_candle(direction: str, row: pd.Series) -> bool:
        open_price, close = float(row.get("open", 0.0)), float(row.get("close", 0.0))
        high, low = float(row.get("high", 0.0)), float(row.get("low", 0.0))
        body = max(abs(close - open_price), 1e-12)
        lower_wick, upper_wick = min(open_price, close) - low, high - max(open_price, close)
        return lower_wick >= body * 1.2 if direction == "BUY" else upper_wick >= body * 1.2

    def _fibonacci_near(self, frame: pd.DataFrame, close: float, atr: float) -> str | None:
        swing = detect_last_significant_swing(frame, atr_column="atr_14")
        if not swing:
            return None
        match = nearest_fibonacci_level(close, calculate_fibonacci_levels(swing), max(atr * 0.3, 1e-12))
        return str(match.get("level")) if match else None

    @staticmethod
    def _structure_id(symbol: str, direction: str, m5: pd.DataFrame, h1: pd.DataFrame) -> str:
        payload = "|".join(
            [
                symbol,
                direction,
                f"{float(m5.iloc[-20:].high.max()):.8f}",
                f"{float(m5.iloc[-20:].low.min()):.8f}",
                f"{float(h1.iloc[-1].get('ema_50', 0.0)):.8f}",
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _indicator_snapshot(m1: pd.Series, m5: pd.Series, m15: pd.Series, h1: pd.Series) -> dict[str, Any]:
        def value(row: pd.Series, key: str) -> float | None:
            item = row.get(key)
            return None if item is None or pd.isna(item) else round(float(item), 8)
        return {
            "m1": {key: value(m1, key) for key in ("close", "ema_9", "ema_20", "ema_50", "rsi_14", "atr_14", "macd_hist", "adx_14", "vwap", "bb_width")},
            "m5": {key: value(m5, key) for key in ("close", "ema_9", "ema_20", "ema_50", "rsi_14", "adx_14", "vwap")},
            "m15": {key: value(m15, key) for key in ("close", "ema_20", "ema_50", "ema_200", "rsi_14")},
            "h1": {key: value(h1, key) for key in ("close", "ema_20", "ema_50", "ema_200", "rsi_14")},
        }

    def _empty_rejection(self, symbol: str, broker: str, session: str, profile: SymbolProfile, code: str, reason: str) -> RoutedSignal:
        return RoutedSignal(
            symbol=symbol,
            broker_symbol=broker,
            session=session,
            profile=profile.profile,
            strategy="NONE",
            direction="NONE",
            raw_score=0.0,
            bonuses=[],
            penalties=[],
            final_score=0.0,
            required_score=profile.minimum_score,
            accepted=False,
            rejection_code=code,
            reasons=[reason],
            indicators={},
            entry_price=0.0,
            stop_loss=0.0,
            tp1=0.0,
            tp2=0.0,
            source_candle="",
            structure_id="",
            created_at_utc=datetime.now(timezone.utc).isoformat(),
        )

    @staticmethod
    def _time_value(value: Any) -> str:
        if isinstance(value, pd.Timestamp):
            return value.tz_convert("UTC").isoformat() if value.tzinfo else value.tz_localize("UTC").isoformat()
        parsed = pd.Timestamp(value)
        return parsed.tz_convert("UTC").isoformat() if parsed.tzinfo else parsed.tz_localize("UTC").isoformat()

    @staticmethod
    def _parse_time(value: str) -> time:
        hour, minute = value.split(":", 1)
        return time(int(hour), int(minute))

    @staticmethod
    def _opposite(direction: str) -> str:
        return "SELL" if direction == "BUY" else "BUY"

    @staticmethod
    def _num(value: Any, default: float = 0.0) -> float:
        try:
            return default if value is None or pd.isna(value) else float(value)
        except (TypeError, ValueError):
            return default

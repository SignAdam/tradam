"""M1 candle-driven signal scanner, separate from position management."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from src.strategy.setup_manager import SetupIdentityManager, TradingSetup
from src.strategy.strategy_router import RoutedSignal, StrategyRouter


class SignalScanner:
    def __init__(
        self,
        market_data: Any,
        router: StrategyRouter,
        setup_manager: SetupIdentityManager,
        strategy_config: dict[str, Any],
        database: Any,
    ) -> None:
        self.market_data = market_data
        self.router = router
        self.setup_manager = setup_manager
        self.config = strategy_config
        self.database = database
        self.last_candle: dict[str, str] = {}
        self.prepared_until: dict[str, datetime] = {}

    def scan(
        self,
        logical_symbol: str,
        broker_symbol: str,
        session: str,
        news_context: dict[str, Any],
        run_id: str,
        session_id: str,
        spread_points: float,
    ) -> tuple[RoutedSignal | None, TradingSetup | None]:
        preview = self.market_data.get_rates(broker_symbol, "M1", 3)
        candle_time = self._last_closed_candle_time(preview)
        now = datetime.now(timezone.utc)
        prepared = self.prepared_until.get(logical_symbol)
        is_new = self.last_candle.get(logical_symbol) != candle_time
        if not is_new and not (prepared and now <= prepared):
            self._diagnostic("NO_NEW_CANDLES", run_id, session_id, logical_symbol, {"last_candle": candle_time})
            return None, None
        self.last_candle[logical_symbol] = candle_time
        self._diagnostic("M1_CANDLE_ANALYZED", run_id, session_id, logical_symbol, {"candle": candle_time})
        frames = self.market_data.get_multi_timeframe_rates(
            broker_symbol,
            ["M1", "M5", "M15", "H1", "H4"],
            self.config.get("history_bars", {}),
        )
        signal = self.router.route(
            logical_symbol,
            broker_symbol,
            session,
            frames,
            news_context,
            {"spread_points": spread_points},
        )
        self._diagnostic("RAW_SETUP_DETECTED", run_id, session_id, logical_symbol, signal.to_dict())
        if not signal.accepted:
            self._diagnostic(signal.rejection_code or "NO_VALID_SETUP", run_id, session_id, logical_symbol, signal.to_dict())
            if signal.final_score >= signal.required_score - 1.0 and signal.strategy in {
                "BREAKOUT_RETEST", "OPENING_RANGE_BREAKOUT", "VWAP_RECLAIM"
            }:
                self.prepared_until[logical_symbol] = now + timedelta(
                    minutes=int(self.config.get("max_prepared_setup_age_minutes", 5))
                )
            return signal, None
        self.prepared_until.pop(logical_symbol, None)
        setup = self.setup_manager.create(
            symbol=logical_symbol,
            strategy=signal.strategy,
            direction=signal.direction,
            session=session,
            source_candle=signal.source_candle,
            structure_id=signal.structure_id,
            expiry_minutes=int(self.config.get("setup_identity", {}).get("default_expiry_minutes", 8)),
        )
        if not self.setup_manager.register(setup, run_id, session_id):
            signal.accepted = False
            signal.rejection_code = "SETUP_ALREADY_PROCESSED"
            signal.reasons.append("The same candle, strategy, direction, and structure were already processed")
            self._diagnostic("SETUP_ALREADY_PROCESSED", run_id, session_id, logical_symbol, signal.to_dict())
            return signal, setup
        allowed, reason = self.setup_manager.can_execute(setup)
        if not allowed:
            signal.accepted = False
            signal.rejection_code = reason
            signal.reasons.append(reason or "Setup execution blocked")
            self._diagnostic(reason or "COOLDOWN_ACTIVE", run_id, session_id, logical_symbol, signal.to_dict())
        else:
            self._diagnostic("VALID_SETUP_DETECTED", run_id, session_id, logical_symbol, signal.to_dict())
        return signal, setup

    @staticmethod
    def _last_closed_candle_time(frame: pd.DataFrame) -> str:
        if frame.empty or "time" not in frame.columns:
            return ""
        index = -2 if len(frame) >= 2 else -1
        value = pd.Timestamp(frame.iloc[index]["time"])
        if value.tzinfo is None:
            value = value.tz_localize("UTC")
        else:
            value = value.tz_convert("UTC")
        return value.isoformat()

    def _diagnostic(
        self,
        code: str,
        run_id: str,
        session_id: str,
        symbol: str,
        details: dict[str, Any],
    ) -> None:
        self.database.insert_diagnostic_event(code, run_id, session_id, symbol, details)

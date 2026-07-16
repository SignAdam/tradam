"""Simple historical backtester for the first strategy iteration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from src.analytics.metrics import compute_trade_metrics, group_performance
from src.strategy.risk_manager import RiskManager, RiskState, SymbolTradingSpec
from src.strategy.signal_engine import SignalEngine


@dataclass
class BacktestResult:
    trades: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    by_symbol: dict[str, Any] = field(default_factory=dict)
    by_session: dict[str, Any] = field(default_factory=dict)
    by_signal_type: dict[str, Any] = field(default_factory=dict)


class SimpleBacktester:
    def __init__(
        self,
        strategy_config: dict[str, Any],
        risk_config: dict[str, Any],
        symbols_config: dict[str, Any],
        initial_equity: float = 10_000.0,
    ) -> None:
        self.strategy_config = strategy_config
        self.risk_manager = RiskManager(risk_config)
        self.signal_engine = SignalEngine(strategy_config, symbols_config)
        self.initial_equity = initial_equity

    def run(
        self,
        frames_by_symbol: dict[str, dict[str, pd.DataFrame]],
        session_name: str = "BACKTEST",
        max_hold_bars: int = 20,
    ) -> BacktestResult:
        equity = self.initial_equity
        trades: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        entry_tf = self.strategy_config.get("entry_timeframe", "M5")

        for symbol, frames in frames_by_symbol.items():
            entry_frame = frames.get(entry_tf)
            if entry_frame is None or len(entry_frame) < 260:
                continue
            for index in range(240, len(entry_frame) - max_hold_bars):
                current_time = entry_frame.iloc[index].get("time")
                sliced = self._slice_frames(frames, current_time, index)
                decision = self.signal_engine.evaluate(
                    logical_symbol=symbol,
                    broker_symbol=symbol,
                    session=session_name,
                    frames=sliced,
                    news_context={"blocked": False, "sentiment": "neutral", "score": 0.0},
                    market_context={"spread_points": 0},
                )
                decisions.append(decision.to_dict())
                if decision.decision != "ACCEPTED":
                    continue

                entry_price = float(entry_frame.iloc[index]["close"])
                stop_loss = float(decision.risk["stop_loss_price"])
                take_profit = float(decision.risk["take_profit_price"])
                spec = SymbolTradingSpec()
                lot = self.risk_manager.calculate_position_size(equity, entry_price, stop_loss, spec)
                risk_check = self.risk_manager.validate_trade(
                    RiskState(equity=equity),
                    decision.direction or "BUY",
                    lot,
                    entry_price,
                    stop_loss,
                    take_profit,
                    spec,
                )
                if not risk_check.ok:
                    continue

                outcome = self._simulate_exit(
                    entry_frame.iloc[index + 1 : index + 1 + max_hold_bars],
                    decision.direction or "BUY",
                    entry_price,
                    stop_loss,
                    take_profit,
                    lot,
                    spec,
                )
                equity += outcome["pnl"]
                trades.append(
                    {
                        "symbol": symbol,
                        "session": session_name,
                        "entry_time": str(current_time or datetime.utcnow().isoformat()),
                        "exit_time": outcome["exit_time"],
                        "direction": decision.direction,
                        "lot": lot,
                        "entry_price": entry_price,
                        "stop_loss": stop_loss,
                        "take_profit": take_profit,
                        "exit_price": outcome["exit_price"],
                        "pnl": outcome["pnl"],
                        "duration_seconds": outcome["bars_held"] * 300,
                        "spread": 0,
                        "timeframe": entry_tf,
                        "signal_reason": "; ".join(decision.reasons[:3]),
                        "status": "CLOSED",
                    }
                )

        metrics = compute_trade_metrics(trades)
        return BacktestResult(
            trades=trades,
            decisions=decisions,
            metrics=metrics,
            by_symbol=group_performance(trades, "symbol"),
            by_session=group_performance(trades, "session"),
            by_signal_type=group_performance(trades, "signal_reason"),
        )

    @staticmethod
    def _slice_frames(
        frames: dict[str, pd.DataFrame], current_time: Any, entry_index: int
    ) -> dict[str, pd.DataFrame]:
        sliced: dict[str, pd.DataFrame] = {}
        for timeframe, frame in frames.items():
            if "time" in frame.columns and current_time is not None:
                sliced_frame = frame[frame["time"] <= current_time].copy()
            else:
                sliced_frame = frame.iloc[: entry_index + 1].copy()
            if len(sliced_frame) > 260:
                sliced_frame = sliced_frame.tail(260)
            sliced[timeframe] = sliced_frame
        return sliced

    @staticmethod
    def _simulate_exit(
        future: pd.DataFrame,
        direction: str,
        entry: float,
        stop_loss: float,
        take_profit: float,
        lot: float,
        spec: SymbolTradingSpec,
    ) -> dict[str, Any]:
        exit_price = float(future.iloc[-1]["close"])
        bars_held = len(future)
        for offset, (_, bar) in enumerate(future.iterrows(), start=1):
            high = float(bar["high"])
            low = float(bar["low"])
            if direction == "BUY":
                if low <= stop_loss:
                    exit_price = stop_loss
                    bars_held = offset
                    break
                if high >= take_profit:
                    exit_price = take_profit
                    bars_held = offset
                    break
            else:
                if high >= stop_loss:
                    exit_price = stop_loss
                    bars_held = offset
                    break
                if low <= take_profit:
                    exit_price = take_profit
                    bars_held = offset
                    break
        price_delta = exit_price - entry if direction == "BUY" else entry - exit_price
        pnl = (price_delta / spec.tick_size) * spec.tick_value * lot
        exit_time = str(future.iloc[min(bars_held - 1, len(future) - 1)].get("time", datetime.utcnow().isoformat()))
        return {"exit_price": exit_price, "pnl": round(pnl, 2), "bars_held": bars_held, "exit_time": exit_time}


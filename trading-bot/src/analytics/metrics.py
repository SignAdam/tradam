"""Trading performance metrics used by session reports and backtests."""

from __future__ import annotations

from collections import defaultdict
from math import isfinite
from typing import Any


def _pnl(trade: dict[str, Any]) -> float:
    value = trade.get("pnl")
    return float(value) if value is not None else 0.0


def max_drawdown_from_pnls(pnls: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = min(max_drawdown, equity - peak)
    return abs(max_drawdown)


def compute_trade_metrics(trades: list[dict[str, Any]]) -> dict[str, Any]:
    closed = [trade for trade in trades if trade.get("pnl") is not None]
    pnls = [_pnl(trade) for trade in closed]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    profit_factor = (
        float("inf")
        if gross_loss == 0 and gross_profit > 0
        else gross_profit / abs(gross_loss)
        if gross_loss
        else 0.0
    )
    total = len(closed)
    winrate = len(wins) / total if total else 0.0
    avg_win = gross_profit / len(wins) if wins else 0.0
    avg_loss = gross_loss / len(losses) if losses else 0.0
    expectancy = (winrate * avg_win) + ((1 - winrate) * avg_loss) if total else 0.0
    return {
        "trades": total,
        "open_trades": len(trades) - total,
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(winrate, 4),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(sum(pnls), 2),
        "profit_factor": "inf" if not isfinite(profit_factor) else round(profit_factor, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "max_drawdown": round(max_drawdown_from_pnls(pnls), 2),
        "average_risk_reward": average_risk_reward(closed),
    }


def average_risk_reward(trades: list[dict[str, Any]]) -> float:
    values: list[float] = []
    for trade in trades:
        entry = trade.get("entry_price")
        sl = trade.get("stop_loss")
        tp = trade.get("take_profit")
        if entry is None or sl is None or tp is None:
            continue
        risk = abs(float(entry) - float(sl))
        reward = abs(float(tp) - float(entry))
        if risk > 0:
            values.append(reward / risk)
    return round(sum(values) / len(values), 3) if values else 0.0


def group_performance(trades: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[str(trade.get(key, "unknown"))].append(trade)
    return {group: compute_trade_metrics(rows) for group, rows in grouped.items()}


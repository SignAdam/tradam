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
    unique = unique_trades(trades)
    closed = [trade for trade in unique if trade.get("pnl") is not None or trade.get("pnl_net") is not None]
    pnls = [float(trade.get("pnl_net") if trade.get("pnl_net") is not None else _pnl(trade)) for trade in closed]
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
    r_values = [float(trade.get("realized_r")) for trade in closed if trade.get("realized_r") is not None]
    mfe_values = [float(trade.get("mfe_r")) for trade in unique if trade.get("mfe_r") is not None]
    mae_values = [float(trade.get("mae_r")) for trade in unique if trade.get("mae_r") is not None]
    duplicate_rate = 0.0 if not trades else (len(trades) - len(unique)) / len(trades)
    reached_025 = _ratio(unique, lambda trade: float(trade.get("mfe_r") or 0.0) >= 0.25)
    reached_05 = _ratio(unique, lambda trade: float(trade.get("mfe_r") or 0.0) >= 0.5)
    reached_tp1 = _ratio(unique, lambda trade: trade.get("tp1_actual_price") is not None)
    reached_tp2 = _ratio(unique, lambda trade: trade.get("tp2_actual_price") is not None)
    return {
        "trades": total,
        "unique_trades": len(unique),
        "open_trades": len(unique) - total,
        "duplicate_rows_detected": len(trades) - len(unique),
        "duplicate_rate": round(duplicate_rate, 4),
        "unique_positions": len({str(t.get("mt5_position_id")) for t in unique if t.get("mt5_position_id")}),
        "unique_deals": len({str(t.get("mt5_deal_ticket")) for t in unique if t.get("mt5_deal_ticket")}),
        "wins": len(wins),
        "losses": len(losses),
        "winrate": round(winrate, 4),
        "winrate_net": round(winrate, 4),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "net_pnl": round(sum(pnls), 2),
        "profit_factor": "inf" if not isfinite(profit_factor) else round(profit_factor, 4),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "expectancy": round(expectancy, 2),
        "expectancy_r": round(sum(r_values) / len(r_values), 4) if r_values else 0.0,
        "payoff_ratio": round(abs(avg_win / avg_loss), 4) if avg_loss else 0.0,
        "max_drawdown": round(max_drawdown_from_pnls(pnls), 2),
        "average_risk_reward": average_risk_reward(closed),
        "average_mfe_r": round(sum(mfe_values) / len(mfe_values), 4) if mfe_values else 0.0,
        "average_mae_r": round(sum(mae_values) / len(mae_values), 4) if mae_values else 0.0,
        "reached_0_25r_percent": reached_025,
        "reached_0_5r_percent": reached_05,
        "reached_tp1_percent": reached_tp1,
        "reached_tp2_percent": reached_tp2,
        "exit_efficiency": exit_efficiency(unique),
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


def unique_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for trade in trades:
        key = (
            trade.get("mt5_deal_ticket")
            or trade.get("mt5_position_id")
            or trade.get("internal_trade_id")
            or trade.get("trade_id")
            or f"{trade.get('symbol')}|{trade.get('entry_time')}|{trade.get('direction')}|{trade.get('entry_price')}"
        )
        key = str(key)
        if key in seen:
            continue
        seen.add(key)
        result.append(trade)
    return result


def _ratio(trades: list[dict[str, Any]], predicate: Any) -> float:
    if not trades:
        return 0.0
    return round(len([trade for trade in trades if predicate(trade)]) / len(trades), 4)


def exit_efficiency(trades: list[dict[str, Any]]) -> float:
    values: list[float] = []
    for trade in trades:
        mfe = trade.get("mfe_amount")
        pnl = trade.get("pnl_net", trade.get("pnl"))
        if mfe is None or pnl is None:
            continue
        mfe_value = float(mfe)
        if mfe_value <= 0:
            continue
        values.append(max(float(pnl), 0.0) / mfe_value)
    return round(sum(values) / len(values), 4) if values else 0.0

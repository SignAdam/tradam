"""Generate HTML, CSV, and JSON analytics reports after a trading session."""

from __future__ import annotations

import csv
import json
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

from src.analytics.charts import equity_curve_svg
from src.analytics.metrics import compute_trade_metrics, group_performance, unique_trades
from src.storage.database import Database
from src.utils.security import redact_sensitive_data


TRADE_EXPORT_FIELDS = [
    "run_id",
    "session_id",
    "mode",
    "internal_trade_id",
    "mt5_position_id",
    "mt5_order_ticket",
    "mt5_deal_ticket",
    "symbol",
    "session",
    "signal_time",
    "order_time",
    "execution_time",
    "entry_time",
    "exit_time",
    "direction",
    "lot",
    "initial_volume",
    "remaining_volume",
    "requested_price",
    "entry_price",
    "actual_entry_price",
    "entry_slippage",
    "stop_loss",
    "initial_stop_loss",
    "final_stop_loss",
    "take_profit",
    "tp1",
    "tp1_close_percent",
    "tp1_actual_price",
    "tp1_pnl",
    "tp2",
    "tp2_actual_price",
    "tp2_pnl",
    "exit_price",
    "pnl",
    "pnl_gross",
    "pnl_net",
    "commission",
    "swap",
    "realized_r",
    "duration_seconds",
    "spread",
    "spread_price",
    "spread_points",
    "estimated_spread_cost",
    "initial_risk_price",
    "initial_risk_amount",
    "initial_risk_percent",
    "sl_modification_count",
    "break_even_applied",
    "break_even_time",
    "break_even_price",
    "trailing_stop_enabled",
    "max_favorable_price",
    "max_adverse_price",
    "mfe_price",
    "mfe_amount",
    "mfe_r",
    "mae_price",
    "mae_amount",
    "mae_r",
    "max_unrealized_profit",
    "max_unrealized_loss",
    "exit_reason",
    "timeframe",
    "h1_trend",
    "rsi",
    "ema20",
    "ema50",
    "ema200",
    "atr",
    "macd",
    "fibonacci_level",
    "signal_reason",
    "sentiment",
    "status",
]


def _safe_json_loads(value: Any, default: Any) -> Any:
    if value in {None, ""}:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


class SessionReportGenerator:
    def __init__(self, database: Database, reports_dir: str | Path) -> None:
        self.database = database
        self.reports_dir = Path(reports_dir)
        self.reports_dir.mkdir(parents=True, exist_ok=True)

    def generate(
        self,
        session: str,
        started_at: str,
        ended_at: str,
        mode: str,
        symbols: list[str],
        config_snapshot: dict[str, Any],
        run_id: str | None = None,
        session_id: str | None = None,
        include_fixtures: bool = False,
    ) -> dict[str, str]:
        filters = {
            "mode": mode,
            "run_id": run_id,
            "session_id": session_id,
            "include_fixtures": include_fixtures,
        }
        trades = unique_trades(self.database.fetch_trades_between(started_at, ended_at, **filters))
        decisions = self.database.fetch_decisions_between(started_at, ended_at, **filters)
        news = self.database.fetch_news_between(started_at, ended_at, **filters)
        events = self.database.fetch_position_events_between(started_at, ended_at, **filters)
        news_health = self.database.fetch_news_provider_status_between(started_at, ended_at, **filters)
        metrics = compute_trade_metrics(trades)
        summary = {
            "date": started_at[:10],
            "session": session,
            "run_id": run_id,
            "session_id": session_id,
            "mode": mode,
            "started_at": started_at,
            "ended_at": ended_at,
            "symbols_analyzed": symbols,
            "metrics": metrics,
            "performance_by_symbol": group_performance(trades, "symbol"),
            "performance_by_session": group_performance(trades, "session"),
            "performance_by_signal_type": group_performance(trades, "signal_reason"),
            "trades_taken": len([d for d in decisions if d.get("decision") == "ACCEPTED"]),
            "trades_refused": len([d for d in decisions if d.get("decision") != "ACCEPTED"]),
            "refusal_reasons": self._refusal_reasons(decisions),
            "weak_points": self._weak_points(metrics, decisions),
            "improvement_suggestions": self._suggestions(metrics, decisions),
            "trades": redact_sensitive_data(trades),
            "decisions": [self._decode_decision(decision) for decision in decisions],
            "news": redact_sensitive_data(news),
            "position_events": redact_sensitive_data(events),
            "news_provider_status": redact_sensitive_data(news_health),
            "configuration": redact_sensitive_data(config_snapshot),
        }

        stamp = datetime.fromisoformat(started_at).strftime("%Y%m%d_%H%M%S")
        base = self.reports_dir / f"{stamp}_{session}"
        csv_path = base.with_suffix(".csv")
        json_path = base.with_suffix(".json")
        html_path = base.with_suffix(".html")

        self._write_csv(csv_path, trades)
        json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=True, default=str), "utf-8")
        html_path.write_text(self._render_html(summary), "utf-8")

        paths = {"csv": str(csv_path), "json": str(json_path), "html": str(html_path)}
        self.database.insert_session_report(
            {
                "started_at": started_at,
                "ended_at": ended_at,
                "run_id": run_id,
                "session_id": session_id,
                "session": session,
                "mode": mode,
                "is_fixture": include_fixtures,
                "symbols": symbols,
                "metrics": metrics,
                "config": config_snapshot,
                "report_paths": paths,
            }
        )
        return paths

    @staticmethod
    def _write_csv(path: Path, trades: list[dict[str, Any]]) -> None:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRADE_EXPORT_FIELDS)
            writer.writeheader()
            for trade in trades:
                writer.writerow({field: trade.get(field) for field in TRADE_EXPORT_FIELDS})

    @staticmethod
    def _decode_decision(decision: dict[str, Any]) -> dict[str, Any]:
        decoded = dict(decision)
        decoded["reasons"] = _safe_json_loads(decision.get("reasons_json"), [])
        decoded["risk"] = _safe_json_loads(decision.get("risk_json"), {})
        decoded["indicators"] = _safe_json_loads(decision.get("indicators_json"), {})
        decoded["news"] = _safe_json_loads(decision.get("news_json"), {})
        return decoded

    @staticmethod
    def _refusal_reasons(decisions: list[dict[str, Any]]) -> dict[str, int]:
        reasons: dict[str, int] = {}
        for decision in decisions:
            if decision.get("decision") == "ACCEPTED":
                continue
            reason = decision.get("rejected_reason") or "unspecified"
            reasons[reason] = reasons.get(reason, 0) + 1
        return reasons

    @staticmethod
    def _weak_points(metrics: dict[str, Any], decisions: list[dict[str, Any]]) -> list[str]:
        points: list[str] = []
        if metrics["trades"] == 0:
            points.append("No closed trades during the session.")
        if metrics["trades"] and metrics["winrate"] < 0.45:
            points.append("Winrate below 45%; review signal quality and stop placement.")
        if metrics["max_drawdown"] > 0 and metrics["net_pnl"] <= 0:
            points.append("Drawdown occurred without positive net PnL.")
        if len([d for d in decisions if d.get("decision") != "ACCEPTED"]) > len(decisions) * 0.8:
            points.append("Most candidate trades were refused; check filters and market regime.")
        return points or ["No major weakness detected from this session sample."]

    @staticmethod
    def _suggestions(metrics: dict[str, Any], decisions: list[dict[str, Any]]) -> list[str]:
        suggestions = [
            "Compare accepted and refused signals around Fibonacci confluence zones.",
            "Review spread and news filters before changing score thresholds.",
        ]
        if metrics["trades"] >= 5 and metrics["profit_factor"] not in {"inf", 0.0}:
            suggestions.append("Segment results by hour to identify weak liquidity windows.")
        if any((d.get("rejected_reason") or "").lower().find("adx") >= 0 for d in decisions):
            suggestions.append("Inspect range conditions; ADX may be filtering a sideways market.")
        return suggestions

    @staticmethod
    def _render_html(summary: dict[str, Any]) -> str:
        metrics_rows = "\n".join(
            f"<tr><th>{escape(str(key))}</th><td>{escape(str(value))}</td></tr>"
            for key, value in summary["metrics"].items()
        )
        trade_rows = "\n".join(
            "<tr>"
            + "".join(f"<td>{escape(str(trade.get(field, '')))}</td>" for field in TRADE_EXPORT_FIELDS)
            + "</tr>"
            for trade in summary["trades"]
        )
        decision_rows = "\n".join(
            "<tr>"
            f"<td>{escape(str(decision.get('created_at')))}</td>"
            f"<td>{escape(str(decision.get('symbol')))}</td>"
            f"<td>{escape(str(decision.get('direction')))}</td>"
            f"<td>{escape(str(decision.get('score')))}</td>"
            f"<td>{escape(str(decision.get('decision')))}</td>"
            f"<td>{escape('; '.join(decision.get('reasons', [])))}</td>"
            f"<td>{escape(str(decision.get('rejected_reason') or ''))}</td>"
            "</tr>"
            for decision in summary["decisions"]
        )
        news_rows = "\n".join(
            "<tr>"
            f"<td>{escape(str(item.get('published_at')))}</td>"
            f"<td>{escape(str(item.get('symbol_group')))}</td>"
            f"<td>{escape(str(item.get('impact')))}</td>"
            f"<td>{escape(str(item.get('sentiment')))}</td>"
            f"<td>{escape(str(item.get('title')))}</td>"
            "</tr>"
            for item in summary["news"]
        )
        event_rows = "\n".join(
            "<tr>"
            f"<td>{escape(str(item.get('timestamp_utc')))}</td>"
            f"<td>{escape(str(item.get('event_type')))}</td>"
            f"<td>{escape(str(item.get('mt5_position_id') or item.get('internal_trade_id') or ''))}</td>"
            f"<td>{escape(str(item.get('bid') or ''))}</td>"
            f"<td>{escape(str(item.get('ask') or ''))}</td>"
            f"<td>{escape(str(item.get('current_r') or ''))}</td>"
            f"<td>{escape(str(item.get('new_stop_loss') or ''))}</td>"
            f"<td>{escape(str(item.get('mt5_retcode') or ''))}</td>"
            f"<td>{escape(str(item.get('error_message') or ''))}</td>"
            "</tr>"
            for item in summary["position_events"]
        )
        news_health_rows = "\n".join(
            "<tr>"
            f"<td>{escape(str(item.get('provider')))}</td>"
            f"<td>{escape(str(item.get('status')))}</td>"
            f"<td>{escape(str(item.get('article_count')))}</td>"
            f"<td>{escape(str(item.get('event_count')))}</td>"
            f"<td>{escape(str(item.get('last_success_utc') or ''))}</td>"
            f"<td>{escape(str(item.get('error') or ''))}</td>"
            "</tr>"
            for item in summary["news_provider_status"]
        )
        pnl_values = [float(trade.get("pnl") or 0) for trade in summary["trades"] if trade.get("pnl") is not None]
        chart = equity_curve_svg(pnl_values)
        weak_points = "".join(f"<li>{escape(item)}</li>" for item in summary["weak_points"])
        suggestions = "".join(f"<li>{escape(item)}</li>" for item in summary["improvement_suggestions"])

        return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{escape(summary['session'])} session report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 28px; color: #111827; background: #ffffff; }}
    h1, h2 {{ margin-bottom: 8px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 16px 0 28px; font-size: 14px; }}
    th, td {{ border: 1px solid #d1d5db; padding: 8px; text-align: left; vertical-align: top; }}
    th {{ background: #f3f4f6; }}
    .meta {{ color: #4b5563; }}
    .grid {{ display: grid; grid-template-columns: minmax(260px, 420px) 1fr; gap: 28px; align-items: start; }}
  </style>
</head>
<body>
  <h1>{escape(summary['session'])} Session Report</h1>
  <p class="meta">{escape(summary['started_at'])} to {escape(summary['ended_at'])} | mode: {escape(summary['mode'])} | run: {escape(str(summary.get('run_id') or ''))} | session_id: {escape(str(summary.get('session_id') or ''))}</p>
  <div class="grid">
    <section>
      <h2>Metrics</h2>
      <table>{metrics_rows}</table>
    </section>
    <section>
      <h2>Equity Curve</h2>
      {chart}
    </section>
  </div>
  <h2>Weak Points</h2>
  <ul>{weak_points}</ul>
  <h2>Suggestions</h2>
  <ul>{suggestions}</ul>
  <h2>Trades</h2>
  <table><thead><tr>{''.join(f'<th>{escape(field)}</th>' for field in TRADE_EXPORT_FIELDS)}</tr></thead><tbody>{trade_rows}</tbody></table>
  <h2>Decisions</h2>
  <table><thead><tr><th>time</th><th>symbol</th><th>direction</th><th>score</th><th>decision</th><th>reasons</th><th>rejected reason</th></tr></thead><tbody>{decision_rows}</tbody></table>
  <h2>News Used</h2>
  <table><thead><tr><th>published</th><th>symbol</th><th>impact</th><th>sentiment</th><th>title</th></tr></thead><tbody>{news_rows}</tbody></table>
  <h2>News Provider Health</h2>
  <table><thead><tr><th>provider</th><th>status</th><th>articles</th><th>events</th><th>last success</th><th>error</th></tr></thead><tbody>{news_health_rows}</tbody></table>
  <h2>Position Events</h2>
  <table><thead><tr><th>time UTC</th><th>event</th><th>position</th><th>bid</th><th>ask</th><th>R</th><th>new SL</th><th>retcode</th><th>error</th></tr></thead><tbody>{event_rows}</tbody></table>
</body>
</html>
"""

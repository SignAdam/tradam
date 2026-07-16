"""SQLite persistence for trades, decisions, news, and generated reports."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from src.storage.models import NewsRecord, SignalDecisionRecord, TradeRecord


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, default=str)


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()

    def initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT,
                symbol TEXT NOT NULL,
                session TEXT,
                entry_time TEXT NOT NULL,
                exit_time TEXT,
                direction TEXT NOT NULL,
                lot REAL NOT NULL,
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                exit_price REAL,
                pnl REAL,
                duration_seconds INTEGER,
                spread REAL,
                timeframe TEXT,
                h1_trend TEXT,
                rsi REAL,
                ema20 REAL,
                ema50 REAL,
                ema200 REAL,
                atr REAL,
                macd REAL,
                fibonacci_level TEXT,
                signal_reason TEXT,
                news_active_json TEXT,
                sentiment TEXT,
                status TEXT NOT NULL,
                metadata_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                session TEXT,
                direction TEXT,
                score REAL NOT NULL,
                decision TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                risk_json TEXT,
                indicators_json TEXT,
                news_json TEXT,
                rejected_reason TEXT,
                raw_json TEXT
            );

            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                published_at TEXT NOT NULL,
                source TEXT,
                symbol_group TEXT,
                title TEXT NOT NULL,
                url TEXT,
                impact TEXT,
                sentiment TEXT,
                score REAL,
                raw_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS session_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                session TEXT,
                mode TEXT,
                symbols_json TEXT,
                metrics_json TEXT,
                config_json TEXT,
                report_paths_json TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
            CREATE INDEX IF NOT EXISTS idx_decisions_created_at ON decisions(created_at);
            CREATE INDEX IF NOT EXISTS idx_news_published_at ON news(published_at);
            """
        )
        self.connection.commit()

    def insert_trade(self, trade: TradeRecord | dict[str, Any]) -> int:
        data = trade.to_dict() if isinstance(trade, TradeRecord) else dict(trade)
        row = {
            "trade_id": data.get("trade_id"),
            "symbol": data["symbol"],
            "session": data.get("session"),
            "entry_time": data["entry_time"],
            "exit_time": data.get("exit_time"),
            "direction": data["direction"],
            "lot": data["lot"],
            "entry_price": data["entry_price"],
            "stop_loss": data["stop_loss"],
            "take_profit": data["take_profit"],
            "exit_price": data.get("exit_price"),
            "pnl": data.get("pnl"),
            "duration_seconds": data.get("duration_seconds"),
            "spread": data.get("spread"),
            "timeframe": data.get("timeframe"),
            "h1_trend": data.get("h1_trend"),
            "rsi": data.get("rsi"),
            "ema20": data.get("ema20"),
            "ema50": data.get("ema50"),
            "ema200": data.get("ema200"),
            "atr": data.get("atr"),
            "macd": data.get("macd"),
            "fibonacci_level": data.get("fibonacci_level"),
            "signal_reason": data.get("signal_reason"),
            "news_active_json": _json(data.get("news_active", [])),
            "sentiment": data.get("sentiment"),
            "status": data.get("status", "OPEN"),
            "metadata_json": _json(data.get("metadata", {})),
        }
        return self._insert("trades", row)

    def insert_decision(self, decision: SignalDecisionRecord | dict[str, Any]) -> int:
        data = decision.to_dict() if isinstance(decision, SignalDecisionRecord) else dict(decision)
        row = {
            "created_at": data["created_at"],
            "symbol": data["symbol"],
            "session": data.get("session"),
            "direction": data.get("direction"),
            "score": data["score"],
            "decision": data["decision"],
            "reasons_json": _json(data.get("reasons", [])),
            "risk_json": _json(data.get("risk", {})),
            "indicators_json": _json(data.get("indicators", {})),
            "news_json": _json(data.get("news", {})),
            "rejected_reason": data.get("rejected_reason"),
            "raw_json": _json(data.get("raw", data)),
        }
        return self._insert("decisions", row)

    def insert_news(self, news: NewsRecord | dict[str, Any]) -> int:
        data = news.to_dict() if isinstance(news, NewsRecord) else dict(news)
        row = {
            "published_at": data["published_at"],
            "source": data.get("source"),
            "symbol_group": data.get("symbol_group"),
            "title": data["title"],
            "url": data.get("url"),
            "impact": data.get("impact", "low"),
            "sentiment": data.get("sentiment", "neutral"),
            "score": data.get("score", 0.0),
            "raw_json": _json(data.get("raw", data)),
        }
        return self._insert("news", row)

    def insert_session_report(self, row: dict[str, Any]) -> int:
        payload = {
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "session": row.get("session"),
            "mode": row.get("mode"),
            "symbols_json": _json(row.get("symbols", [])),
            "metrics_json": _json(row.get("metrics", {})),
            "config_json": _json(row.get("config", {})),
            "report_paths_json": _json(row.get("report_paths", {})),
        }
        return self._insert("session_reports", payload)

    def _insert(self, table: str, row: dict[str, Any]) -> int:
        columns = ", ".join(row.keys())
        placeholders = ", ".join(f":{key}" for key in row)
        cursor = self.connection.execute(
            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", row
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def fetch_between(self, table: str, time_column: str, start: str, end: str) -> list[dict[str, Any]]:
        cursor = self.connection.execute(
            f"SELECT * FROM {table} WHERE {time_column} BETWEEN ? AND ? ORDER BY {time_column}",
            (start, end),
        )
        return [dict(row) for row in cursor.fetchall()]

    def fetch_trades_between(self, start: str, end: str) -> list[dict[str, Any]]:
        return self.fetch_between("trades", "entry_time", start, end)

    def fetch_decisions_between(self, start: str, end: str) -> list[dict[str, Any]]:
        return self.fetch_between("decisions", "created_at", start, end)

    def fetch_news_between(self, start: str, end: str) -> list[dict[str, Any]]:
        return self.fetch_between("news", "published_at", start, end)

    def executemany_news(self, items: Iterable[NewsRecord | dict[str, Any]]) -> list[int]:
        return [self.insert_news(item) for item in items]


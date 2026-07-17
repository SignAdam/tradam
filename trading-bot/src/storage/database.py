"""SQLite persistence with additive migrations and idempotent inserts."""

from __future__ import annotations

import json
import sqlite3
import shutil
from pathlib import Path
from typing import Any, Iterable

from src.storage.models import NewsRecord, SignalDecisionRecord, TradeRecord
from src.utils.identity import new_id, utc_now_iso


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
            PRAGMA foreign_keys = ON;

            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL,
                source TEXT NOT NULL,
                started_at_utc TEXT NOT NULL,
                ended_at_utc TEXT,
                config_json TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trading_sessions (
                session_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                session TEXT,
                mode TEXT NOT NULL,
                source TEXT NOT NULL,
                started_at_utc TEXT NOT NULL,
                ended_at_utc TEXT,
                broker_timezone TEXT,
                broker_utc_offset_minutes INTEGER,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

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

            CREATE TABLE IF NOT EXISTS position_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                run_id TEXT,
                session_id TEXT,
                internal_trade_id TEXT,
                mt5_position_id TEXT,
                event_type TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                bid REAL,
                ask REAL,
                spread REAL,
                unrealized_profit REAL,
                current_r REAL,
                old_stop_loss REAL,
                new_stop_loss REAL,
                volume REAL,
                mt5_retcode INTEGER,
                error_message TEXT,
                attempts INTEGER DEFAULT 0,
                payload_json TEXT,
                is_fixture INTEGER DEFAULT 0,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mt5_deals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mt5_deal_ticket TEXT NOT NULL,
                mt5_order_ticket TEXT,
                mt5_position_id TEXT,
                run_id TEXT,
                session_id TEXT,
                mode TEXT NOT NULL,
                symbol TEXT,
                direction TEXT,
                volume REAL,
                price REAL,
                profit REAL,
                commission REAL,
                swap REAL,
                time_utc TEXT,
                raw_json TEXT,
                is_fixture INTEGER DEFAULT 0,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mt5_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mt5_order_ticket TEXT NOT NULL,
                mt5_position_id TEXT,
                run_id TEXT,
                session_id TEXT,
                mode TEXT NOT NULL,
                symbol TEXT,
                direction TEXT,
                volume_initial REAL,
                volume_current REAL,
                price_open REAL,
                sl REAL,
                tp REAL,
                time_setup_utc TEXT,
                raw_json TEXT,
                is_fixture INTEGER DEFAULT 0,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS news_provider_status (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                session_id TEXT,
                provider TEXT NOT NULL,
                last_request_utc TEXT,
                last_success_utc TEXT,
                article_count INTEGER DEFAULT 0,
                event_count INTEGER DEFAULT 0,
                status TEXT NOT NULL,
                error TEXT,
                freshness_seconds INTEGER,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS setups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                setup_id TEXT NOT NULL,
                run_id TEXT,
                session_id TEXT,
                symbol TEXT NOT NULL,
                strategy TEXT NOT NULL,
                direction TEXT NOT NULL,
                session TEXT,
                source_candle TEXT NOT NULL,
                structure_id TEXT NOT NULL,
                detected_at_utc TEXT NOT NULL,
                expires_at_utc TEXT NOT NULL,
                executed_at_utc TEXT,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS symbol_cooldowns (
                symbol TEXT PRIMARY KEY,
                structure_id TEXT NOT NULL,
                outcome TEXT NOT NULL,
                cooldown_until_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS diagnostic_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                run_id TEXT,
                session_id TEXT,
                symbol TEXT,
                code TEXT NOT NULL,
                timestamp_utc TEXT NOT NULL,
                details_json TEXT,
                created_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS managed_positions (
                mt5_position_id TEXT PRIMARY KEY,
                internal_trade_id TEXT NOT NULL,
                run_id TEXT,
                session_id TEXT,
                symbol TEXT NOT NULL,
                state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                closed INTEGER DEFAULT 0,
                created_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS heartbeats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                session_id TEXT,
                timestamp_utc TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_trades_entry_time ON trades(entry_time);
            CREATE INDEX IF NOT EXISTS idx_decisions_created_at ON decisions(created_at);
            CREATE INDEX IF NOT EXISTS idx_news_published_at ON news(published_at);
            """
        )
        self._migrate_v2_identity_columns()
        self._migrate_v3_demo_live_columns()
        self._create_indexes()
        self._create_demo_only_triggers()
        self.connection.commit()

    def backup(self, backup_dir: str | Path | None = None) -> Path:
        directory = Path(backup_dir) if backup_dir else self.path.parent / "backups"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{self.path.stem}_{utc_now_iso().replace(':', '').replace('+', 'Z')}.sqlite"
        shutil.copy2(self.path, target)
        return target

    def _migrate_v2_identity_columns(self) -> None:
        columns = {
            "trades": {
                "run_id": "TEXT",
                "session_id": "TEXT",
                "signal_id": "TEXT",
                "internal_trade_id": "TEXT",
                "mt5_position_id": "TEXT",
                "mt5_order_ticket": "TEXT",
                "mt5_deal_ticket": "TEXT",
                "parent_position_id": "TEXT",
                "mode": "TEXT DEFAULT 'demo_live'",
                "source": "TEXT DEFAULT 'bot'",
                "is_fixture": "INTEGER DEFAULT 0",
                "created_at_utc": "TEXT",
                "updated_at_utc": "TEXT",
                "pnl_gross": "REAL",
                "pnl_net": "REAL",
                "commission": "REAL DEFAULT 0",
                "swap": "REAL DEFAULT 0",
                "spread_price": "REAL",
                "spread_points": "REAL",
                "estimated_spread_cost": "REAL",
                "requested_price": "REAL",
                "actual_entry_price": "REAL",
                "entry_slippage": "REAL",
                "signal_time": "TEXT",
                "order_time": "TEXT",
                "execution_time": "TEXT",
                "initial_volume": "REAL",
                "remaining_volume": "REAL",
                "initial_stop_loss": "REAL",
                "final_stop_loss": "REAL",
                "initial_risk_price": "REAL",
                "initial_risk_amount": "REAL",
                "initial_risk_percent": "REAL",
                "tp1": "REAL",
                "tp1_close_percent": "REAL",
                "tp1_actual_price": "REAL",
                "tp1_pnl": "REAL",
                "tp2": "REAL",
                "tp2_actual_price": "REAL",
                "tp2_pnl": "REAL",
                "sl_modification_count": "INTEGER DEFAULT 0",
                "break_even_applied": "INTEGER DEFAULT 0",
                "break_even_time": "TEXT",
                "break_even_price": "REAL",
                "trailing_stop_enabled": "INTEGER DEFAULT 0",
                "max_favorable_price": "REAL",
                "max_adverse_price": "REAL",
                "mfe_price": "REAL",
                "mfe_amount": "REAL",
                "mfe_r": "REAL",
                "mae_price": "REAL",
                "mae_amount": "REAL",
                "mae_r": "REAL",
                "max_unrealized_profit": "REAL",
                "max_unrealized_loss": "REAL",
                "realized_r": "REAL",
                "exit_reason": "TEXT",
                "entry_indicators_json": "TEXT",
                "exit_indicators_json": "TEXT",
                "news_used_json": "TEXT",
                "news_health_json": "TEXT",
            },
            "decisions": {
                "run_id": "TEXT",
                "session_id": "TEXT",
                "signal_id": "TEXT",
                "mode": "TEXT DEFAULT 'demo_live'",
                "source": "TEXT DEFAULT 'bot'",
                "is_fixture": "INTEGER DEFAULT 0",
                "created_at_utc": "TEXT",
                "updated_at_utc": "TEXT",
            },
            "news": {
                "run_id": "TEXT",
                "session_id": "TEXT",
                "mode": "TEXT DEFAULT 'demo_live'",
                "is_fixture": "INTEGER DEFAULT 0",
                "provider_status": "TEXT DEFAULT 'UNKNOWN'",
                "created_at_utc": "TEXT",
                "updated_at_utc": "TEXT",
            },
            "session_reports": {
                "run_id": "TEXT",
                "session_id": "TEXT",
                "is_fixture": "INTEGER DEFAULT 0",
                "created_at_utc": "TEXT",
                "updated_at_utc": "TEXT",
            },
        }
        for table, table_columns in columns.items():
            existing = self._column_names(table)
            for name, definition in table_columns.items():
                if name not in existing:
                    self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

        now = utc_now_iso()
        self.connection.execute(
            "UPDATE trades SET internal_trade_id = COALESCE(internal_trade_id, 'legacy_trade_' || id), "
            "created_at_utc = COALESCE(created_at_utc, created_at), updated_at_utc = COALESCE(updated_at_utc, created_at), "
            "mode = COALESCE(mode, 'paper'), source = COALESCE(source, 'legacy'), is_fixture = CASE WHEN trade_id = 'example-001' THEN 1 ELSE COALESCE(is_fixture, 0) END, "
            "initial_volume = COALESCE(initial_volume, lot), remaining_volume = COALESCE(remaining_volume, lot), "
            "actual_entry_price = COALESCE(actual_entry_price, entry_price), initial_stop_loss = COALESCE(initial_stop_loss, stop_loss), "
            "final_stop_loss = COALESCE(final_stop_loss, stop_loss), pnl_gross = COALESCE(pnl_gross, pnl), pnl_net = COALESCE(pnl_net, pnl)"
        )
        self.connection.execute(
            "UPDATE decisions SET signal_id = COALESCE(signal_id, 'legacy_signal_' || id), "
            "created_at_utc = COALESCE(created_at_utc, created_at), updated_at_utc = COALESCE(updated_at_utc, created_at), "
            "mode = COALESCE(mode, 'paper'), source = COALESCE(source, 'legacy'), is_fixture = COALESCE(is_fixture, 0)"
        )
        self.connection.execute(
            "UPDATE news SET created_at_utc = COALESCE(created_at_utc, created_at, ?), "
            "updated_at_utc = COALESCE(updated_at_utc, created_at, ?), mode = COALESCE(mode, 'paper'), "
            "is_fixture = COALESCE(is_fixture, 0), provider_status = COALESCE(provider_status, 'UNKNOWN')",
            (now, now),
        )

    def _migrate_v3_demo_live_columns(self) -> None:
        columns = {
            "trades": {
                "setup_id": "TEXT",
                "strategy": "TEXT",
                "profile": "TEXT",
                "signal_score": "REAL",
                "tp1_volume": "REAL",
                "tp2_volume": "REAL",
                "tp1_time": "TEXT",
                "tp2_time": "TEXT",
                "risk_target_amount": "REAL",
                "raw_volume": "REAL",
                "rounded_volume": "REAL",
                "estimated_loss_after_rounding": "REAL",
                "estimated_margin": "REAL",
                "order_check_json": "TEXT",
                "management_state": "TEXT DEFAULT 'INITIAL_RISK'",
            },
            "decisions": {
                "setup_id": "TEXT",
                "strategy": "TEXT",
                "profile": "TEXT",
                "raw_score": "REAL",
                "required_score": "REAL",
                "bonuses_json": "TEXT",
                "penalties_json": "TEXT",
            },
        }
        for table, table_columns in columns.items():
            existing = self._column_names(table)
            for name, definition in table_columns.items():
                if name not in existing:
                    self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        self.connection.execute(
            "INSERT OR IGNORE INTO schema_migrations(version, applied_at_utc) VALUES(3, ?)",
            (utc_now_iso(),),
        )

    def _create_demo_only_triggers(self) -> None:
        self.connection.executescript(
            """
            CREATE TRIGGER IF NOT EXISTS trg_runs_demo_only
            BEFORE INSERT ON runs WHEN NEW.mode NOT IN ('demo_live', 'backtest')
            BEGIN SELECT RAISE(ABORT, 'paper/live modes are disabled'); END;
            CREATE TRIGGER IF NOT EXISTS trg_sessions_demo_only
            BEFORE INSERT ON trading_sessions WHEN NEW.mode NOT IN ('demo_live', 'backtest')
            BEGIN SELECT RAISE(ABORT, 'paper/live modes are disabled'); END;
            CREATE TRIGGER IF NOT EXISTS trg_trades_demo_only
            BEFORE INSERT ON trades WHEN NEW.mode NOT IN ('demo_live', 'backtest')
            BEGIN SELECT RAISE(ABORT, 'paper/live modes are disabled'); END;
            CREATE TRIGGER IF NOT EXISTS trg_decisions_demo_only
            BEFORE INSERT ON decisions WHEN NEW.mode NOT IN ('demo_live', 'backtest')
            BEGIN SELECT RAISE(ABORT, 'paper/live modes are disabled'); END;
            CREATE TRIGGER IF NOT EXISTS trg_news_demo_only
            BEFORE INSERT ON news WHEN NEW.mode NOT IN ('demo_live', 'backtest')
            BEGIN SELECT RAISE(ABORT, 'paper/live modes are disabled'); END;
            CREATE TRIGGER IF NOT EXISTS trg_reports_demo_only
            BEFORE INSERT ON session_reports WHEN NEW.mode != 'demo_live'
            BEGIN SELECT RAISE(ABORT, 'reports are demo_live only'); END;
            """
        )

    def _create_indexes(self) -> None:
        self.connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_trades_session_mode_fixture ON trades(session_id, mode, is_fixture);
            CREATE INDEX IF NOT EXISTS idx_trades_run_mode_fixture ON trades(run_id, mode, is_fixture);
            CREATE INDEX IF NOT EXISTS idx_decisions_session_mode_fixture ON decisions(session_id, mode, is_fixture);
            CREATE INDEX IF NOT EXISTS idx_news_session_mode_fixture ON news(session_id, mode, is_fixture);
            CREATE INDEX IF NOT EXISTS idx_position_events_trade ON position_events(internal_trade_id, timestamp_utc);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_internal_trade_id ON trades(internal_trade_id);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_mt5_deal ON trades(mt5_deal_ticket) WHERE mt5_deal_ticket IS NOT NULL AND is_fixture = 0;
            CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_mt5_position ON trades(mt5_position_id, mode) WHERE mt5_position_id IS NOT NULL AND is_fixture = 0;
            CREATE UNIQUE INDEX IF NOT EXISTS ux_decisions_signal_id ON decisions(signal_id);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_position_events_event_id ON position_events(event_id);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_mt5_deals_ticket ON mt5_deals(mt5_deal_ticket);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_mt5_orders_ticket ON mt5_orders(mt5_order_ticket);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_setups_setup_id ON setups(setup_id);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_setups_identity ON setups(symbol, strategy, direction, session, source_candle, structure_id);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_diagnostic_event_id ON diagnostic_events(event_id);
            CREATE INDEX IF NOT EXISTS idx_diagnostics_session_symbol ON diagnostic_events(session_id, symbol, code);
            CREATE INDEX IF NOT EXISTS idx_managed_positions_open ON managed_positions(closed, symbol);
            """
        )

    def _column_names(self, table: str) -> set[str]:
        cursor = self.connection.execute(f"PRAGMA table_info({table})")
        return {row["name"] for row in cursor.fetchall()}

    def insert_trade(self, trade: TradeRecord | dict[str, Any]) -> int:
        data = trade.to_dict() if isinstance(trade, TradeRecord) else dict(trade)
        now = utc_now_iso()
        internal_trade_id = data.get("internal_trade_id") or data.get("trade_id") or new_id("trd")
        existing_internal_id = self._existing_trade_internal_id(data)
        if existing_internal_id:
            internal_trade_id = existing_internal_id
            existing = self.connection.execute(
                "SELECT metadata_json, entry_indicators_json, news_used_json, news_health_json FROM trades WHERE internal_trade_id = ?",
                (existing_internal_id,),
            ).fetchone()
            if existing:
                data["metadata"] = {
                    **_safe_json(existing["metadata_json"], {}),
                    **dict(data.get("metadata") or {}),
                }
                if not data.get("entry_indicators") and not data.get("indicators"):
                    data["entry_indicators"] = _safe_json(existing["entry_indicators_json"], {})
                if not data.get("news_used") and not data.get("news_active"):
                    data["news_used"] = _safe_json(existing["news_used_json"], [])
                if not data.get("news_health"):
                    data["news_health"] = _safe_json(existing["news_health_json"], {})
        entry_price = data.get("actual_entry_price") or data.get("entry_price")
        stop_loss = data.get("initial_stop_loss") or data.get("stop_loss")
        risk_price = abs(float(entry_price) - float(stop_loss)) if entry_price is not None and stop_loss is not None else None
        row = {
            "trade_id": data.get("trade_id"),
            "run_id": data.get("run_id"),
            "session_id": data.get("session_id"),
            "signal_id": data.get("signal_id"),
            "internal_trade_id": internal_trade_id,
            "mt5_position_id": _str_or_none(data.get("mt5_position_id")),
            "mt5_order_ticket": _str_or_none(data.get("mt5_order_ticket")),
            "mt5_deal_ticket": _str_or_none(data.get("mt5_deal_ticket")),
            "parent_position_id": _str_or_none(data.get("parent_position_id")),
            "mode": data.get("mode", "demo_live"),
            "source": data.get("source", "bot"),
            "is_fixture": int(bool(data.get("is_fixture", False))),
            "created_at_utc": data.get("created_at_utc") or now,
            "updated_at_utc": data.get("updated_at_utc") or now,
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
            "pnl_gross": data.get("pnl_gross", data.get("pnl")),
            "pnl_net": data.get("pnl_net", data.get("pnl")),
            "commission": data.get("commission", 0.0),
            "swap": data.get("swap", 0.0),
            "duration_seconds": data.get("duration_seconds"),
            "spread": data.get("spread"),
            "spread_price": data.get("spread_price"),
            "spread_points": data.get("spread_points", data.get("spread")),
            "estimated_spread_cost": data.get("estimated_spread_cost"),
            "requested_price": data.get("requested_price", data.get("entry_price")),
            "actual_entry_price": data.get("actual_entry_price", data.get("entry_price")),
            "entry_slippage": data.get("entry_slippage"),
            "signal_time": data.get("signal_time"),
            "order_time": data.get("order_time"),
            "execution_time": data.get("execution_time", data.get("entry_time")),
            "initial_volume": data.get("initial_volume", data.get("lot")),
            "remaining_volume": data.get("remaining_volume", data.get("lot")),
            "initial_stop_loss": data.get("initial_stop_loss", data.get("stop_loss")),
            "final_stop_loss": data.get("final_stop_loss", data.get("stop_loss")),
            "initial_risk_price": data.get("initial_risk_price", risk_price),
            "initial_risk_amount": data.get("initial_risk_amount"),
            "initial_risk_percent": data.get("initial_risk_percent"),
            "tp1": data.get("tp1"),
            "tp1_close_percent": data.get("tp1_close_percent"),
            "tp1_actual_price": data.get("tp1_actual_price"),
            "tp1_pnl": data.get("tp1_pnl"),
            "tp2": data.get("tp2"),
            "tp2_actual_price": data.get("tp2_actual_price"),
            "tp2_pnl": data.get("tp2_pnl"),
            "sl_modification_count": data.get("sl_modification_count", 0),
            "break_even_applied": int(bool(data.get("break_even_applied", False))),
            "break_even_time": data.get("break_even_time"),
            "break_even_price": data.get("break_even_price"),
            "trailing_stop_enabled": int(bool(data.get("trailing_stop_enabled", False))),
            "max_favorable_price": data.get("max_favorable_price"),
            "max_adverse_price": data.get("max_adverse_price"),
            "mfe_price": data.get("mfe_price"),
            "mfe_amount": data.get("mfe_amount"),
            "mfe_r": data.get("mfe_r"),
            "mae_price": data.get("mae_price"),
            "mae_amount": data.get("mae_amount"),
            "mae_r": data.get("mae_r"),
            "max_unrealized_profit": data.get("max_unrealized_profit"),
            "max_unrealized_loss": data.get("max_unrealized_loss"),
            "realized_r": data.get("realized_r"),
            "exit_reason": data.get("exit_reason"),
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
            "entry_indicators_json": _json(data.get("entry_indicators", data.get("indicators", {}))),
            "exit_indicators_json": _json(data.get("exit_indicators", {})),
            "news_used_json": _json(data.get("news_used", data.get("news_active", []))),
            "news_health_json": _json(data.get("news_health", {})),
            "setup_id": data.get("setup_id"),
            "strategy": data.get("strategy"),
            "profile": data.get("profile"),
            "signal_score": data.get("signal_score", data.get("score")),
            "tp1_volume": data.get("tp1_volume"),
            "tp2_volume": data.get("tp2_volume"),
            "tp1_time": data.get("tp1_time"),
            "tp2_time": data.get("tp2_time"),
            "risk_target_amount": data.get("risk_target_amount"),
            "raw_volume": data.get("raw_volume"),
            "rounded_volume": data.get("rounded_volume"),
            "estimated_loss_after_rounding": data.get("estimated_loss_after_rounding"),
            "estimated_margin": data.get("estimated_margin"),
            "order_check_json": _json(data.get("order_check", {})),
            "management_state": data.get("management_state", "INITIAL_RISK"),
            "metadata_json": _json(data.get("metadata", {})),
        }
        return self._upsert("trades", row, "internal_trade_id")

    def _existing_trade_internal_id(self, data: dict[str, Any]) -> str | None:
        for column in ("mt5_deal_ticket", "mt5_position_id", "mt5_order_ticket"):
            value = _str_or_none(data.get(column))
            if not value:
                continue
            row = self.connection.execute(
                f"SELECT internal_trade_id FROM trades WHERE {column} = ? AND COALESCE(is_fixture, 0) = 0",
                (value,),
            ).fetchone()
            if row and row["internal_trade_id"]:
                return str(row["internal_trade_id"])
        return None

    def insert_decision(self, decision: SignalDecisionRecord | dict[str, Any]) -> int:
        data = decision.to_dict() if isinstance(decision, SignalDecisionRecord) else dict(decision)
        now = utc_now_iso()
        row = {
            "created_at": data["created_at"],
            "run_id": data.get("run_id"),
            "session_id": data.get("session_id"),
            "signal_id": data.get("signal_id") or new_id("sig"),
            "mode": data.get("mode", "demo_live"),
            "source": data.get("source", "bot"),
            "is_fixture": int(bool(data.get("is_fixture", False))),
            "created_at_utc": data.get("created_at_utc") or data.get("created_at") or now,
            "updated_at_utc": data.get("updated_at_utc") or now,
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
            "setup_id": data.get("setup_id"),
            "strategy": data.get("strategy"),
            "profile": data.get("profile"),
            "raw_score": data.get("raw_score", data.get("score")),
            "required_score": data.get("required_score"),
            "bonuses_json": _json(data.get("bonuses", [])),
            "penalties_json": _json(data.get("penalties", [])),
        }
        return self._upsert("decisions", row, "signal_id")

    def insert_news(self, news: NewsRecord | dict[str, Any]) -> int:
        data = news.to_dict() if isinstance(news, NewsRecord) else dict(news)
        now = utc_now_iso()
        row = {
            "published_at": data["published_at"],
            "run_id": data.get("run_id"),
            "session_id": data.get("session_id"),
            "mode": data.get("mode", "demo_live"),
            "is_fixture": int(bool(data.get("is_fixture", False))),
            "provider_status": data.get("provider_status", "UNKNOWN"),
            "created_at_utc": data.get("created_at_utc") or now,
            "updated_at_utc": data.get("updated_at_utc") or now,
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
        now = utc_now_iso()
        payload = {
            "run_id": row.get("run_id"),
            "session_id": row.get("session_id"),
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "session": row.get("session"),
            "mode": row.get("mode"),
            "is_fixture": int(bool(row.get("is_fixture", False))),
            "created_at_utc": row.get("created_at_utc") or now,
            "updated_at_utc": row.get("updated_at_utc") or now,
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

    def _upsert(self, table: str, row: dict[str, Any], conflict_column: str) -> int:
        existing_value = row.get(conflict_column)
        if existing_value is None:
            return self._insert(table, row)
        columns = ", ".join(row.keys())
        placeholders = ", ".join(f":{key}" for key in row)
        updates = ", ".join(
            f"{key}=COALESCE(excluded.{key}, {table}.{key})"
            for key in row
            if key not in {"id", conflict_column, "created_at_utc"}
        )
        cursor = self.connection.execute(
            f"""
            INSERT INTO {table} ({columns}) VALUES ({placeholders})
            ON CONFLICT({conflict_column}) DO UPDATE SET {updates}
            """,
            row,
        )
        self.connection.commit()
        if cursor.lastrowid:
            return int(cursor.lastrowid)
        existing = self.connection.execute(
            f"SELECT id FROM {table} WHERE {conflict_column} = ?", (existing_value,)
        ).fetchone()
        return int(existing["id"]) if existing else 0

    def fetch_between(
        self,
        table: str,
        time_column: str,
        start: str,
        end: str,
        mode: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        include_fixtures: bool = False,
    ) -> list[dict[str, Any]]:
        conditions = [f"{time_column} BETWEEN ? AND ?"]
        params: list[Any] = [start, end]
        columns = self._column_names(table)
        if "is_fixture" in columns and not include_fixtures:
            conditions.append("COALESCE(is_fixture, 0) = 0")
        if mode and "mode" in columns:
            conditions.append("mode = ?")
            params.append(mode)
        if run_id and "run_id" in columns:
            conditions.append("run_id = ?")
            params.append(run_id)
        if session_id and "session_id" in columns:
            conditions.append("session_id = ?")
            params.append(session_id)
        cursor = self.connection.execute(
            f"SELECT * FROM {table} WHERE {' AND '.join(conditions)} ORDER BY {time_column}",
            params,
        )
        return [dict(row) for row in cursor.fetchall()]

    def fetch_trades_between(self, start: str, end: str, **filters: Any) -> list[dict[str, Any]]:
        rows = self.fetch_between("trades", "entry_time", start, end, **filters)
        return deduplicate_trades(rows)

    def fetch_decisions_between(self, start: str, end: str, **filters: Any) -> list[dict[str, Any]]:
        return self.fetch_between("decisions", "created_at", start, end, **filters)

    def fetch_news_between(self, start: str, end: str, **filters: Any) -> list[dict[str, Any]]:
        return self.fetch_between("news", "published_at", start, end, **filters)

    def fetch_position_events_between(self, start: str, end: str, **filters: Any) -> list[dict[str, Any]]:
        return self.fetch_between("position_events", "timestamp_utc", start, end, **filters)

    def fetch_news_provider_status_between(self, start: str, end: str, **filters: Any) -> list[dict[str, Any]]:
        return self.fetch_between("news_provider_status", "created_at_utc", start, end, **filters)

    def executemany_news(self, items: Iterable[NewsRecord | dict[str, Any]]) -> list[int]:
        return [self.insert_news(item) for item in items]

    def create_run(self, mode: str, source: str, config: dict[str, Any] | None = None) -> str:
        run_id = new_id("run")
        now = utc_now_iso()
        self._insert(
            "runs",
            {
                "run_id": run_id,
                "mode": mode,
                "source": source,
                "started_at_utc": now,
                "ended_at_utc": None,
                "config_json": _json(config or {}),
                "created_at_utc": now,
                "updated_at_utc": now,
            },
        )
        return run_id

    def finish_run(self, run_id: str) -> None:
        self.connection.execute(
            "UPDATE runs SET ended_at_utc = ?, updated_at_utc = ? WHERE run_id = ?",
            (utc_now_iso(), utc_now_iso(), run_id),
        )
        self.connection.commit()

    def create_trading_session(
        self,
        run_id: str,
        session: str | None,
        mode: str,
        source: str,
        broker_timezone: str | None = None,
        broker_utc_offset_minutes: int | None = None,
    ) -> str:
        session_id = new_id("ses")
        now = utc_now_iso()
        self._insert(
            "trading_sessions",
            {
                "session_id": session_id,
                "run_id": run_id,
                "session": session,
                "mode": mode,
                "source": source,
                "started_at_utc": now,
                "ended_at_utc": None,
                "broker_timezone": broker_timezone,
                "broker_utc_offset_minutes": broker_utc_offset_minutes,
                "created_at_utc": now,
                "updated_at_utc": now,
            },
        )
        return session_id

    def insert_position_event(self, event: dict[str, Any]) -> int:
        now = utc_now_iso()
        row = {
            "event_id": event.get("event_id") or new_id("evt"),
            "run_id": event.get("run_id"),
            "session_id": event.get("session_id"),
            "internal_trade_id": event.get("internal_trade_id"),
            "mt5_position_id": _str_or_none(event.get("mt5_position_id")),
            "event_type": event["event_type"],
            "timestamp_utc": event.get("timestamp_utc") or now,
            "bid": event.get("bid"),
            "ask": event.get("ask"),
            "spread": event.get("spread"),
            "unrealized_profit": event.get("unrealized_profit"),
            "current_r": event.get("current_r"),
            "old_stop_loss": event.get("old_stop_loss"),
            "new_stop_loss": event.get("new_stop_loss"),
            "volume": event.get("volume"),
            "mt5_retcode": event.get("mt5_retcode"),
            "error_message": event.get("error_message"),
            "attempts": event.get("attempts", 0),
            "payload_json": _json(event.get("payload", {})),
            "is_fixture": int(bool(event.get("is_fixture", False))),
            "created_at_utc": event.get("created_at_utc") or now,
            "updated_at_utc": event.get("updated_at_utc") or now,
        }
        return self._upsert("position_events", row, "event_id")

    def insert_mt5_deal(self, deal: dict[str, Any]) -> int:
        now = utc_now_iso()
        row = {
            "mt5_deal_ticket": str(deal["mt5_deal_ticket"]),
            "mt5_order_ticket": _str_or_none(deal.get("mt5_order_ticket")),
            "mt5_position_id": _str_or_none(deal.get("mt5_position_id")),
            "run_id": deal.get("run_id"),
            "session_id": deal.get("session_id"),
            "mode": deal.get("mode", "demo_live"),
            "symbol": deal.get("symbol"),
            "direction": deal.get("direction"),
            "volume": deal.get("volume"),
            "price": deal.get("price"),
            "profit": deal.get("profit"),
            "commission": deal.get("commission"),
            "swap": deal.get("swap"),
            "time_utc": deal.get("time_utc"),
            "raw_json": _json(deal.get("raw", deal)),
            "is_fixture": int(bool(deal.get("is_fixture", False))),
            "created_at_utc": deal.get("created_at_utc") or now,
            "updated_at_utc": deal.get("updated_at_utc") or now,
        }
        return self._upsert("mt5_deals", row, "mt5_deal_ticket")

    def mt5_deal_exists(self, ticket: str | int) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM mt5_deals WHERE mt5_deal_ticket = ?", (str(ticket),)
        ).fetchone()
        return row is not None

    def insert_mt5_order(self, order: dict[str, Any]) -> int:
        now = utc_now_iso()
        row = {
            "mt5_order_ticket": str(order["mt5_order_ticket"]),
            "mt5_position_id": _str_or_none(order.get("mt5_position_id")),
            "run_id": order.get("run_id"),
            "session_id": order.get("session_id"),
            "mode": order.get("mode", "demo_live"),
            "symbol": order.get("symbol"),
            "direction": order.get("direction"),
            "volume_initial": order.get("volume_initial"),
            "volume_current": order.get("volume_current"),
            "price_open": order.get("price_open"),
            "sl": order.get("sl"),
            "tp": order.get("tp"),
            "time_setup_utc": order.get("time_setup_utc"),
            "raw_json": _json(order.get("raw", order)),
            "is_fixture": int(bool(order.get("is_fixture", False))),
            "created_at_utc": order.get("created_at_utc") or now,
            "updated_at_utc": order.get("updated_at_utc") or now,
        }
        return self._upsert("mt5_orders", row, "mt5_order_ticket")

    def mt5_order_exists(self, ticket: str | int) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM mt5_orders WHERE mt5_order_ticket = ?", (str(ticket),)
        ).fetchone()
        return row is not None

    def insert_setup(self, setup: dict[str, Any]) -> bool:
        now = utc_now_iso()
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO setups(
                setup_id, run_id, session_id, symbol, strategy, direction, session,
                source_candle, structure_id, detected_at_utc, expires_at_utc,
                executed_at_utc, created_at_utc, updated_at_utc
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                setup["setup_id"], setup.get("run_id"), setup.get("session_id"),
                setup["symbol"], setup["strategy"], setup["direction"], setup.get("session"),
                setup["source_candle"], setup["structure_id"],
                setup.get("detected_at") or setup.get("detected_at_utc") or now,
                setup.get("expires_at") or setup.get("expires_at_utc") or now,
                setup.get("executed_at_utc"), now, now,
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def get_setup(self, setup_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM setups WHERE setup_id = ?", (setup_id,)
        ).fetchone()
        return dict(row) if row else None

    def mark_setup_executed(self, setup_id: str, executed_at_utc: str) -> None:
        self.connection.execute(
            "UPDATE setups SET executed_at_utc = ?, updated_at_utc = ? WHERE setup_id = ? AND executed_at_utc IS NULL",
            (executed_at_utc, utc_now_iso(), setup_id),
        )
        self.connection.commit()

    def latest_symbol_cooldown(self, symbol: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM symbol_cooldowns WHERE symbol = ?", (symbol,)
        ).fetchone()
        return dict(row) if row else None

    def upsert_symbol_cooldown(
        self, symbol: str, structure_id: str, outcome: str, cooldown_until_utc: str
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO symbol_cooldowns(symbol, structure_id, outcome, cooldown_until_utc, updated_at_utc)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                structure_id=excluded.structure_id,
                outcome=excluded.outcome,
                cooldown_until_utc=excluded.cooldown_until_utc,
                updated_at_utc=excluded.updated_at_utc
            """,
            (symbol, structure_id, outcome, cooldown_until_utc, utc_now_iso()),
        )
        self.connection.commit()

    def insert_diagnostic_event(
        self,
        code: str,
        run_id: str | None = None,
        session_id: str | None = None,
        symbol: str | None = None,
        details: dict[str, Any] | None = None,
        event_id: str | None = None,
    ) -> int:
        now = utc_now_iso()
        return self._insert(
            "diagnostic_events",
            {
                "event_id": event_id or new_id("diag"),
                "run_id": run_id,
                "session_id": session_id,
                "symbol": symbol,
                "code": code,
                "timestamp_utc": now,
                "details_json": _json(details or {}),
                "created_at_utc": now,
            },
        )

    def fetch_diagnostic_events_between(self, start: str, end: str, **filters: Any) -> list[dict[str, Any]]:
        return self.fetch_between("diagnostic_events", "timestamp_utc", start, end, **filters)

    def upsert_managed_position(self, payload: dict[str, Any], closed: bool = False) -> None:
        now = utc_now_iso()
        position_id = str(payload["mt5_position_id"])
        self.connection.execute(
            """
            INSERT INTO managed_positions(
                mt5_position_id, internal_trade_id, run_id, session_id, symbol, state,
                payload_json, closed, created_at_utc, updated_at_utc
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mt5_position_id) DO UPDATE SET
                internal_trade_id=excluded.internal_trade_id,
                run_id=excluded.run_id,
                session_id=excluded.session_id,
                symbol=excluded.symbol,
                state=excluded.state,
                payload_json=excluded.payload_json,
                closed=excluded.closed,
                updated_at_utc=excluded.updated_at_utc
            """,
            (
                position_id, payload["internal_trade_id"], payload.get("run_id"),
                payload.get("session_id"), payload["symbol"], payload.get("state", "INITIAL_RISK"),
                _json(payload), int(closed), now, now,
            ),
        )
        self.connection.commit()

    def load_open_managed_positions(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT payload_json FROM managed_positions WHERE closed = 0 ORDER BY created_at_utc"
        ).fetchall()
        return [_safe_json(row["payload_json"], {}) for row in rows]

    def insert_heartbeat(self, run_id: str, session_id: str, payload: dict[str, Any]) -> int:
        return self._insert(
            "heartbeats",
            {
                "run_id": run_id,
                "session_id": session_id,
                "timestamp_utc": payload.get("utc") or utc_now_iso(),
                "payload_json": _json(payload),
            },
        )

    def fetch_open_trades(self, mode: str = "demo_live") -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM trades WHERE mode = ? AND COALESCE(is_fixture, 0) = 0 AND status IN ('OPEN', 'PARTIALLY_CLOSED')",
            (mode,),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_trade_fields(self, internal_trade_id: str, fields: dict[str, Any]) -> None:
        if not fields:
            return
        allowed = self._column_names("trades")
        normalized = {key: value for key, value in fields.items() if key in allowed and key != "id"}
        normalized["updated_at_utc"] = utc_now_iso()
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        self.connection.execute(
            f"UPDATE trades SET {assignments} WHERE internal_trade_id = ?",
            [*normalized.values(), internal_trade_id],
        )
        self.connection.commit()

    def close_trading_session(self, session_id: str) -> None:
        now = utc_now_iso()
        self.connection.execute(
            "UPDATE trading_sessions SET ended_at_utc = ?, updated_at_utc = ? WHERE session_id = ?",
            (now, now, session_id),
        )
        self.connection.commit()

    def cleanup_example_fixtures(self) -> int:
        cursor = self.connection.execute("DELETE FROM trades WHERE trade_id = 'example-001'")
        deleted = cursor.rowcount
        self.connection.commit()
        return int(deleted)

    def insert_news_provider_status(self, status: dict[str, Any]) -> int:
        now = utc_now_iso()
        return self._insert(
            "news_provider_status",
            {
                "run_id": status.get("run_id"),
                "session_id": status.get("session_id"),
                "provider": status["provider"],
                "last_request_utc": status.get("last_request_utc"),
                "last_success_utc": status.get("last_success_utc"),
                "article_count": status.get("article_count", 0),
                "event_count": status.get("event_count", 0),
                "status": status.get("status", "UNKNOWN"),
                "error": status.get("error"),
                "freshness_seconds": status.get("freshness_seconds"),
                "created_at_utc": status.get("created_at_utc") or now,
                "updated_at_utc": status.get("updated_at_utc") or now,
            },
        )


def _str_or_none(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    return str(value)


def _safe_json(value: Any, default: Any) -> Any:
    if value in {None, ""}:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def deduplicate_trades(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for row in rows:
        key = (
            row.get("mt5_deal_ticket")
            or row.get("mt5_position_id")
            or row.get("internal_trade_id")
            or row.get("trade_id")
            or f"{row.get('symbol')}|{row.get('entry_time')}|{row.get('direction')}|{row.get('entry_price')}"
        )
        key = str(key)
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result

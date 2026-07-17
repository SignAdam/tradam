from __future__ import annotations

import json
from pathlib import Path

from src.analytics.session_report import SessionReportGenerator
from src.storage.database import Database


def test_trade_cannot_appear_twice(tmp_path) -> None:
    db = Database(tmp_path / "bot.sqlite")
    db.initialize()
    trade = {
        "internal_trade_id": "trade_once",
        "symbol": "XAUUSD",
        "session": "US",
        "direction": "SELL",
        "lot": 0.29,
        "entry_price": 3997.40,
        "stop_loss": 4006.02,
        "take_profit": 3984.67,
        "entry_time": "2026-07-16T16:01:19+00:00",
        "mode": "demo_live",
    }
    db.insert_trade(trade)
    db.insert_trade({**trade, "pnl": -218.44})

    rows = db.fetch_trades_between(
        "2026-07-16T00:00:00+00:00",
        "2026-07-17T00:00:00+00:00",
        mode="demo_live",
    )

    assert len(rows) == 1
    assert rows[0]["pnl"] == -218.44
    db.close()


def test_fixtures_are_excluded_from_normal_reports(tmp_path) -> None:
    db = Database(tmp_path / "bot.sqlite")
    db.initialize()
    db.insert_trade(
        {
            "internal_trade_id": "fixture_trade",
            "trade_id": "example-001",
            "symbol": "XAUUSD",
            "session": "US",
            "direction": "BUY",
            "lot": 0.02,
            "entry_price": 1,
            "stop_loss": 0,
            "take_profit": 2,
            "entry_time": "2026-07-16T16:01:19+00:00",
            "mode": "paper",
            "is_fixture": True,
        }
    )
    paths = SessionReportGenerator(db, tmp_path / "reports").generate(
        "US",
        "2026-07-16T00:00:00+00:00",
        "2026-07-17T00:00:00+00:00",
        "paper",
        ["XAUUSD"],
        {},
    )
    data = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert data["metrics"]["unique_trades"] == 0
    assert "example-001" not in Path(paths["html"]).read_text(encoding="utf-8")
    db.close()


def test_report_mode_filter_separates_paper_and_demo_live(tmp_path) -> None:
    db = Database(tmp_path / "bot.sqlite")
    db.initialize()
    base = {
        "symbol": "XAUUSD",
        "session": "US",
        "direction": "SELL",
        "lot": 0.1,
        "entry_price": 10,
        "stop_loss": 11,
        "take_profit": 8,
        "entry_time": "2026-07-16T16:01:19+00:00",
        "pnl": 1,
    }
    db.insert_trade({**base, "internal_trade_id": "paper_trade", "mode": "paper"})
    db.insert_trade({**base, "internal_trade_id": "demo_trade", "mode": "demo_live", "pnl": -2})
    paths = SessionReportGenerator(db, tmp_path / "reports").generate(
        "US",
        "2026-07-16T00:00:00+00:00",
        "2026-07-17T00:00:00+00:00",
        "demo_live",
        ["XAUUSD"],
        {},
    )
    data = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
    assert data["mode"] == "demo_live"
    assert data["metrics"]["net_pnl"] == -2
    assert len(data["trades"]) == 1
    db.close()


def test_reconciliation_updates_existing_position_without_duplicate(tmp_path) -> None:
    db = Database(tmp_path / "bot.sqlite")
    db.initialize()
    base = {
        "symbol": "XAUUSD",
        "session": "US",
        "direction": "SELL",
        "lot": 0.29,
        "entry_price": 3997.40,
        "stop_loss": 4006.02,
        "take_profit": 3984.67,
        "entry_time": "2026-07-16T16:01:19+00:00",
        "mode": "demo_live",
        "mt5_position_id": "500",
    }
    db.insert_trade({**base, "internal_trade_id": "bot_created"})
    db.insert_trade({**base, "internal_trade_id": "mt5_position_500", "pnl": -218.44})

    rows = db.fetch_trades_between(
        "2026-07-16T00:00:00+00:00",
        "2026-07-17T00:00:00+00:00",
        mode="demo_live",
    )

    assert len(rows) == 1
    assert rows[0]["internal_trade_id"] == "bot_created"
    assert rows[0]["pnl"] == -218.44
    db.close()

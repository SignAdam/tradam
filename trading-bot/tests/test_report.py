from __future__ import annotations

from pathlib import Path

from src.analytics.session_report import SessionReportGenerator
from src.storage.database import Database
from src.storage.models import SignalDecisionRecord, TradeRecord


def test_session_report_generates_html_csv_json(tmp_path) -> None:
    db = Database(tmp_path / "bot.sqlite")
    db.initialize()
    db.insert_decision(
        SignalDecisionRecord(
            symbol="XAUUSD",
            session="US",
            direction="BUY",
            score=8,
            decision="ACCEPTED",
            reasons=["H1 bullish", "Fibonacci confluence"],
            created_at="2026-07-07T15:00:00+00:00",
        )
    )
    db.insert_trade(
        TradeRecord(
            symbol="XAUUSD",
            session="US",
            direction="BUY",
            lot=0.02,
            entry_price=100,
            stop_loss=99,
            take_profit=102,
            entry_time="2026-07-07T15:01:00+00:00",
            exit_time="2026-07-07T15:20:00+00:00",
            exit_price=102,
            pnl=40,
            status="CLOSED",
        )
    )
    paths = SessionReportGenerator(db, tmp_path / "reports").generate(
        "US",
        "2026-07-07T14:30:00+00:00",
        "2026-07-07T22:00:00+00:00",
        "paper",
        ["XAUUSD"],
        {"settings": {"trading": {"mode": "paper"}}},
    )
    assert set(paths) == {"csv", "json", "html"}
    for path in paths.values():
        assert Path(path).exists()
    assert "Session Report" in open(paths["html"], encoding="utf-8").read()
    db.close()

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src.strategy.session_filter import SessionFilter


def test_session_filter_allows_us_session() -> None:
    config = {
        "timezone": "Europe/Paris",
        "sessions": {
            "US": {
                "enabled": True,
                "start": "14:30",
                "end": "22:00",
                "allow_new_trades_until_minutes_before_end": 20,
                "close_positions_before_end_minutes": 10,
            }
        },
        "low_liquidity_blocks": [],
    }
    state = SessionFilter(config).evaluate(datetime(2026, 7, 7, 15, 0, tzinfo=ZoneInfo("Europe/Paris")))
    assert state["session"] == "US"
    assert state["allow_new_trades"]


def test_session_filter_blocks_low_liquidity_window() -> None:
    config = {
        "timezone": "Europe/Paris",
        "sessions": {
            "US": {
                "enabled": True,
                "start": "14:30",
                "end": "23:30",
                "allow_new_trades_until_minutes_before_end": 20,
                "close_positions_before_end_minutes": 10,
            }
        },
        "low_liquidity_blocks": [
            {"enabled": True, "start": "22:55", "end": "23:10", "reason": "rollover"}
        ],
    }
    state = SessionFilter(config).evaluate(datetime(2026, 7, 7, 23, 0, tzinfo=ZoneInfo("Europe/Paris")))
    assert not state["allow_new_trades"]
    assert "rollover" in state["reasons"]


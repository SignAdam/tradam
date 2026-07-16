from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.news.economic_calendar import EconomicEvent
from src.news.news_filter import NewsFilter


def test_news_filter_blocks_high_impact_calendar_window() -> None:
    now = datetime(2026, 7, 7, 12, 0, tzinfo=timezone.utc)
    event = EconomicEvent(
        title="US CPI inflation",
        country="US",
        currency="USD",
        event_time=(now + timedelta(minutes=10)).isoformat(),
        impact="high",
        source="test",
    )
    decision = NewsFilter(
        {
            "high_impact_block_before_minutes": 30,
            "high_impact_block_after_minutes": 30,
            "dangerous_sentiment_threshold": -0.45,
        }
    ).evaluate("XAUUSD", [], [event], now=now)
    assert decision["blocked"]
    assert "US CPI" in decision["reasons"][0]


"""Combine news, sentiment, and calendar data into trade filters."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.news.economic_calendar import EconomicCalendar, EconomicEvent
from src.news.news_client import NewsArticle
from src.news.sentiment_engine import SentimentEngine


class NewsFilter:
    def __init__(
        self,
        config: dict[str, Any],
        sentiment_engine: SentimentEngine | None = None,
        economic_calendar: EconomicCalendar | None = None,
    ) -> None:
        self.config = config
        self.sentiment_engine = sentiment_engine or SentimentEngine(
            positive_threshold=float(config.get("positive_sentiment_threshold", 0.25)),
            negative_threshold=float(config.get("negative_sentiment_threshold", -0.25)),
        )
        self.calendar = economic_calendar or EconomicCalendar(config)

    def evaluate(
        self,
        symbol_group: str,
        articles: list[NewsArticle | dict[str, Any]],
        events: list[EconomicEvent | dict[str, Any]],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        article_dicts = [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in articles]
        event_objects = [self._event(item) for item in events]
        aggregate = self.sentiment_engine.aggregate(article_dicts)
        blocked_events = self.calendar.high_impact_events_near(
            event_objects,
            now,
            int(self.config.get("high_impact_block_before_minutes", 30)),
            int(self.config.get("high_impact_block_after_minutes", 30)),
            currencies=self._currencies_for_symbol(symbol_group),
        )
        dangerous_articles = [
            article
            for article in article_dicts
            if article.get("impact") == "high"
            and float(article.get("score", 0.0) or 0.0)
            <= float(self.config.get("dangerous_sentiment_threshold", -0.45))
        ]
        reasons: list[str] = []
        if blocked_events:
            reasons.extend(f"High-impact calendar event: {event.title}" for event in blocked_events)
        if dangerous_articles:
            reasons.extend(f"Dangerous high-impact news: {article['title']}" for article in dangerous_articles[:3])
        return {
            "symbol_group": symbol_group,
            "blocked": bool(blocked_events or dangerous_articles),
            "reasons": reasons,
            "sentiment": aggregate["sentiment"],
            "score": aggregate["score"],
            "impact": "high" if blocked_events or dangerous_articles else aggregate["impact"],
            "active_news": article_dicts[:10],
            "active_events": [event.to_dict() for event in blocked_events],
        }

    @staticmethod
    def _event(item: EconomicEvent | dict[str, Any]) -> EconomicEvent:
        if isinstance(item, EconomicEvent):
            return item
        return EconomicEvent(
            title=item.get("title", item.get("event", "")),
            country=item.get("country", ""),
            currency=item.get("currency"),
            event_time=item.get("event_time", item.get("date", "")),
            impact=item.get("impact", "low"),
            source=item.get("source", "manual"),
            raw=item.get("raw", item),
        )

    @staticmethod
    def _currencies_for_symbol(symbol_group: str) -> set[str]:
        if symbol_group in {"XAUUSD", "DJ30"}:
            return {"USD"}
        if symbol_group == "BTC":
            return {"USD", "BTC"}
        return {"USD"}


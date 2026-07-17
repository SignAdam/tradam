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
        provider_health: list[dict[str, Any]] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        article_dicts = [item.to_dict() if hasattr(item, "to_dict") else dict(item) for item in articles]
        event_objects = [self._event(item) for item in events]
        aggregate = self.sentiment_engine.aggregate(article_dicts)
        provider_health = provider_health or []
        news_health = [item for item in provider_health if "calendar" not in str(item.get("provider", "")).lower()]
        calendar_health = [item for item in provider_health if "calendar" in str(item.get("provider", "")).lower()]
        news_state = self._provider_state(news_health, bool(article_dicts))
        calendar_state = self._provider_state(calendar_health, bool(event_objects))
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
        degraded_block = self._should_block_degraded(symbol_group, news_state, calendar_state)
        if degraded_block:
            reasons.append(f"News/calendar data degraded: news={news_state}, calendar={calendar_state}")
        medium_events = self._events_near(
            event_objects,
            now,
            int(self.config.get("high_impact_block_before_minutes", 30)),
            int(self.config.get("high_impact_block_after_minutes", 30)),
            self._currencies_for_symbol(symbol_group),
            "medium",
        )
        medium_penalty = float(self.config.get("medium_impact_score_penalty", 0.5)) if medium_events else 0.0
        risk_multiplier = float(self.config.get("medium_impact_risk_multiplier", 0.75)) if medium_events else 1.0
        calendar_evidence = {
            "status": calendar_state,
            "providers": calendar_health,
            "events_evaluated": len(event_objects),
            "high_impact_events_in_window": [event.to_dict() for event in blocked_events],
            "valid_no_high_impact_evidence": calendar_state in {"HEALTHY", "EMPTY_VALID_RESPONSE"} and not blocked_events,
        }
        return {
            "symbol_group": symbol_group,
            "blocked": bool(blocked_events or dangerous_articles or degraded_block),
            "reasons": reasons,
            "sentiment": aggregate["sentiment"] if news_state == "HEALTHY" else "unknown",
            "score": aggregate["score"],
            "impact": "high" if blocked_events or dangerous_articles or degraded_block else aggregate["impact"],
            "data_state": news_state,
            "news_state": news_state,
            "calendar_state": calendar_state,
            "score_penalty": medium_penalty,
            "risk_multiplier": risk_multiplier,
            "active_news": article_dicts[:10],
            "active_events": [event.to_dict() for event in blocked_events],
            "medium_impact_events": [event.to_dict() for event in medium_events],
            "calendar_evidence": calendar_evidence,
            "provider_health": provider_health,
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
        if symbol_group in {"XAUUSD", "US30", "NAS100", "NDX100"}:
            return {"USD"}
        if symbol_group == "BTCUSD":
            return {"USD", "BTC"}
        if symbol_group == "AUDNZD":
            return {"AUD", "NZD", "CNY"}
        return {"USD"}

    @staticmethod
    def _provider_state(
        provider_health: list[dict[str, Any]],
        has_usable_data: bool,
    ) -> str:
        statuses = {str(item.get("status", "UNKNOWN")) for item in provider_health}
        if not provider_health:
            return "UNAVAILABLE"
        if statuses and statuses <= {"NOT_CONFIGURED"}:
            return "NOT_CONFIGURED"
        if any(status == "HEALTHY" for status in statuses):
            return "HEALTHY" if has_usable_data else "EMPTY_VALID_RESPONSE"
        if any(status == "EMPTY_VALID_RESPONSE" for status in statuses):
            return "EMPTY_VALID_RESPONSE"
        if any(status == "STALE" for status in statuses):
            return "STALE"
        if any(status == "RATE_LIMITED" for status in statuses):
            return "RATE_LIMITED"
        if any(status == "AUTH_ERROR" for status in statuses):
            return "AUTH_ERROR"
        return "UNAVAILABLE"

    def _should_block_degraded(
        self, symbol_group: str, news_state: str, calendar_state: str
    ) -> bool:
        calendar_degraded = calendar_state not in {"HEALTHY", "EMPTY_VALID_RESPONSE"}
        news_degraded = news_state not in {"HEALTHY", "EMPTY_VALID_RESPONSE"}
        if symbol_group in {"XAUUSD", "US30", "NAS100", "NDX100"}:
            if not (calendar_degraded or news_degraded):
                return False
            return bool(self.config.get("block_when_macro_news_unavailable", True))
        if symbol_group == "AUDNZD":
            return calendar_degraded and bool(self.config.get("block_audnzd_when_calendar_unavailable", True))
        if symbol_group == "BTCUSD":
            if not (calendar_degraded and news_degraded):
                return False
            return bool(self.config.get("block_btc_when_news_unavailable", False))
        return False

    @staticmethod
    def _events_near(
        events: list[EconomicEvent],
        now: datetime,
        before_minutes: int,
        after_minutes: int,
        currencies: set[str],
        impact: str,
    ) -> list[EconomicEvent]:
        from datetime import timedelta
        from src.news.economic_calendar import parse_event_time

        result: list[EconomicEvent] = []
        for event in events:
            if event.impact != impact:
                continue
            if event.currency and event.currency.upper() not in currencies:
                continue
            event_time = parse_event_time(event.event_time)
            if event_time and event_time - timedelta(minutes=before_minutes) <= now <= event_time + timedelta(minutes=after_minutes):
                result.append(event)
        return result

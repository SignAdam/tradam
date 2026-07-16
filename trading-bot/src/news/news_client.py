"""Optional news API clients with graceful fallback when keys or providers fail."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from src.news.sentiment_engine import SentimentEngine


@dataclass
class NewsArticle:
    symbol_group: str
    title: str
    source: str
    published_at: str
    url: str | None = None
    summary: str | None = None
    impact: str = "low"
    sentiment: str = "neutral"
    score: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class NewsClient:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        session: requests.Session | None = None,
        sentiment_engine: SentimentEngine | None = None,
    ) -> None:
        self.config = config or {}
        self.timeout = int(self.config.get("request_timeout_seconds", 8))
        self.session = session or requests.Session()
        self.sentiment_engine = sentiment_engine or SentimentEngine(
            positive_threshold=float(self.config.get("positive_sentiment_threshold", 0.25)),
            negative_threshold=float(self.config.get("negative_sentiment_threshold", -0.25)),
        )

    def fetch_for_symbol(self, symbol_group: str, queries: list[str], limit: int = 20) -> list[NewsArticle]:
        articles: list[NewsArticle] = []
        for query in queries[:5]:
            articles.extend(self._marketaux(symbol_group, query))
            articles.extend(self._newsapi(symbol_group, query))
        articles.extend(self._alpha_vantage(symbol_group, queries))
        articles.extend(self._finnhub(symbol_group))
        if symbol_group.upper() == "BTC":
            articles.extend(self._binance_context(symbol_group))
        deduped = self._dedupe(articles)
        return deduped[:limit]

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException:
            return None
        except ValueError:
            return None

    def _marketaux(self, symbol_group: str, query: str) -> list[NewsArticle]:
        token = os.getenv("MARKETAUX_API_KEY")
        if not token:
            return []
        data = self._get_json(
            "https://api.marketaux.com/v1/news/all",
            {"api_token": token, "search": query, "language": "en", "limit": 5},
        )
        if not data:
            return []
        return [
            self._article(
                symbol_group,
                title=item.get("title", ""),
                source=item.get("source", "Marketaux"),
                published_at=item.get("published_at"),
                url=item.get("url"),
                summary=item.get("description"),
                raw=item,
            )
            for item in data.get("data", [])
            if item.get("title")
        ]

    def _newsapi(self, symbol_group: str, query: str) -> list[NewsArticle]:
        token = os.getenv("NEWSAPI_API_KEY")
        if not token:
            return []
        data = self._get_json(
            "https://newsapi.org/v2/everything",
            {"apiKey": token, "q": query, "language": "en", "pageSize": 5, "sortBy": "publishedAt"},
        )
        if not data:
            return []
        return [
            self._article(
                symbol_group,
                title=item.get("title", ""),
                source=(item.get("source") or {}).get("name", "NewsAPI"),
                published_at=item.get("publishedAt"),
                url=item.get("url"),
                summary=item.get("description"),
                raw=item,
            )
            for item in data.get("articles", [])
            if item.get("title")
        ]

    def _alpha_vantage(self, symbol_group: str, queries: list[str]) -> list[NewsArticle]:
        token = os.getenv("ALPHA_VANTAGE_API_KEY")
        if not token:
            return []
        data = self._get_json(
            "https://www.alphavantage.co/query",
            {"function": "NEWS_SENTIMENT", "apikey": token, "topics": "financial_markets", "limit": 20},
        )
        if not data:
            return []
        results: list[NewsArticle] = []
        for item in data.get("feed", []):
            title = item.get("title", "")
            summary = item.get("summary", "")
            if not title:
                continue
            combined = f"{title} {summary}".lower()
            if not any(term.lower() in combined for term in queries):
                continue
            provider_score = _float_or_none(item.get("overall_sentiment_score"))
            results.append(
                self._article(
                    symbol_group,
                    title=title,
                    source=item.get("source", "Alpha Vantage"),
                    published_at=_alpha_time(item.get("time_published")),
                    url=item.get("url"),
                    summary=summary,
                    provider_score=provider_score,
                    raw=item,
                )
            )
        return results

    def _finnhub(self, symbol_group: str) -> list[NewsArticle]:
        token = os.getenv("FINNHUB_API_KEY")
        if not token:
            return []
        data = self._get_json("https://finnhub.io/api/v1/news", {"category": "general", "token": token})
        if not isinstance(data, list):
            return []
        keywords = {
            "XAUUSD": ["gold", "fed", "inflation", "cpi", "usd"],
            "BTC": ["bitcoin", "crypto", "btc", "binance", "coinbase"],
            "DJ30": ["dow", "stocks", "fed", "earnings", "cpi"],
        }.get(symbol_group, [symbol_group.lower()])
        results = []
        for item in data:
            title = item.get("headline", "")
            summary = item.get("summary", "")
            combined = f"{title} {summary}".lower()
            if not any(keyword in combined for keyword in keywords):
                continue
            published = datetime.fromtimestamp(item.get("datetime", 0), tz=timezone.utc).isoformat()
            results.append(
                self._article(
                    symbol_group,
                    title=title,
                    source=item.get("source", "Finnhub"),
                    published_at=published,
                    url=item.get("url"),
                    summary=summary,
                    raw=item,
                )
            )
        return results

    def _binance_context(self, symbol_group: str) -> list[NewsArticle]:
        data = self._get_json("https://api.binance.com/api/v3/ticker/24hr", {"symbol": "BTCUSDT"})
        if not data:
            return []
        change = _float_or_none(data.get("priceChangePercent")) or 0.0
        title = f"BTCUSDT 24h change {change:.2f}% on Binance public ticker"
        provider_score = max(min(change / 10, 1), -1)
        return [
            self._article(
                symbol_group,
                title=title,
                source="Binance public API",
                published_at=datetime.now(timezone.utc).isoformat(),
                summary="Market context only, not a news article.",
                provider_score=provider_score,
                raw=data,
            )
        ]

    def _article(
        self,
        symbol_group: str,
        title: str,
        source: str,
        published_at: str | None,
        url: str | None = None,
        summary: str | None = None,
        provider_score: float | None = None,
        raw: dict[str, Any] | None = None,
    ) -> NewsArticle:
        score = self.sentiment_engine.score_text(f"{title} {summary or ''}", provider_score)
        return NewsArticle(
            symbol_group=symbol_group,
            title=title,
            source=source,
            published_at=published_at or datetime.now(timezone.utc).isoformat(),
            url=url,
            summary=summary,
            impact=score.impact,
            sentiment=score.label,
            score=score.score,
            raw=raw or {},
        )

    @staticmethod
    def _dedupe(articles: list[NewsArticle]) -> list[NewsArticle]:
        seen: set[str] = set()
        deduped: list[NewsArticle] = []
        for article in articles:
            key = (article.title or "").strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(article)
        return sorted(deduped, key=lambda item: item.published_at, reverse=True)


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _alpha_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return value

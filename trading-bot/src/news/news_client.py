"""Optional financial-news clients with explicit provider health states."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import requests

from src.news.sentiment_engine import SentimentEngine


PROVIDER_STATES = {
    "HEALTHY",
    "STALE",
    "UNAVAILABLE",
    "RATE_LIMITED",
    "AUTH_ERROR",
    "EMPTY_VALID_RESPONSE",
    "NOT_CONFIGURED",
}


@dataclass
class NewsArticle:
    symbol_group: str
    title: str
    source: str
    published_at: str
    url: str | None = None
    summary: str | None = None
    impact: str = "low"
    sentiment: str = "unknown"
    score: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProviderHealth:
    provider: str
    status: str
    last_request_utc: str
    last_success_utc: str | None = None
    article_count: int = 0
    event_count: int = 0
    error: str | None = None
    freshness_seconds: int | None = None

    def __post_init__(self) -> None:
        if self.status not in PROVIDER_STATES:
            raise ValueError(f"Unsupported provider state: {self.status}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ApiResponse:
    data: Any
    status: str
    error: str | None = None


class NewsClient:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        session: requests.Session | None = None,
        sentiment_engine: SentimentEngine | None = None,
    ) -> None:
        self.config = config or {}
        self.timeout = int(self.config.get("request_timeout_seconds", 8))
        self.max_age_seconds = int(self.config.get("max_data_age_minutes", 60)) * 60
        self.session = session or requests.Session()
        self.sentiment_engine = sentiment_engine or SentimentEngine(
            positive_threshold=float(self.config.get("positive_sentiment_threshold", 0.25)),
            negative_threshold=float(self.config.get("negative_sentiment_threshold", -0.25)),
        )
        self.provider_health: list[ProviderHealth] = []

    def fetch_for_symbol(
        self, symbol_group: str, queries: list[str], limit: int = 20
    ) -> list[NewsArticle]:
        self.provider_health = []
        articles: list[NewsArticle] = []
        for query in queries[:5]:
            articles.extend(self._marketaux(symbol_group, query))
            articles.extend(self._newsapi(symbol_group, query))
        articles.extend(self._alpha_vantage(symbol_group, queries))
        articles.extend(self._finnhub(symbol_group))
        if symbol_group.upper() == "BTCUSD":
            articles.extend(self._binance_context(symbol_group))
        return self._dedupe(articles)[:limit]

    def _get_json(self, url: str, params: dict[str, Any]) -> ApiResponse:
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            if response.status_code in {401, 403}:
                return ApiResponse(None, "AUTH_ERROR", f"HTTP {response.status_code}")
            if response.status_code == 429:
                return ApiResponse(None, "RATE_LIMITED", "HTTP 429")
            response.raise_for_status()
            return ApiResponse(response.json(), "HEALTHY")
        except requests.RequestException as exc:
            return ApiResponse(None, "UNAVAILABLE", str(exc))
        except ValueError as exc:
            return ApiResponse(None, "UNAVAILABLE", f"Invalid JSON: {exc}")

    def _marketaux(self, symbol_group: str, query: str) -> list[NewsArticle]:
        requested = _utc_now()
        token = os.getenv("MARKETAUX_API_KEY")
        if not token:
            self._health("Marketaux", "NOT_CONFIGURED", requested)
            return []
        response = self._get_json(
            "https://api.marketaux.com/v1/news/all",
            {"api_token": token, "search": query, "language": "en", "limit": 5},
        )
        if response.data is None:
            self._health("Marketaux", response.status, requested, error=response.error)
            return []
        rows = response.data.get("data", []) if isinstance(response.data, dict) else []
        articles = [
            self._article(
                symbol_group,
                item.get("title", ""),
                item.get("source", "Marketaux"),
                item.get("published_at"),
                item.get("url"),
                item.get("description"),
                raw=item,
            )
            for item in rows
            if item.get("title")
        ]
        self._health_for_articles("Marketaux", articles, requested)
        return articles

    def _newsapi(self, symbol_group: str, query: str) -> list[NewsArticle]:
        requested = _utc_now()
        token = os.getenv("NEWSAPI_API_KEY")
        if not token:
            self._health("NewsAPI", "NOT_CONFIGURED", requested)
            return []
        response = self._get_json(
            "https://newsapi.org/v2/everything",
            {"apiKey": token, "q": query, "language": "en", "pageSize": 5, "sortBy": "publishedAt"},
        )
        if response.data is None:
            self._health("NewsAPI", response.status, requested, error=response.error)
            return []
        rows = response.data.get("articles", []) if isinstance(response.data, dict) else []
        articles = [
            self._article(
                symbol_group,
                item.get("title", ""),
                (item.get("source") or {}).get("name", "NewsAPI"),
                item.get("publishedAt"),
                item.get("url"),
                item.get("description"),
                raw=item,
            )
            for item in rows
            if item.get("title")
        ]
        self._health_for_articles("NewsAPI", articles, requested)
        return articles

    def _alpha_vantage(self, symbol_group: str, queries: list[str]) -> list[NewsArticle]:
        requested = _utc_now()
        token = os.getenv("ALPHA_VANTAGE_API_KEY")
        if not token:
            self._health("Alpha Vantage", "NOT_CONFIGURED", requested)
            return []
        response = self._get_json(
            "https://www.alphavantage.co/query",
            {"function": "NEWS_SENTIMENT", "apikey": token, "topics": "financial_markets", "limit": 50},
        )
        if response.data is None:
            self._health("Alpha Vantage", response.status, requested, error=response.error)
            return []
        rows = response.data.get("feed", []) if isinstance(response.data, dict) else []
        results: list[NewsArticle] = []
        for item in rows:
            title, summary = item.get("title", ""), item.get("summary", "")
            if not title or not any(term.lower() in f"{title} {summary}".lower() for term in queries):
                continue
            results.append(
                self._article(
                    symbol_group,
                    title,
                    item.get("source", "Alpha Vantage"),
                    _alpha_time(item.get("time_published")),
                    item.get("url"),
                    summary,
                    _float_or_none(item.get("overall_sentiment_score")),
                    item,
                )
            )
        self._health_for_articles("Alpha Vantage", results, requested)
        return results

    def _finnhub(self, symbol_group: str) -> list[NewsArticle]:
        requested = _utc_now()
        token = os.getenv("FINNHUB_API_KEY")
        if not token:
            self._health("Finnhub", "NOT_CONFIGURED", requested)
            return []
        response = self._get_json(
            "https://finnhub.io/api/v1/news", {"category": "general", "token": token}
        )
        if response.data is None:
            self._health("Finnhub", response.status, requested, error=response.error)
            return []
        if not isinstance(response.data, list):
            self._health("Finnhub", "UNAVAILABLE", requested, error="Unexpected response shape")
            return []
        keywords = {
            "XAUUSD": ["gold", "fed", "inflation", "cpi", "usd", "geopolit"],
            "BTCUSD": ["bitcoin", "crypto", "btc", "binance", "coinbase", "etf"],
            "US30": ["dow", "stocks", "fed", "earnings", "cpi"],
            "NAS100": ["nasdaq", "stocks", "fed", "earnings", "cpi"],
            "NDX100": ["nasdaq", "stocks", "fed", "earnings", "cpi"],
            "AUDNZD": ["rba", "rbnz", "australia", "new zealand", "china"],
        }.get(symbol_group, [symbol_group.lower()])
        results: list[NewsArticle] = []
        for item in response.data:
            title, summary = item.get("headline", ""), item.get("summary", "")
            if not any(keyword in f"{title} {summary}".lower() for keyword in keywords):
                continue
            published = datetime.fromtimestamp(item.get("datetime", 0), tz=timezone.utc).isoformat()
            results.append(
                self._article(
                    symbol_group, title, item.get("source", "Finnhub"), published,
                    item.get("url"), summary, raw=item
                )
            )
        self._health_for_articles("Finnhub", results, requested)
        return results

    def _binance_context(self, symbol_group: str) -> list[NewsArticle]:
        requested = _utc_now()
        response = self._get_json(
            "https://api.binance.com/api/v3/ticker/24hr", {"symbol": "BTCUSDT"}
        )
        if response.data is None:
            self._health("Binance public API", response.status, requested, error=response.error)
            return []
        if not isinstance(response.data, dict):
            self._health("Binance public API", "UNAVAILABLE", requested, error="Unexpected response shape")
            return []
        change = _float_or_none(response.data.get("priceChangePercent")) or 0.0
        article = self._article(
            symbol_group,
            f"BTCUSDT 24h change {change:.2f}% on Binance public ticker",
            "Binance public API",
            requested,
            summary="Market context only; it is not evidence of news-calendar availability.",
            provider_score=max(min(change / 10, 1), -1),
            raw=response.data,
        )
        self._health_for_articles("Binance public API", [article], requested)
        return [article]

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
            published_at=published_at or _utc_now(),
            url=url,
            summary=summary,
            impact=score.impact,
            sentiment=score.label,
            score=score.score,
            raw=raw or {},
        )

    def _health_for_articles(
        self, provider: str, articles: list[NewsArticle], requested: str
    ) -> None:
        if not articles:
            self._health(
                provider, "EMPTY_VALID_RESPONSE", requested, requested,
                article_count=0, freshness_seconds=0
            )
            return
        newest = max((_parse_datetime(item.published_at) for item in articles), default=None)
        freshness = int((datetime.now(timezone.utc) - newest).total_seconds()) if newest else None
        status = "STALE" if freshness is None or freshness > self.max_age_seconds else "HEALTHY"
        self._health(
            provider, status, requested, requested,
            article_count=len(articles), freshness_seconds=freshness
        )

    def _health(
        self,
        provider: str,
        status: str,
        last_request_utc: str,
        last_success_utc: str | None = None,
        article_count: int = 0,
        error: str | None = None,
        freshness_seconds: int | None = None,
    ) -> None:
        existing = next((item for item in self.provider_health if item.provider == provider), None)
        if existing is None:
            self.provider_health.append(
                ProviderHealth(
                    provider, status, last_request_utc, last_success_utc,
                    article_count, 0, error, freshness_seconds
                )
            )
            return
        existing.last_request_utc = max(existing.last_request_utc, last_request_utc)
        existing.article_count += article_count
        existing.last_success_utc = last_success_utc or existing.last_success_utc
        if freshness_seconds is not None:
            existing.freshness_seconds = (
                freshness_seconds
                if existing.freshness_seconds is None
                else min(existing.freshness_seconds, freshness_seconds)
            )
        rank = {
            "HEALTHY": 7,
            "EMPTY_VALID_RESPONSE": 6,
            "STALE": 5,
            "RATE_LIMITED": 4,
            "AUTH_ERROR": 3,
            "UNAVAILABLE": 2,
            "NOT_CONFIGURED": 1,
        }
        if rank[status] > rank[existing.status]:
            existing.status = status
            existing.error = error

    @staticmethod
    def _dedupe(articles: list[NewsArticle]) -> list[NewsArticle]:
        seen: set[str] = set()
        result: list[NewsArticle] = []
        for article in articles:
            key = article.title.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append(article)
        return sorted(result, key=lambda item: item.published_at, reverse=True)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _alpha_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return value


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

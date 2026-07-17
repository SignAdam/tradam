"""Economic calendar fetching and high-impact event window checks."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from src.news.news_client import ApiResponse, ProviderHealth


HIGH_IMPACT_KEYWORDS = {
    "cpi",
    "ppi",
    "nonfarm",
    "nfp",
    "unemployment",
    "fomc",
    "powell",
    "fed",
    "rate decision",
    "interest rate",
    "gdp",
    "pmi",
    "retail sales",
    "inflation",
    "rba",
    "rbnz",
    "australia employment",
    "new zealand employment",
    "china pmi",
}


@dataclass
class EconomicEvent:
    title: str
    country: str
    event_time: str
    impact: str = "low"
    source: str = "unknown"
    currency: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EconomicCalendar:
    def __init__(self, config: dict[str, Any] | None = None, session: requests.Session | None = None) -> None:
        self.config = config or {}
        self.timeout = int(self.config.get("request_timeout_seconds", 8))
        self.max_age_seconds = int(self.config.get("max_data_age_minutes", 60)) * 60
        self.session = session or requests.Session()
        self.provider_health: list[ProviderHealth] = []
        self._last_success: dict[str, datetime] = {}

    def fetch(self, start: datetime | None = None, end: datetime | None = None) -> list[EconomicEvent]:
        start = start or datetime.now(timezone.utc) - timedelta(days=1)
        end = end or datetime.now(timezone.utc) + timedelta(days=1)
        self.provider_health = []
        events: list[EconomicEvent] = []
        events.extend(self._fmp(start, end))
        events.extend(self._finnhub(start, end))
        return self._dedupe(events)

    def high_impact_events_near(
        self,
        events: list[EconomicEvent],
        now: datetime,
        before_minutes: int,
        after_minutes: int,
        currencies: set[str] | None = None,
    ) -> list[EconomicEvent]:
        currencies = currencies or {"USD"}
        blocked: list[EconomicEvent] = []
        for event in events:
            if event.currency and event.currency.upper() not in currencies:
                continue
            if event.impact != "high":
                continue
            event_time = parse_event_time(event.event_time)
            if not event_time:
                continue
            start = event_time - timedelta(minutes=before_minutes)
            end = event_time + timedelta(minutes=after_minutes)
            if start <= now.astimezone(timezone.utc) <= end:
                blocked.append(event)
        return blocked

    def _fmp(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        requested = datetime.now(timezone.utc).isoformat()
        token = os.getenv("FMP_API_KEY")
        if not token:
            self._health("Financial Modeling Prep calendar", "NOT_CONFIGURED", requested)
            return []
        response = self._get_json(
            "https://financialmodelingprep.com/api/v3/economic_calendar",
            {"from": start.date().isoformat(), "to": end.date().isoformat(), "apikey": token},
        )
        if response.data is None:
            self._failed_health("Financial Modeling Prep calendar", response, requested)
            return []
        data = response.data
        if not isinstance(data, list):
            self._health("Financial Modeling Prep calendar", "UNAVAILABLE", requested, error="Unexpected response shape")
            return []
        events = [
            EconomicEvent(
                title=item.get("event", ""),
                country=item.get("country", ""),
                currency=item.get("currency"),
                event_time=item.get("date", ""),
                impact=classify_impact(item.get("event", ""), item.get("impact")),
                source="Financial Modeling Prep",
                raw=item,
            )
            for item in data
            if item.get("event")
        ]
        self._health("Financial Modeling Prep calendar", "HEALTHY" if events else "EMPTY_VALID_RESPONSE", requested, requested, event_count=len(events), freshness_seconds=0)
        self._last_success["Financial Modeling Prep calendar"] = datetime.now(timezone.utc)
        return events

    def _finnhub(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        requested = datetime.now(timezone.utc).isoformat()
        token = os.getenv("FINNHUB_API_KEY")
        if not token:
            self._health("Finnhub calendar", "NOT_CONFIGURED", requested)
            return []
        response = self._get_json(
            "https://finnhub.io/api/v1/calendar/economic",
            {"from": start.date().isoformat(), "to": end.date().isoformat(), "token": token},
        )
        if response.data is None:
            self._failed_health("Finnhub calendar", response, requested)
            return []
        data = response.data
        rows = data.get("economicCalendar", []) if isinstance(data, dict) else []
        events = [
            EconomicEvent(
                title=item.get("event", ""),
                country=item.get("country", ""),
                currency=item.get("currency"),
                event_time=item.get("time", item.get("date", "")),
                impact=classify_impact(item.get("event", ""), item.get("impact")),
                source="Finnhub",
                raw=item,
            )
            for item in rows
            if item.get("event")
        ]
        self._health("Finnhub calendar", "HEALTHY" if events else "EMPTY_VALID_RESPONSE", requested, requested, event_count=len(events), freshness_seconds=0)
        self._last_success["Finnhub calendar"] = datetime.now(timezone.utc)
        return events

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

    def _failed_health(self, provider: str, response: ApiResponse, requested: str) -> None:
        last_success = self._last_success.get(provider)
        freshness = int((datetime.now(timezone.utc) - last_success).total_seconds()) if last_success else None
        status = "STALE" if freshness is not None and freshness > self.max_age_seconds else response.status
        self._health(
            provider,
            status,
            requested,
            last_success.isoformat() if last_success else None,
            error=response.error,
            freshness_seconds=freshness,
        )

    def _health(
        self,
        provider: str,
        status: str,
        requested: str,
        successful: str | None = None,
        event_count: int = 0,
        error: str | None = None,
        freshness_seconds: int | None = None,
    ) -> None:
        self.provider_health.append(
            ProviderHealth(
                provider=provider,
                status=status,
                last_request_utc=requested,
                last_success_utc=successful,
                event_count=event_count,
                error=error,
                freshness_seconds=freshness_seconds,
            )
        )

    @staticmethod
    def _dedupe(events: list[EconomicEvent]) -> list[EconomicEvent]:
        seen: set[tuple[str, str]] = set()
        deduped: list[EconomicEvent] = []
        for event in events:
            key = (event.title.lower(), event.event_time)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(event)
        return deduped


def classify_impact(title: str, provider_impact: str | None = None) -> str:
    normalized_impact = str(provider_impact or "").lower()
    if normalized_impact in {"high", "medium", "low"}:
        return normalized_impact
    normalized_title = title.lower()
    if any(keyword in normalized_title for keyword in HIGH_IMPACT_KEYWORDS):
        return "high"
    return "medium" if any(word in normalized_title for word in {"usd", "fed", "employment"}) else "low"


def parse_event_time(value: str) -> datetime | None:
    if not value:
        return None
    cleaned = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None

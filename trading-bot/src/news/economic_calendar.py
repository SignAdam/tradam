"""Economic calendar fetching and high-impact event window checks."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests


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
        self.session = session or requests.Session()

    def fetch(self, start: datetime | None = None, end: datetime | None = None) -> list[EconomicEvent]:
        start = start or datetime.now(timezone.utc) - timedelta(days=1)
        end = end or datetime.now(timezone.utc) + timedelta(days=1)
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
        token = os.getenv("FMP_API_KEY")
        if not token:
            return []
        data = self._get_json(
            "https://financialmodelingprep.com/api/v3/economic_calendar",
            {"from": start.date().isoformat(), "to": end.date().isoformat(), "apikey": token},
        )
        if not isinstance(data, list):
            return []
        return [
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

    def _finnhub(self, start: datetime, end: datetime) -> list[EconomicEvent]:
        token = os.getenv("FINNHUB_API_KEY")
        if not token:
            return []
        data = self._get_json(
            "https://finnhub.io/api/v1/calendar/economic",
            {"from": start.date().isoformat(), "to": end.date().isoformat(), "token": token},
        )
        rows = data.get("economicCalendar", []) if isinstance(data, dict) else []
        return [
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

    def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            return None

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


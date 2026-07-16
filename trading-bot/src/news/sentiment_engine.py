"""Lightweight, provider-independent news sentiment scoring."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


POSITIVE_TERMS = {
    "rally",
    "bullish",
    "surge",
    "beats",
    "growth",
    "optimism",
    "easing",
    "dovish",
    "cooling inflation",
    "rate cut",
    "risk-on",
    "approval",
}
NEGATIVE_TERMS = {
    "selloff",
    "bearish",
    "slump",
    "misses",
    "recession",
    "hawkish",
    "hot inflation",
    "rate hike",
    "risk-off",
    "crackdown",
    "lawsuit",
    "war",
    "sanctions",
}
HIGH_IMPACT_TERMS = {
    "cpi",
    "ppi",
    "nfp",
    "payroll",
    "fomc",
    "powell",
    "fed",
    "interest rate",
    "rate decision",
    "inflation",
    "unemployment",
    "gdp",
    "pmi",
    "retail sales",
    "geopolitical",
    "etf approval",
}


@dataclass
class SentimentScore:
    label: str
    score: float
    impact: str
    matched_terms: list[str]


class SentimentEngine:
    def __init__(
        self,
        positive_threshold: float = 0.25,
        negative_threshold: float = -0.25,
    ) -> None:
        self.positive_threshold = positive_threshold
        self.negative_threshold = negative_threshold

    def score_text(self, text: str, provider_score: float | None = None) -> SentimentScore:
        normalized = text.lower()
        positive = [term for term in POSITIVE_TERMS if term in normalized]
        negative = [term for term in NEGATIVE_TERMS if term in normalized]
        high_impact = [term for term in HIGH_IMPACT_TERMS if term in normalized]
        lexicon_score = 0.0
        if positive or negative:
            lexicon_score = (len(positive) - len(negative)) / max(len(positive) + len(negative), 1)
        score = provider_score if provider_score is not None else lexicon_score
        if provider_score is not None and (positive or negative):
            score = (provider_score + lexicon_score) / 2
        label = "neutral"
        if score >= self.positive_threshold:
            label = "bullish"
        elif score <= self.negative_threshold:
            label = "bearish"
        impact = "high" if high_impact else "medium" if re.search(r"\b(fed|usd|bitcoin|dow|gold)\b", normalized) else "low"
        return SentimentScore(label=label, score=round(score, 4), impact=impact, matched_terms=positive + negative + high_impact)

    def aggregate(self, articles: list[dict[str, Any]]) -> dict[str, Any]:
        if not articles:
            return {"sentiment": "neutral", "score": 0.0, "impact": "low", "count": 0}
        scores = [float(article.get("score", 0.0) or 0.0) for article in articles]
        avg = sum(scores) / len(scores)
        high_count = len([article for article in articles if article.get("impact") == "high"])
        if avg >= self.positive_threshold:
            label = "bullish"
        elif avg <= self.negative_threshold:
            label = "bearish"
        else:
            label = "neutral"
        impact = "high" if high_count else "medium" if len(articles) >= 3 else "low"
        return {"sentiment": label, "score": round(avg, 4), "impact": impact, "count": len(articles)}


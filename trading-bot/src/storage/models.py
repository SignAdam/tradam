"""Dataclasses used for persisted trading decisions and analytics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


JsonDict = dict[str, Any]


@dataclass
class SignalDecisionRecord:
    symbol: str
    session: str | None
    direction: str | None
    score: float
    decision: str
    reasons: list[str]
    risk: JsonDict = field(default_factory=dict)
    indicators: JsonDict = field(default_factory=dict)
    news: JsonDict = field(default_factory=dict)
    rejected_reason: str | None = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    raw: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class TradeRecord:
    symbol: str
    session: str | None
    direction: str
    lot: float
    entry_price: float
    stop_loss: float
    take_profit: float
    entry_time: str
    trade_id: str | None = None
    exit_time: str | None = None
    exit_price: float | None = None
    pnl: float | None = None
    duration_seconds: int | None = None
    spread: float | None = None
    timeframe: str | None = None
    h1_trend: str | None = None
    rsi: float | None = None
    ema20: float | None = None
    ema50: float | None = None
    ema200: float | None = None
    atr: float | None = None
    macd: float | None = None
    fibonacci_level: str | None = None
    signal_reason: str | None = None
    news_active: list[JsonDict] = field(default_factory=list)
    sentiment: str | None = None
    status: str = "OPEN"
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass
class NewsRecord:
    symbol_group: str
    title: str
    source: str
    published_at: str
    impact: str = "low"
    sentiment: str = "neutral"
    score: float = 0.0
    url: str | None = None
    raw: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


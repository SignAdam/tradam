"""Dataclasses used for persisted trading decisions and analytics."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from src.utils.identity import new_id, utc_now_iso


JsonDict = dict[str, Any]


@dataclass
class SignalDecisionRecord:
    symbol: str
    session: str | None
    direction: str | None
    score: float
    decision: str
    reasons: list[str]
    run_id: str | None = None
    session_id: str | None = None
    signal_id: str = field(default_factory=lambda: new_id("sig"))
    setup_id: str | None = None
    strategy: str | None = None
    profile: str | None = None
    raw_score: float | None = None
    required_score: float | None = None
    bonuses: list[JsonDict] = field(default_factory=list)
    penalties: list[JsonDict] = field(default_factory=list)
    mode: str = "demo_live"
    source: str = "bot"
    is_fixture: bool = False
    risk: JsonDict = field(default_factory=dict)
    indicators: JsonDict = field(default_factory=dict)
    news: JsonDict = field(default_factory=dict)
    rejected_reason: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    created_at_utc: str = field(default_factory=utc_now_iso)
    updated_at_utc: str = field(default_factory=utc_now_iso)
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
    run_id: str | None = None
    session_id: str | None = None
    signal_id: str | None = None
    internal_trade_id: str = field(default_factory=lambda: new_id("trd"))
    mt5_position_id: str | None = None
    mt5_order_ticket: str | None = None
    mt5_deal_ticket: str | None = None
    parent_position_id: str | None = None
    setup_id: str | None = None
    strategy: str | None = None
    profile: str | None = None
    signal_score: float | None = None
    mode: str = "demo_live"
    source: str = "bot"
    is_fixture: bool = False
    created_at_utc: str = field(default_factory=utc_now_iso)
    updated_at_utc: str = field(default_factory=utc_now_iso)
    exit_time: str | None = None
    exit_price: float | None = None
    pnl: float | None = None
    pnl_gross: float | None = None
    pnl_net: float | None = None
    commission: float = 0.0
    swap: float = 0.0
    duration_seconds: int | None = None
    spread: float | None = None
    spread_price: float | None = None
    spread_points: float | None = None
    estimated_spread_cost: float | None = None
    requested_price: float | None = None
    actual_entry_price: float | None = None
    entry_slippage: float | None = None
    signal_time: str | None = None
    order_time: str | None = None
    execution_time: str | None = None
    initial_volume: float | None = None
    remaining_volume: float | None = None
    initial_stop_loss: float | None = None
    final_stop_loss: float | None = None
    initial_risk_price: float | None = None
    initial_risk_amount: float | None = None
    initial_risk_percent: float | None = None
    tp1: float | None = None
    tp1_close_percent: float | None = None
    tp1_actual_price: float | None = None
    tp1_pnl: float | None = None
    tp1_volume: float | None = None
    tp1_time: str | None = None
    tp2: float | None = None
    tp2_actual_price: float | None = None
    tp2_pnl: float | None = None
    tp2_volume: float | None = None
    tp2_time: str | None = None
    sl_modification_count: int = 0
    break_even_applied: bool = False
    break_even_time: str | None = None
    break_even_price: float | None = None
    trailing_stop_enabled: bool = False
    max_favorable_price: float | None = None
    max_adverse_price: float | None = None
    mfe_price: float | None = None
    mfe_amount: float | None = None
    mfe_r: float | None = None
    mae_price: float | None = None
    mae_amount: float | None = None
    mae_r: float | None = None
    max_unrealized_profit: float | None = None
    max_unrealized_loss: float | None = None
    realized_r: float | None = None
    risk_target_amount: float | None = None
    raw_volume: float | None = None
    rounded_volume: float | None = None
    estimated_loss_after_rounding: float | None = None
    estimated_margin: float | None = None
    order_check: JsonDict = field(default_factory=dict)
    management_state: str = "INITIAL_RISK"
    exit_reason: str | None = None
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
    run_id: str | None = None
    session_id: str | None = None
    mode: str = "demo_live"
    is_fixture: bool = False
    provider_status: str = "UNKNOWN"
    created_at_utc: str = field(default_factory=utc_now_iso)
    updated_at_utc: str = field(default_factory=utc_now_iso)
    impact: str = "low"
    sentiment: str = "unknown"
    score: float = 0.0
    url: str | None = None
    raw: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)

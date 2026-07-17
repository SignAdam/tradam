"""Typed access to per-symbol scalping profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


PRIORITY_SCALPING_PROFILE = "priority"
VALIDATED_SETUP_PROFILE = "validated"


@dataclass(frozen=True)
class SymbolProfile:
    symbol: str
    profile: str
    minimum_score: float
    risk_per_trade_percent: float
    max_risk_per_trade_percent: float
    max_trades_per_session: int
    tp1_volume_percent: float
    tp2_volume_percent: float
    tp1_target_r_min: float
    tp1_target_r_max: float
    tp2_target_r_min: float
    tp2_target_r_max: float
    risk_reduction_after_r: float
    reduced_risk_r: float
    max_trade_duration_minutes: int
    no_progress_timeout_minutes: int
    no_progress_min_r: float
    strategies: tuple[str, ...]
    sessions: tuple[str, ...]
    cooldown: dict[str, int]

    @property
    def is_priority(self) -> bool:
        return self.profile == PRIORITY_SCALPING_PROFILE


def load_symbol_profile(symbol: str, symbols_config: dict[str, Any]) -> SymbolProfile:
    profiles = symbols_config.get("symbols", symbols_config)
    data = profiles[symbol]
    profile = str(data.get("profile", VALIDATED_SETUP_PROFILE))
    if profile not in {PRIORITY_SCALPING_PROFILE, VALIDATED_SETUP_PROFILE}:
        raise ValueError(f"Unsupported profile {profile!r} for {symbol}")
    tp1_min = float(data.get("tp1_target_r_min", 0.6))
    tp2_min = float(data.get("tp2_target_r_min", 1.2))
    return SymbolProfile(
        symbol=symbol,
        profile=profile,
        minimum_score=float(data.get("minimum_score", 7.0)),
        risk_per_trade_percent=float(data.get("risk_per_trade_percent", 0.25)),
        max_risk_per_trade_percent=float(data.get("max_risk_per_trade_percent", 0.35)),
        max_trades_per_session=int(data.get("max_trades_per_session", 3)),
        tp1_volume_percent=float(data.get("tp1_volume_percent", 60.0)),
        tp2_volume_percent=float(data.get("tp2_volume_percent", 40.0)),
        tp1_target_r_min=tp1_min,
        tp1_target_r_max=float(data.get("tp1_target_r_max", max(tp1_min, 0.9))),
        tp2_target_r_min=tp2_min,
        tp2_target_r_max=float(data.get("tp2_target_r_max", max(tp2_min, 1.5))),
        risk_reduction_after_r=float(data.get("risk_reduction_after_r", 0.6)),
        reduced_risk_r=float(data.get("reduced_risk_r", 0.35)),
        max_trade_duration_minutes=int(data.get("max_trade_duration_minutes", 30)),
        no_progress_timeout_minutes=int(data.get("no_progress_timeout_minutes", 10)),
        no_progress_min_r=float(data.get("no_progress_min_r", 0.2)),
        strategies=tuple(data.get("strategies", [])),
        sessions=tuple(data.get("sessions", [])),
        cooldown={key: int(value) for key, value in data.get("cooldown", {}).items()},
    )

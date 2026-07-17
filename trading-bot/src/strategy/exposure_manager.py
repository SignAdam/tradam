"""Underlying exposure controls for correlated logical symbols."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.mt5.symbol_resolver import ResolvedSymbol, normalize_symbol_name


@dataclass
class ExposureDecision:
    allowed: bool
    reason: str | None = None
    group: str | None = None
    preferred_symbol: str | None = None
    conflicting_positions: list[str] = field(default_factory=list)


class UnderlyingExposureManager:
    def __init__(self, symbols_config: dict[str, Any], risk_config: dict[str, Any]) -> None:
        self.symbols = symbols_config.get("symbols", {})
        self.groups = symbols_config.get("underlying_groups", {})
        self.group_limits = risk_config.get("underlying_group_limits", {})

    def select_preferred_symbols(
        self, resolved: dict[str, ResolvedSymbol]
    ) -> tuple[dict[str, ResolvedSymbol], dict[str, str]]:
        selected = dict(resolved)
        suppressed: dict[str, str] = {}
        for group, logical_symbols in self.groups.items():
            candidates = [
                selected[logical]
                for logical in logical_symbols
                if logical in selected and selected[logical].tradable
            ]
            if group != "nasdaq_group" or len(candidates) <= 1:
                continue
            best = min(candidates, key=self._execution_rank)
            for candidate in candidates:
                if candidate.logical_symbol == best.logical_symbol:
                    continue
                same_broker = normalize_symbol_name(candidate.broker_symbol or "") == normalize_symbol_name(best.broker_symbol or "")
                reason = (
                    f"same MT5 symbol as {best.logical_symbol}"
                    if same_broker
                    else f"same Nasdaq underlying; {best.logical_symbol} has better execution conditions"
                )
                suppressed[candidate.logical_symbol] = reason
        return selected, suppressed

    def can_open(
        self,
        logical_symbol: str,
        direction: str,
        open_positions: list[dict[str, Any]],
        resolved: dict[str, ResolvedSymbol],
        proposed_risk_percent: float = 0.0,
    ) -> ExposureDecision:
        group = self.symbols.get(logical_symbol, {}).get("underlying_group")
        if not group:
            return ExposureDecision(True)
        group_logicals = set(self.groups.get(group, [logical_symbol]))
        broker_to_logical = {
            normalize_symbol_name(item.broker_symbol or ""): logical
            for logical, item in resolved.items()
            if item.broker_symbol
        }
        conflicts: list[str] = []
        group_risk = 0.0
        for position in open_positions:
            broker_name = normalize_symbol_name(str(position.get("symbol") or ""))
            existing_logical = position.get("logical_symbol") or broker_to_logical.get(broker_name)
            if existing_logical not in group_logicals:
                continue
            group_risk += float(position.get("initial_risk_percent") or 0.0)
            same_direction = str(position.get("direction") or "").upper() == direction.upper()
            if same_direction or group == "nasdaq_group":
                conflicts.append(str(position.get("ticket") or position.get("mt5_position_id") or existing_logical))
        limit = self.group_limits.get(group, {})
        max_positions = int(limit.get("max_positions", 1))
        max_risk = float(limit.get("max_risk_percent", 100.0))
        if len(conflicts) >= max_positions:
            return ExposureDecision(False, "CORRELATED_POSITION_OPEN", group, conflicting_positions=conflicts)
        if group_risk + proposed_risk_percent > max_risk + 1e-12:
            return ExposureDecision(False, "RISK_LIMIT_REACHED", group, conflicting_positions=conflicts)
        return ExposureDecision(True, group=group)

    @staticmethod
    def _execution_rank(symbol: ResolvedSymbol) -> tuple[float, float, float]:
        spread = symbol.spread_points if symbol.spread_points is not None else float("inf")
        tick_size = symbol.tick_size if symbol.tick_size and symbol.tick_size > 0 else float("inf")
        return spread, tick_size, -symbol.resolution_score

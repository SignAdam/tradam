"""Broker-aware symbol discovery and execution-quality selection."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from src.mt5.connection import _as_dict, mt5


def normalize_symbol_name(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


@dataclass
class ResolvedSymbol:
    logical_symbol: str
    broker_symbol: str | None
    matched_alias: str | None
    status: str
    asset_class: str | None = None
    profile: str | None = None
    underlying_group: str | None = None
    digits: int | None = None
    point: float | None = None
    tick_size: float | None = None
    tick_value: float | None = None
    trade_contract_size: float | None = None
    volume_min: float | None = None
    volume_max: float | None = None
    volume_step: float | None = None
    stops_level: int | None = None
    spread_points: float | None = None
    trade_mode: int | None = None
    quote_sessions: list[dict[str, Any]] = field(default_factory=list)
    visible: bool = False
    tradable: bool = False
    selected: bool = False
    resolution_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SymbolResolver:
    """Resolve configured logical assets against the symbols exposed by MT5."""

    def __init__(self, symbols_config: dict[str, Any], broker_api: Any | None = None) -> None:
        self.config = symbols_config.get("symbols", symbols_config)
        self.broker = broker_api or mt5

    def resolve_all(self) -> dict[str, ResolvedSymbol]:
        if self.broker is None:
            raise RuntimeError("MetaTrader5 package is unavailable")
        available = list(self.broker.symbols_get() or [])
        names = [str(_as_dict(item).get("name") or getattr(item, "name", "")) for item in available]
        resolved: dict[str, ResolvedSymbol] = {}
        ordered = sorted(
            self.config.items(),
            key=lambda item: int(item[1].get("priority", 999)),
        )
        for logical, profile in ordered:
            if profile.get("enabled", True):
                resolved[logical] = self.resolve_one(logical, names)
        return resolved

    def resolve_one(self, logical: str, available_symbols: list[str] | None = None) -> ResolvedSymbol:
        if self.broker is None:
            raise RuntimeError("MetaTrader5 package is unavailable")
        profile = self.config.get(logical, {})
        if available_symbols is None:
            available_symbols = [
                str(_as_dict(item).get("name") or getattr(item, "name", ""))
                for item in list(self.broker.symbols_get() or [])
            ]
        aliases = list(dict.fromkeys([*profile.get("aliases", []), logical]))
        candidates = self._candidate_names(aliases, available_symbols)
        best: tuple[float, str, str, dict[str, Any], dict[str, Any]] | None = None
        for name, alias, name_score in candidates:
            info = _as_dict(self.broker.symbol_info(name))
            tick = _as_dict(self.broker.symbol_info_tick(name))
            if not info:
                continue
            trade_mode = int(info.get("trade_mode", 0) or 0)
            disabled = getattr(self.broker, "SYMBOL_TRADE_MODE_DISABLED", 0)
            tradable = trade_mode != disabled
            bid = float(tick.get("bid") or 0.0)
            ask = float(tick.get("ask") or 0.0)
            point = float(info.get("point") or 0.0)
            spread = (ask - bid) / point if point > 0 and ask > 0 and bid > 0 else float("inf")
            quality = name_score + (20.0 if tradable else -100.0) + (5.0 if info.get("visible") else 0.0)
            quality -= min(spread if spread != float("inf") else 10_000.0, 10_000.0) / 10_000.0
            if best is None or quality > best[0]:
                best = (quality, name, alias, info, tick)

        if best is None:
            return ResolvedSymbol(
                logical_symbol=logical,
                broker_symbol=None,
                matched_alias=None,
                status="SYMBOL_NOT_FOUND",
                asset_class=profile.get("asset_class"),
                profile=profile.get("profile"),
                underlying_group=profile.get("underlying_group"),
            )

        quality, name, alias, info, tick = best
        selected = bool(self.broker.symbol_select(name, True))
        refreshed_info = _as_dict(self.broker.symbol_info(name)) or info
        refreshed_tick = _as_dict(self.broker.symbol_info_tick(name)) or tick
        trade_mode = int(refreshed_info.get("trade_mode", 0) or 0)
        disabled = getattr(self.broker, "SYMBOL_TRADE_MODE_DISABLED", 0)
        bid = float(refreshed_tick.get("bid") or 0.0)
        ask = float(refreshed_tick.get("ask") or 0.0)
        point = float(refreshed_info.get("point") or 0.0)
        spread = (ask - bid) / point if point > 0 and ask > 0 and bid > 0 else None
        tradable = selected and trade_mode != disabled and bid > 0 and ask > 0
        return ResolvedSymbol(
            logical_symbol=logical,
            broker_symbol=name,
            matched_alias=alias,
            status="RESOLVED" if tradable else "SYMBOL_NOT_TRADABLE",
            asset_class=profile.get("asset_class"),
            profile=profile.get("profile"),
            underlying_group=profile.get("underlying_group"),
            digits=_int_or_none(refreshed_info.get("digits")),
            point=_float_or_none(refreshed_info.get("point")),
            tick_size=_float_or_none(refreshed_info.get("trade_tick_size")),
            tick_value=_float_or_none(refreshed_info.get("trade_tick_value")),
            trade_contract_size=_float_or_none(refreshed_info.get("trade_contract_size")),
            volume_min=_float_or_none(refreshed_info.get("volume_min")),
            volume_max=_float_or_none(refreshed_info.get("volume_max")),
            volume_step=_float_or_none(refreshed_info.get("volume_step")),
            stops_level=_int_or_none(refreshed_info.get("trade_stops_level")),
            spread_points=spread,
            trade_mode=trade_mode,
            quote_sessions=self._quote_sessions(name),
            visible=bool(refreshed_info.get("visible", False)),
            tradable=tradable,
            selected=selected,
            resolution_score=round(quality, 6),
        )

    @staticmethod
    def _candidate_names(aliases: list[str], available: list[str]) -> list[tuple[str, str, float]]:
        candidates: dict[str, tuple[str, float]] = {}
        for alias_index, alias in enumerate(aliases):
            alias_normalized = normalize_symbol_name(alias)
            if not alias_normalized:
                continue
            for broker_name in available:
                normalized = normalize_symbol_name(broker_name)
                score = 0.0
                if broker_name.upper() == alias.upper():
                    score = 100.0
                elif normalized == alias_normalized:
                    score = 95.0
                elif normalized.startswith(alias_normalized):
                    score = 80.0
                elif alias_normalized in normalized:
                    score = 65.0
                if score <= 0:
                    continue
                score -= alias_index * 0.5
                previous = candidates.get(broker_name)
                if previous is None or score > previous[1]:
                    candidates[broker_name] = (alias, score)
        return [(name, alias, score) for name, (alias, score) in candidates.items()]

    def _quote_sessions(self, symbol: str) -> list[dict[str, Any]]:
        getter = getattr(self.broker, "symbol_info_session_quote", None)
        if getter is None:
            return []
        sessions: list[dict[str, Any]] = []
        for weekday in range(7):
            for index in range(10):
                try:
                    value = getter(symbol, weekday, index)
                except (TypeError, RuntimeError):
                    return sessions
                if not value:
                    break
                sessions.append({"weekday": weekday, "index": index, "value": str(value)})
        return sessions


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None

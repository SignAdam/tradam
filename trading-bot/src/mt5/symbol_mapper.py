"""Map desired logical symbols to the broker-specific MT5 symbol names."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolMapping:
    logical: str
    broker_symbol: str | None
    matched_alias: str | None
    status: str


def _normalize(value: str) -> str:
    return "".join(ch for ch in value.upper() if ch.isalnum())


def unique_aliases(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = _normalize(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(value)
    return result


class SymbolMapper:
    def __init__(self, symbols_config: dict) -> None:
        self.symbols_config = symbols_config.get("symbols", symbols_config)

    def map_symbol(self, logical: str, available_symbols: list[str]) -> SymbolMapping:
        config = self.symbols_config.get(logical, {})
        aliases = unique_aliases([*config.get("aliases", []), logical])
        available_by_norm = {_normalize(symbol): symbol for symbol in available_symbols}

        for alias in aliases:
            normalized = _normalize(alias)
            if normalized in available_by_norm:
                return SymbolMapping(logical, available_by_norm[normalized], alias, "matched_exact")

        for alias in aliases:
            normalized_alias = _normalize(alias)
            for normalized_symbol, broker_symbol in available_by_norm.items():
                if normalized_symbol.startswith(normalized_alias) or normalized_alias in normalized_symbol:
                    return SymbolMapping(logical, broker_symbol, alias, "matched_fuzzy")

        return SymbolMapping(logical, None, None, "missing")

    def resolve_all(self, available_symbols: list[str]) -> dict[str, SymbolMapping]:
        mappings: dict[str, SymbolMapping] = {}
        for logical, config in self.symbols_config.items():
            if config.get("enabled", True):
                mappings[logical] = self.map_symbol(logical, available_symbols)
        return mappings

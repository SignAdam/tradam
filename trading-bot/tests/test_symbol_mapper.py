from __future__ import annotations

from src.mt5.symbol_mapper import SymbolMapper


def test_symbol_mapper_prefers_configured_aliases_before_logical_name() -> None:
    mapper = SymbolMapper(
        {
            "symbols": {
                "BTC": {
                    "enabled": True,
                    "aliases": ["BTCUSD", "BTCUSDT", "BTC"],
                }
            }
        }
    )

    mapping = mapper.map_symbol("BTC", ["BTC", "BTCUSDT"])

    assert mapping.broker_symbol == "BTCUSDT"
    assert mapping.matched_alias == "BTCUSDT"


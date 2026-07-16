from __future__ import annotations

from src.mt5.order_manager import OrderManager, OrderRequest


def test_order_validation_rejects_bad_buy_geometry() -> None:
    manager = OrderManager(
        market_data=None,
        trading_config={"mode": "paper"},
        risk_config={"execution": {"reject_if_spread_above_symbol_limit": True}},
        symbols_config={"symbols": {"XAUUSD": {"aliases": ["XAUUSD"], "max_spread_points": 50}}},
    )
    result = manager.validate_order(
        OrderRequest("XAUUSD", "BUY", 0.1, 100, 101, 102, 20),
        symbol_info={"point": 0.01, "volume_min": 0.01, "volume_max": 1, "volume_step": 0.01, "trade_stops_level": 5},
        tick={"spread_points": 10},
    )
    assert not result.ok
    assert any("BUY order" in reason for reason in result.reasons)


def test_order_validation_accepts_valid_paper_order() -> None:
    manager = OrderManager(
        market_data=None,
        trading_config={"mode": "paper"},
        risk_config={"execution": {"reject_if_spread_above_symbol_limit": True}},
        symbols_config={"symbols": {"XAUUSD": {"aliases": ["XAUUSD"], "max_spread_points": 50}}},
    )
    result = manager.validate_order(
        OrderRequest("XAUUSD", "BUY", 0.1, 100, 99, 102, 20),
        symbol_info={"point": 0.01, "volume_min": 0.01, "volume_max": 1, "volume_step": 0.01, "trade_stops_level": 5},
        tick={"spread_points": 10},
    )
    assert result.ok


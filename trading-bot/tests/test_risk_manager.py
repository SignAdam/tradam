from __future__ import annotations

from src.strategy.risk_manager import RiskManager, RiskState, SymbolTradingSpec


RISK_CONFIG = {
    "risk": {
        "risk_per_trade_percent": 0.5,
        "max_risk_per_trade_percent": 0.5,
        "min_risk_reward": 1.4,
        "max_trades_per_session": 6,
        "max_consecutive_losses": 3,
        "max_drawdown_percent": 6.0,
        "max_session_loss_percent": 1.5,
        "max_daily_loss_percent": 3.0,
        "allow_martingale": False,
        "allow_grid": False,
        "allow_loss_recovery": False,
        "increase_lot_after_loss": False,
        "require_stop_loss": True,
        "require_take_profit": True,
    },
    "position_sizing": {"volume_rounding": "floor"},
}


def test_position_size_uses_equity_risk_and_stop_distance() -> None:
    manager = RiskManager(RISK_CONFIG)
    spec = SymbolTradingSpec(tick_value=1.0, tick_size=0.01, volume_min=0.01, volume_max=10, volume_step=0.01)
    lot = manager.calculate_position_size(10_000, entry_price=100, stop_loss=99, symbol_spec=spec)
    assert lot == 0.5


def test_risk_manager_rejects_low_risk_reward() -> None:
    manager = RiskManager(RISK_CONFIG)
    spec = SymbolTradingSpec(tick_value=1.0, tick_size=0.01, volume_min=0.01, volume_max=10, volume_step=0.01)
    check = manager.validate_trade(
        RiskState(equity=10_000),
        "BUY",
        lot=0.5,
        entry_price=100,
        stop_loss=99,
        take_profit=101,
        symbol_spec=spec,
    )
    assert not check.ok
    assert any("Risk/reward" in reason for reason in check.reasons)


def test_demo_live_sizing_rejects_missing_broker_tick_values() -> None:
    manager = RiskManager(RISK_CONFIG)
    spec = SymbolTradingSpec(tick_value=0.0, tick_size=0.0, volume_min=0.01, volume_max=1, volume_step=0.01)
    try:
        manager.calculate_position_size_with_broker(
            broker_api=object(),
            symbol="XAUUSD",
            direction="SELL",
            equity=10_000,
            entry_price=3997.4,
            stop_loss=4006.02,
            symbol_spec=spec,
        )
    except ValueError as exc:
        assert "tick_size" in str(exc)
        assert "tick_value" in str(exc)
    else:
        raise AssertionError("Missing broker tick values must be rejected")

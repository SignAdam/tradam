from __future__ import annotations

from src.strategy.position_manager import ManagedPosition, PositionManager, PositionManagerConfig


class EventSink:
    def __init__(self) -> None:
        self.events = []

    def log_position_event(self, event):
        self.events.append(event)


class RejectingBroker:
    TRADE_RETCODE_DONE = 10009
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_DEAL = 1
    ORDER_TYPE_BUY = 0

    def __init__(self) -> None:
        self.calls = 0

    def order_send(self, payload):
        self.calls += 1
        if payload.get("action") == self.TRADE_ACTION_DEAL:
            return {"retcode": self.TRADE_RETCODE_DONE, "comment": "Done"}
        return {"retcode": 10016, "comment": "Invalid stops"}


def test_sell_break_even_threshold_requires_correct_price_touch() -> None:
    sink = EventSink()
    manager = PositionManager(None, sink, PositionManagerConfig(tp1_r=0.8, tp2_r=1.3, risk_reduction_r=0.4))
    position = ManagedPosition(
        internal_trade_id="sell_3997",
        symbol="XAUUSD",
        direction="SELL",
        volume_initial=0.29,
        volume_remaining=0.29,
        entry_price=3997.40,
        stop_loss=4006.02,
        tp1=3990.50,
        tp2=3986.00,
    )

    manager.evaluate_tick(position, bid=3991.0, ask=3991.1, symbol_info={"point": 0.01, "volume_min": 0.01, "volume_step": 0.01})

    assert not any(event["event_type"].startswith("BREAK_EVEN") for event in sink.events)


def test_sell_break_even_rejection_is_logged_with_retcode() -> None:
    sink = EventSink()
    manager = PositionManager(RejectingBroker(), sink, PositionManagerConfig(tp1_r=0.8, tp2_r=1.3, risk_reduction_r=9.0))
    position = ManagedPosition(
        internal_trade_id="sell_3997",
        mt5_position_id="123",
        symbol="XAUUSD",
        direction="SELL",
        volume_initial=0.29,
        volume_remaining=0.29,
        entry_price=3997.40,
        stop_loss=4006.02,
        tp1=3990.50,
        tp2=3986.00,
    )

    manager.evaluate_tick(position, bid=3990.4, ask=3990.5, symbol_info={"point": 0.01, "volume_min": 0.01, "volume_step": 0.01})

    assert any(event["event_type"] == "BREAK_EVEN_REJECTED" for event in sink.events)
    assert any(event.get("mt5_retcode") == 10016 and event.get("error_message") == "Invalid stops" for event in sink.events)

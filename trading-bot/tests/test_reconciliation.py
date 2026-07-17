from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from src.mt5.reconciliation import BrokerReconciliationService
from src.storage.database import Database


@dataclass
class FakeDeal:
    ticket: int
    order: int
    position_id: int
    symbol: str
    type: int
    volume: float
    price: float
    profit: float
    commission: float
    swap: float
    time: int
    entry: int = 0


@dataclass
class FakeOrder:
    ticket: int
    position_id: int
    symbol: str
    volume_initial: float
    volume_current: float
    price_open: float
    sl: float
    tp: float
    time_setup: int


class FakeBroker:
    DEAL_TYPE_BUY = 0
    DEAL_TYPE_SELL = 1
    COPY_TICKS_ALL = 0

    def history_deals_get(self, start, end):
        return [
            FakeDeal(10, 100, 500, "XAUUSD", self.DEAL_TYPE_SELL, 0.29, 3997.40, 0, 0, 0, 1784224879),
            FakeDeal(11, 101, 500, "XAUUSD", self.DEAL_TYPE_BUY, 0.29, 4006.02, -218.44, 0, 0, 1784226167),
        ]

    def history_orders_get(self, start, end):
        return [FakeOrder(100, 500, "XAUUSD", 0.29, 0, 3997.40, 4006.02, 3984.67, 1784224879)]

    def positions_get(self):
        return []

    def symbol_info(self, symbol):
        return {"trade_tick_size": 0.01, "trade_tick_value": 1.0, "point": 0.01}

    def copy_ticks_range(self, symbol, start, end, flags):
        return [
            {"bid": 3996.0, "ask": 3996.2},
            {"bid": 3991.0, "ask": 3991.2},
            {"bid": 4005.8, "ask": 4006.02},
        ]


def test_mt5_deal_is_imported_only_once_and_trade_appears(tmp_path) -> None:
    db = Database(tmp_path / "bot.sqlite")
    db.initialize()
    service = BrokerReconciliationService(db, broker_api=FakeBroker())
    start = datetime(2026, 7, 16, tzinfo=timezone.utc)
    end = datetime(2026, 7, 17, tzinfo=timezone.utc)
    service.reconcile_history(start, end, session_name="US")
    service.reconcile_history(start, end, session_name="US")

    deals = db.connection.execute("SELECT COUNT(*) AS count FROM mt5_deals").fetchone()["count"]
    trades = db.fetch_trades_between("2026-07-16T00:00:00+00:00", "2026-07-17T00:00:00+00:00", mode="demo_live")

    assert deals == 2
    assert len(trades) == 1
    assert trades[0]["symbol"] == "XAUUSD"
    assert trades[0]["pnl_net"] == -218.44
    assert trades[0]["mfe_r"] is not None
    db.close()


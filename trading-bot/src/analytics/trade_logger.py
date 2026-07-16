"""Logging bridge that writes decisions, trades, and news to SQLite and log files."""

from __future__ import annotations

import logging
from typing import Any

from src.storage.database import Database
from src.storage.models import NewsRecord, SignalDecisionRecord, TradeRecord


class TradeLogger:
    def __init__(
        self,
        database: Database,
        signal_logger: logging.Logger | None = None,
        order_logger: logging.Logger | None = None,
        news_logger: logging.Logger | None = None,
    ) -> None:
        self.database = database
        self.signal_logger = signal_logger or logging.getLogger("tradam.signals")
        self.order_logger = order_logger or logging.getLogger("tradam.orders")
        self.news_logger = news_logger or logging.getLogger("tradam.news")

    def log_decision(self, decision: SignalDecisionRecord | dict[str, Any]) -> int:
        row_id = self.database.insert_decision(decision)
        data = decision.to_dict() if isinstance(decision, SignalDecisionRecord) else decision
        self.signal_logger.info(
            "decision=%s symbol=%s direction=%s score=%s reasons=%s",
            data.get("decision"),
            data.get("symbol"),
            data.get("direction"),
            data.get("score"),
            "; ".join(data.get("reasons", [])),
        )
        return row_id

    def log_trade(self, trade: TradeRecord | dict[str, Any]) -> int:
        row_id = self.database.insert_trade(trade)
        data = trade.to_dict() if isinstance(trade, TradeRecord) else trade
        self.order_logger.info(
            "trade status=%s symbol=%s direction=%s lot=%s entry=%s sl=%s tp=%s pnl=%s",
            data.get("status"),
            data.get("symbol"),
            data.get("direction"),
            data.get("lot"),
            data.get("entry_price"),
            data.get("stop_loss"),
            data.get("take_profit"),
            data.get("pnl"),
        )
        return row_id

    def log_news(self, items: list[NewsRecord | dict[str, Any]]) -> list[int]:
        ids = self.database.executemany_news(items)
        for item in items:
            data = item.to_dict() if isinstance(item, NewsRecord) else item
            self.news_logger.info(
                "news symbol=%s impact=%s sentiment=%s source=%s title=%s",
                data.get("symbol_group"),
                data.get("impact"),
                data.get("sentiment"),
                data.get("source"),
                data.get("title"),
            )
        return ids


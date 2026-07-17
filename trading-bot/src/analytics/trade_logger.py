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
        context: dict[str, Any] | None = None,
    ) -> None:
        self.database = database
        self.signal_logger = signal_logger or logging.getLogger("tradam.signals")
        self.order_logger = order_logger or logging.getLogger("tradam.orders")
        self.news_logger = news_logger or logging.getLogger("tradam.news")
        self.context = context or {}

    def set_context(self, **context: Any) -> None:
        self.context.update({key: value for key, value in context.items() if value is not None})

    def log_decision(self, decision: SignalDecisionRecord | dict[str, Any]) -> int:
        data = decision.to_dict() if isinstance(decision, SignalDecisionRecord) else dict(decision)
        data = self._apply_context(data)
        row_id = self.database.insert_decision(data)
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
        data = trade.to_dict() if isinstance(trade, TradeRecord) else dict(trade)
        data = self._apply_context(data)
        row_id = self.database.insert_trade(data)
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
        normalized = [
            self._apply_context(item.to_dict() if isinstance(item, NewsRecord) else dict(item))
            for item in items
        ]
        ids = self.database.executemany_news(normalized)
        for data in normalized:
            self.news_logger.info(
                "news symbol=%s impact=%s sentiment=%s source=%s title=%s",
                data.get("symbol_group"),
                data.get("impact"),
                data.get("sentiment"),
                data.get("source"),
                data.get("title"),
            )
        return ids

    def log_position_event(self, event: dict[str, Any]) -> int:
        data = self._apply_context(dict(event))
        row_id = self.database.insert_position_event(data)
        self.order_logger.info(
            "position_event=%s position=%s trade=%s r=%s retcode=%s error=%s",
            data.get("event_type"),
            data.get("mt5_position_id"),
            data.get("internal_trade_id"),
            data.get("current_r"),
            data.get("mt5_retcode"),
            data.get("error_message"),
        )
        return row_id

    def _apply_context(self, data: dict[str, Any]) -> dict[str, Any]:
        for key in ("run_id", "session_id", "mode", "source"):
            if data.get(key) is None and self.context.get(key) is not None:
                data[key] = self.context[key]
        return data

"""Reconcile local storage with MetaTrader 5 positions, orders, and deals."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from src.analytics.excursions import calculate_excursions
from src.mt5.connection import mt5
from src.storage.database import Database
from src.storage.models import TradeRecord
from src.utils.identity import new_id, utc_now_iso


@dataclass
class ReconciliationResult:
    imported_deals: int = 0
    imported_trades: int = 0
    updated_trades: int = 0
    divergences: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class BrokerReconciliationService:
    def __init__(
        self,
        database: Database,
        mode: str = "demo_live",
        broker_api: Any | None = None,
    ) -> None:
        self.database = database
        self.mode = mode
        self.broker_api = broker_api or mt5

    def reconcile_history(
        self,
        start_utc: datetime,
        end_utc: datetime,
        run_id: str | None = None,
        session_id: str | None = None,
        session_name: str | None = None,
    ) -> ReconciliationResult:
        result = ReconciliationResult()
        if self.broker_api is None:
            result.errors.append("MetaTrader5 package is unavailable")
            return result

        deals = self._history_deals(start_utc, end_utc)
        orders = [normalize_order(order) for order in self._history_orders(start_utc, end_utc)]
        orders_by_position: dict[str, list[dict[str, Any]]] = {}
        for order in orders:
            if order.get("mt5_position_id"):
                orders_by_position.setdefault(str(order["mt5_position_id"]), []).append(order)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for deal in deals:
            normalized = normalize_deal(deal)
            if not normalized.get("mt5_deal_ticket"):
                continue
            self.database.insert_mt5_deal(
                {
                    **normalized,
                    "run_id": run_id,
                    "session_id": session_id,
                    "mode": self.mode,
                }
            )
            result.imported_deals += 1
            position_key = normalized.get("mt5_position_id") or normalized["mt5_deal_ticket"]
            grouped.setdefault(str(position_key), []).append(normalized)

        for position_id, position_deals in grouped.items():
            trade = trade_from_deals(
                position_id,
                position_deals,
                orders=orders_by_position.get(position_id, []),
                ticks=self._ticks_for_deals(position_deals),
                symbol_info=self._symbol_info(position_deals[0].get("symbol")),
                run_id=run_id,
                session_id=session_id,
                session_name=session_name,
                mode=self.mode,
            )
            if trade:
                self.database.insert_trade(trade)
                result.imported_trades += 1
        return result

    def reconcile_open_positions(
        self,
        run_id: str | None = None,
        session_id: str | None = None,
    ) -> ReconciliationResult:
        result = ReconciliationResult()
        if self.broker_api is None:
            result.errors.append("MetaTrader5 package is unavailable")
            return result
        positions = self._positions()
        for position in positions:
            row = normalize_position(position)
            self.database.insert_position_event(
                {
                    "run_id": run_id,
                    "session_id": session_id,
                    "mt5_position_id": row.get("mt5_position_id"),
                    "event_type": "BROKER_RECONCILED_OPEN_POSITION",
                    "timestamp_utc": utc_now_iso(),
                    "unrealized_profit": row.get("profit"),
                    "volume": row.get("volume"),
                    "payload": row,
                }
            )
        return result

    def full_session_reconciliation(
        self,
        start_utc: datetime,
        end_utc: datetime,
        run_id: str | None = None,
        session_id: str | None = None,
        session_name: str | None = None,
    ) -> ReconciliationResult:
        history = self.reconcile_history(start_utc, end_utc, run_id, session_id, session_name)
        open_positions = self.reconcile_open_positions(run_id, session_id)
        history.errors.extend(open_positions.errors)
        history.divergences.extend(open_positions.divergences)
        return history

    def _history_deals(self, start_utc: datetime, end_utc: datetime) -> list[Any]:
        deals = self.broker_api.history_deals_get(start_utc, end_utc)
        return list(deals or [])

    def _history_orders(self, start_utc: datetime, end_utc: datetime) -> list[Any]:
        if not hasattr(self.broker_api, "history_orders_get"):
            return []
        orders = self.broker_api.history_orders_get(start_utc, end_utc)
        return list(orders or [])

    def _positions(self) -> list[Any]:
        positions = self.broker_api.positions_get()
        return list(positions or [])

    def _symbol_info(self, symbol: str | None) -> dict[str, Any]:
        if not symbol or not hasattr(self.broker_api, "symbol_info"):
            return {}
        info = self.broker_api.symbol_info(symbol)
        return _as_dict(info)

    def _ticks_for_deals(self, deals: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not deals or not hasattr(self.broker_api, "copy_ticks_range"):
            return []
        first = deals[0]
        last = deals[-1]
        symbol = first.get("symbol")
        start = _parse_time(first.get("time_utc"))
        end = _parse_time(last.get("time_utc"))
        if not symbol or not start or not end or start == end:
            return []
        flags = getattr(self.broker_api, "COPY_TICKS_ALL", 0)
        ticks = self.broker_api.copy_ticks_range(symbol, start, end, flags)
        return [_as_dict(tick) for tick in list(ticks or [])]


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "_asdict"):
        return dict(value._asdict())
    if isinstance(value, dict):
        return dict(value)
    return {name: getattr(value, name) for name in dir(value) if not name.startswith("_")}


def normalize_deal(deal: Any) -> dict[str, Any]:
    row = _as_dict(deal)
    timestamp = row.get("time")
    time_utc = None
    if timestamp is not None:
        time_utc = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()
    direction = direction_from_deal_type(row.get("type"))
    return {
        "mt5_deal_ticket": str(row.get("ticket")) if row.get("ticket") is not None else None,
        "mt5_order_ticket": str(row.get("order")) if row.get("order") is not None else None,
        "mt5_position_id": str(row.get("position_id")) if row.get("position_id") is not None else None,
        "symbol": row.get("symbol"),
        "direction": direction,
        "volume": row.get("volume"),
        "price": row.get("price"),
        "profit": row.get("profit", 0.0),
        "commission": row.get("commission", 0.0),
        "swap": row.get("swap", 0.0),
        "time_utc": time_utc,
        "entry": row.get("entry"),
        "raw": row,
    }


def normalize_position(position: Any) -> dict[str, Any]:
    row = _as_dict(position)
    return {
        "mt5_position_id": str(row.get("ticket")) if row.get("ticket") is not None else None,
        "symbol": row.get("symbol"),
        "volume": row.get("volume"),
        "price_open": row.get("price_open"),
        "sl": row.get("sl"),
        "tp": row.get("tp"),
        "profit": row.get("profit"),
        "raw": row,
    }


def normalize_order(order: Any) -> dict[str, Any]:
    row = _as_dict(order)
    timestamp = row.get("time_setup") or row.get("time_done")
    time_utc = None
    if timestamp is not None:
        time_utc = datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat()
    return {
        "mt5_order_ticket": str(row.get("ticket")) if row.get("ticket") is not None else None,
        "mt5_position_id": str(row.get("position_id")) if row.get("position_id") is not None else None,
        "symbol": row.get("symbol"),
        "volume_initial": row.get("volume_initial"),
        "volume_current": row.get("volume_current"),
        "price_open": row.get("price_open"),
        "sl": row.get("sl"),
        "tp": row.get("tp"),
        "time_setup_utc": time_utc,
        "raw": row,
    }


def direction_from_deal_type(deal_type: Any) -> str:
    try:
        value = int(deal_type)
    except (TypeError, ValueError):
        return "UNKNOWN"
    if mt5 is not None:
        if value == getattr(mt5, "DEAL_TYPE_BUY", -999):
            return "BUY"
        if value == getattr(mt5, "DEAL_TYPE_SELL", -999):
            return "SELL"
    return "BUY" if value == 0 else "SELL" if value == 1 else "UNKNOWN"


def trade_from_deals(
    position_id: str,
    deals: list[dict[str, Any]],
    orders: list[dict[str, Any]] | None,
    ticks: list[dict[str, Any]] | None,
    symbol_info: dict[str, Any] | None,
    run_id: str | None,
    session_id: str | None,
    session_name: str | None,
    mode: str,
) -> dict[str, Any] | None:
    sorted_deals = sorted(deals, key=lambda item: item.get("time_utc") or "")
    if not sorted_deals:
        return None
    open_deal = sorted_deals[0]
    close_deal = sorted_deals[-1] if len(sorted_deals) > 1 else None
    orders = orders or []
    symbol_info = symbol_info or {}
    order_sl = first_nonzero([order.get("sl") for order in orders])
    order_tp = first_nonzero([order.get("tp") for order in orders])
    entry_price = float(open_deal.get("price") or 0.0)
    exit_price = float(close_deal.get("price") or 0.0) if close_deal else None
    pnl_gross = sum(float(item.get("profit") or 0.0) for item in sorted_deals)
    commission = sum(float(item.get("commission") or 0.0) for item in sorted_deals)
    swap = sum(float(item.get("swap") or 0.0) for item in sorted_deals)
    pnl_net = pnl_gross + commission + swap
    direction = open_deal.get("direction") or "UNKNOWN"
    volume = float(open_deal.get("volume") or 0.0)
    stop_loss = float(order_sl or 0.0)
    take_profit = float(order_tp or 0.0)
    tick_size = float(symbol_info.get("trade_tick_size") or symbol_info.get("point") or 0.01)
    tick_value = float(symbol_info.get("trade_tick_value") or 1.0)
    excursions = calculate_excursions(
        direction,
        entry_price,
        stop_loss or None,
        volume,
        ticks or [],
        tick_size=tick_size,
        tick_value=tick_value,
    )
    realized_r = None
    if stop_loss:
        risk_amount = abs(entry_price - stop_loss) * (tick_value / tick_size) * volume
        realized_r = pnl_net / risk_amount if risk_amount else None
    internal_trade_id = f"mt5_position_{position_id}"
    status = "CLOSED" if close_deal and close_deal.get("time_utc") != open_deal.get("time_utc") else "OPEN"
    return TradeRecord(
        run_id=run_id,
        session_id=session_id,
        internal_trade_id=internal_trade_id,
        mt5_position_id=position_id,
        mt5_order_ticket=open_deal.get("mt5_order_ticket"),
        mt5_deal_ticket=open_deal.get("mt5_deal_ticket"),
        mode=mode,
        source="mt5_reconciliation",
        symbol=open_deal.get("symbol") or "UNKNOWN",
        session=session_name,
        direction=direction,
        lot=volume,
        initial_volume=volume,
        remaining_volume=0.0 if status == "CLOSED" else volume,
        entry_price=entry_price,
        actual_entry_price=entry_price,
        requested_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        initial_stop_loss=stop_loss,
        final_stop_loss=stop_loss,
        entry_time=open_deal.get("time_utc") or utc_now_iso(),
        execution_time=open_deal.get("time_utc"),
        exit_time=close_deal.get("time_utc") if close_deal else None,
        exit_price=exit_price,
        pnl=pnl_net,
        pnl_gross=pnl_gross,
        pnl_net=pnl_net,
        commission=commission,
        swap=swap,
        status=status,
        exit_reason="BROKER_HISTORY",
        mfe_price=excursions["mfe_price"],
        mfe_amount=excursions["mfe_amount"],
        mfe_r=excursions["mfe_r"],
        mae_price=excursions["mae_price"],
        mae_amount=excursions["mae_amount"],
        mae_r=excursions["mae_r"],
        max_favorable_price=excursions["max_favorable_price"],
        max_adverse_price=excursions["max_adverse_price"],
        max_unrealized_profit=excursions["max_unrealized_profit"],
        max_unrealized_loss=excursions["max_unrealized_loss"],
        realized_r=realized_r,
        metadata={"deals": sorted_deals, "reconciled_at_utc": utc_now_iso(), "synthetic_id": new_id("rec")},
    ).to_dict()


def first_nonzero(values: list[Any]) -> Any:
    for value in values:
        try:
            if value is not None and float(value) != 0.0:
                return value
        except (TypeError, ValueError):
            continue
    return None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

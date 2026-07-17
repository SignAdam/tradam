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
    duplicate_deals: int = 0
    imported_orders: int = 0
    imported_trades: int = 0
    updated_trades: int = 0
    synchronized_positions: int = 0
    divergences: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class BrokerReconciliationService:
    def __init__(
        self,
        database: Database,
        mode: str = "demo_live",
        broker_api: Any | None = None,
    ) -> None:
        if mode != "demo_live":
            raise ValueError("Broker reconciliation is demo_live only")
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
            if order.get("mt5_order_ticket"):
                order_present = self.database.mt5_order_exists(order["mt5_order_ticket"])
                self.database.insert_mt5_order(
                    {**order, "run_id": run_id, "session_id": session_id, "mode": self.mode}
                )
                if not order_present:
                    result.imported_orders += 1
            if order.get("mt5_position_id"):
                orders_by_position.setdefault(str(order["mt5_position_id"]), []).append(order)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for deal in deals:
            normalized = normalize_deal(deal)
            if not normalized.get("mt5_deal_ticket"):
                continue
            already_present = self.database.mt5_deal_exists(normalized["mt5_deal_ticket"])
            self.database.insert_mt5_deal(
                {
                    **normalized,
                    "run_id": run_id,
                    "session_id": session_id,
                    "mode": self.mode,
                }
            )
            if already_present:
                result.duplicate_deals += 1
            else:
                result.imported_deals += 1
            position_key = normalized.get("mt5_position_id") or normalized["mt5_deal_ticket"]
            grouped.setdefault(str(position_key), []).append(normalized)

        active_positions = {
            str(row.get("mt5_position_id")): row
            for row in (normalize_position(item) for item in self._positions())
            if row.get("mt5_position_id")
        }
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
                active_position=active_positions.get(position_id),
            )
            if trade:
                existed = self.database._existing_trade_internal_id(trade) is not None
                self.database.insert_trade(trade)
                if existed:
                    result.updated_trades += 1
                else:
                    result.imported_trades += 1
                self._events_from_deals(position_id, position_deals, trade, run_id, session_id)
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
            existing = self.database.connection.execute(
                "SELECT * FROM trades WHERE mt5_position_id = ? AND mode = 'demo_live'",
                (row.get("mt5_position_id"),),
            ).fetchone()
            if existing:
                existing_row = dict(existing)
                self.database.update_trade_fields(
                    existing_row["internal_trade_id"],
                    {
                        "remaining_volume": row.get("volume"),
                        "final_stop_loss": row.get("sl"),
                        "take_profit": row.get("tp") or existing_row.get("take_profit"),
                        "status": "PARTIALLY_CLOSED" if float(row.get("volume") or 0) < float(existing_row.get("initial_volume") or existing_row.get("lot") or 0) else "OPEN",
                    },
                )
                if row.get("sl") and float(row.get("sl")) != float(existing_row.get("final_stop_loss") or 0):
                    self.database.insert_position_event(
                        {
                            "event_id": f"reconcile_sl_{row.get('mt5_position_id')}_{row.get('sl')}",
                            "run_id": run_id,
                            "session_id": session_id,
                            "internal_trade_id": existing_row.get("internal_trade_id"),
                            "mt5_position_id": row.get("mt5_position_id"),
                            "event_type": "TRAILING_UPDATED",
                            "timestamp_utc": utc_now_iso(),
                            "old_stop_loss": existing_row.get("final_stop_loss"),
                            "new_stop_loss": row.get("sl"),
                            "payload": {"source": "broker_reconciliation"},
                        }
                    )
            else:
                self.database.insert_trade(trade_from_open_position(row, run_id, session_id, self.mode))
                result.divergences.append(f"Imported missing open position {row.get('mt5_position_id')}")
            self.database.insert_position_event(
                {
                    "event_id": f"reconcile_open_{row.get('mt5_position_id')}_{row.get('volume')}_{row.get('sl')}",
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
            result.synchronized_positions += 1
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
        history.synchronized_positions += open_positions.synchronized_positions
        return history

    def reconcile_cycle(
        self,
        start_utc: datetime,
        end_utc: datetime,
        run_id: str,
        session_id: str,
        session_name: str | None,
    ) -> ReconciliationResult:
        result = self.full_session_reconciliation(
            start_utc, end_utc, run_id, session_id, session_name
        )
        for order in self._orders():
            normalized = normalize_order(order)
            if normalized.get("mt5_order_ticket"):
                self.database.insert_mt5_order(
                    {**normalized, "run_id": run_id, "session_id": session_id, "mode": self.mode}
                )
        return result

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

    def _orders(self) -> list[Any]:
        if not hasattr(self.broker_api, "orders_get"):
            return []
        return list(self.broker_api.orders_get() or [])

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

    def _events_from_deals(
        self,
        position_id: str,
        deals: list[dict[str, Any]],
        trade: dict[str, Any],
        run_id: str | None,
        session_id: str | None,
    ) -> None:
        first_direction = deals[0].get("direction") if deals else None
        entries = [
            item for item in deals
            if item.get("direction") == first_direction and int(item.get("entry") or 0) == 0
        ]
        exits = [
            item for item in deals
            if item not in entries or int(item.get("entry") or 0) != 0
        ]
        for index, deal in enumerate(sorted(exits, key=lambda item: item.get("time_utc") or "")):
            is_final = index == len(exits) - 1 and trade.get("status") == "CLOSED"
            event_type = "POSITION_CLOSED" if is_final else "PARTIAL_CLOSE_CONFIRMED"
            self.database.insert_position_event(
                {
                    "event_id": f"reconcile_deal_{deal.get('mt5_deal_ticket')}",
                    "run_id": run_id,
                    "session_id": session_id,
                    "internal_trade_id": trade.get("internal_trade_id"),
                    "mt5_position_id": position_id,
                    "event_type": event_type,
                    "timestamp_utc": deal.get("time_utc") or utc_now_iso(),
                    "volume": deal.get("volume"),
                    "payload": {"deal": deal, "entry_deal_count": len(entries)},
                }
            )


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
    timestamp = row.get("time")
    return {
        "mt5_position_id": str(row.get("ticket")) if row.get("ticket") is not None else None,
        "symbol": row.get("symbol"),
        "direction": direction_from_deal_type(row.get("type")),
        "volume": row.get("volume"),
        "price_open": row.get("price_open"),
        "sl": row.get("sl"),
        "tp": row.get("tp"),
        "profit": row.get("profit"),
        "time_utc": datetime.fromtimestamp(int(timestamp), tz=timezone.utc).isoformat() if timestamp is not None else None,
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
        "direction": direction_from_order_type(row.get("type")),
        "volume_initial": row.get("volume_initial"),
        "volume_current": row.get("volume_current"),
        "price_open": row.get("price_open"),
        "sl": row.get("sl"),
        "tp": row.get("tp"),
        "time_setup_utc": time_utc,
        "state": row.get("state"),
        "reason": row.get("reason"),
        "comment": row.get("comment"),
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


def direction_from_order_type(order_type: Any) -> str:
    try:
        value = int(order_type)
    except (TypeError, ValueError):
        return "UNKNOWN"
    return "BUY" if value in {0, 2, 4, 6} else "SELL" if value in {1, 3, 5, 7} else "UNKNOWN"


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
    active_position: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    sorted_deals = sorted(deals, key=lambda item: item.get("time_utc") or "")
    if not sorted_deals:
        return None
    open_deal = sorted_deals[0]
    direction = open_deal.get("direction") or "UNKNOWN"
    entry_deals = [
        item
        for item in sorted_deals
        if item.get("direction") == direction and int(item.get("entry") or 0) == 0
    ] or [open_deal]
    exit_deals = [
        item
        for item in sorted_deals
        if item not in entry_deals or int(item.get("entry") or 0) != 0
    ]
    close_deal = exit_deals[-1] if exit_deals else None
    orders = orders or []
    symbol_info = symbol_info or {}
    order_sl = first_nonzero([order.get("sl") for order in orders])
    order_tp = first_nonzero([order.get("tp") for order in orders])
    entry_volume = sum(float(item.get("volume") or 0.0) for item in entry_deals)
    entry_price = (
        sum(float(item.get("price") or 0.0) * float(item.get("volume") or 0.0) for item in entry_deals)
        / entry_volume
        if entry_volume > 0
        else float(open_deal.get("price") or 0.0)
    )
    exit_price = float(close_deal.get("price") or 0.0) if close_deal else None
    pnl_gross = sum(float(item.get("profit") or 0.0) for item in sorted_deals)
    commission = sum(float(item.get("commission") or 0.0) for item in sorted_deals)
    swap = sum(float(item.get("swap") or 0.0) for item in sorted_deals)
    pnl_net = pnl_gross + commission + swap
    volume = entry_volume or float(open_deal.get("volume") or 0.0)
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
    status = "OPEN" if active_position else "CLOSED" if close_deal else "OPEN"
    remaining_volume = float(active_position.get("volume") or 0.0) if active_position else max(
        volume - sum(float(item.get("volume") or 0.0) for item in exit_deals), 0.0
    )
    if status == "OPEN" and remaining_volume < volume:
        status = "PARTIALLY_CLOSED"
    first_partial = exit_deals[0] if exit_deals and (len(exit_deals) > 1 or status == "PARTIALLY_CLOSED") else None
    duration_seconds = None
    if close_deal and open_deal.get("time_utc") and close_deal.get("time_utc"):
        opened = _parse_time(open_deal.get("time_utc"))
        closed = _parse_time(close_deal.get("time_utc"))
        if opened and closed:
            duration_seconds = int((closed - opened).total_seconds())
    exit_reason = infer_exit_reason(exit_price, stop_loss, take_profit, tick_size, status)
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
        remaining_volume=remaining_volume,
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
        duration_seconds=duration_seconds,
        exit_reason=exit_reason,
        tp1_actual_price=float(first_partial.get("price") or 0.0) if first_partial else None,
        tp1_pnl=float(first_partial.get("profit") or 0.0) if first_partial else None,
        tp1_volume=float(first_partial.get("volume") or 0.0) if first_partial else None,
        tp1_time=first_partial.get("time_utc") if first_partial else None,
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


def trade_from_open_position(
    position: dict[str, Any],
    run_id: str | None,
    session_id: str | None,
    mode: str,
) -> dict[str, Any]:
    position_id = str(position["mt5_position_id"])
    entry = float(position.get("price_open") or 0.0)
    stop = float(position.get("sl") or 0.0)
    take_profit = float(position.get("tp") or 0.0)
    return TradeRecord(
        run_id=run_id,
        session_id=session_id,
        internal_trade_id=f"mt5_position_{position_id}",
        mt5_position_id=position_id,
        mode=mode,
        source="mt5_reconciliation",
        symbol=position.get("symbol") or "UNKNOWN",
        session=None,
        direction=position.get("direction") or "UNKNOWN",
        lot=float(position.get("volume") or 0.0),
        initial_volume=float(position.get("volume") or 0.0),
        remaining_volume=float(position.get("volume") or 0.0),
        entry_price=entry,
        actual_entry_price=entry,
        requested_price=entry,
        stop_loss=stop,
        initial_stop_loss=stop,
        final_stop_loss=stop,
        take_profit=take_profit,
        entry_time=position.get("time_utc") or utc_now_iso(),
        status="OPEN",
        metadata={"reconciled_open_position": position},
    ).to_dict()


def infer_exit_reason(
    exit_price: float | None,
    stop_loss: float,
    take_profit: float,
    tick_size: float,
    status: str,
) -> str | None:
    if status != "CLOSED" or exit_price is None:
        return None
    tolerance = max(tick_size * 3, 1e-12)
    if stop_loss and abs(exit_price - stop_loss) <= tolerance:
        return "STOP_LOSS"
    if take_profit and abs(exit_price - take_profit) <= tolerance:
        return "TAKE_PROFIT"
    return "BROKER_OR_MANUAL_EXIT"


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

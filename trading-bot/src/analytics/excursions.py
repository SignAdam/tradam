"""MFE/MAE calculations from bid/ask ticks."""

from __future__ import annotations

from typing import Any


def calculate_excursions(
    direction: str,
    entry_price: float,
    stop_loss: float | None,
    volume: float,
    ticks: list[dict[str, Any]],
    tick_size: float = 0.01,
    tick_value: float = 1.0,
) -> dict[str, float | None]:
    if not ticks or entry_price <= 0:
        return {
            "max_favorable_price": None,
            "max_adverse_price": None,
            "mfe_price": None,
            "mae_price": None,
            "mfe_amount": None,
            "mae_amount": None,
            "mfe_r": None,
            "mae_r": None,
            "max_unrealized_profit": None,
            "max_unrealized_loss": None,
        }
    side = direction.upper()
    close_prices = []
    for tick in ticks:
        if side == "BUY":
            close_prices.append(float(tick.get("bid") or tick.get("last") or 0.0))
        else:
            close_prices.append(float(tick.get("ask") or tick.get("last") or 0.0))
    close_prices = [price for price in close_prices if price > 0]
    if not close_prices:
        return calculate_excursions(direction, entry_price, stop_loss, volume, [], tick_size, tick_value)

    if side == "BUY":
        max_favorable = max(close_prices)
        max_adverse = min(close_prices)
        mfe_price = max_favorable - entry_price
        mae_price = entry_price - max_adverse
    else:
        max_favorable = min(close_prices)
        max_adverse = max(close_prices)
        mfe_price = entry_price - max_favorable
        mae_price = max_adverse - entry_price

    money_per_price = tick_value / tick_size if tick_size else 0.0
    mfe_amount = max(mfe_price, 0.0) * money_per_price * volume
    mae_amount = max(mae_price, 0.0) * money_per_price * volume
    risk_price = abs(entry_price - stop_loss) if stop_loss else None
    mfe_r = mfe_price / risk_price if risk_price else None
    mae_r = mae_price / risk_price if risk_price else None
    return {
        "max_favorable_price": max_favorable,
        "max_adverse_price": max_adverse,
        "mfe_price": mfe_price,
        "mae_price": mae_price,
        "mfe_amount": mfe_amount,
        "mae_amount": mae_amount,
        "mfe_r": mfe_r,
        "mae_r": mae_r,
        "max_unrealized_profit": mfe_amount,
        "max_unrealized_loss": -mae_amount,
    }


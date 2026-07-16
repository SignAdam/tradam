"""Command-line runner for paper, demo_live, live-guarded, and report workflows."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analytics.session_report import SessionReportGenerator
from src.analytics.trade_logger import TradeLogger
from src.mt5.connection import MT5Connection, mt5_available
from src.mt5.market_data import MT5MarketData
from src.mt5.order_manager import OrderManager, OrderRequest
from src.mt5.symbol_mapper import SymbolMapper
from src.news.economic_calendar import EconomicCalendar
from src.news.news_client import NewsClient
from src.news.news_filter import NewsFilter
from src.storage.database import Database
from src.storage.models import NewsRecord, TradeRecord
from src.strategy.risk_manager import RiskManager, RiskState, SymbolTradingSpec
from src.strategy.session_filter import SessionFilter
from src.strategy.signal_engine import SignalEngine
from src.utils.config import enforce_live_trading_guard, load_project_config, redacted_config
from src.utils.exceptions import BrokerValidationError
from src.utils.logger import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tradam demo-first scalping bot")
    parser.add_argument("--mode", choices=["backtest", "paper", "demo_live", "live"], help="Runtime mode override")
    parser.add_argument("--once", action="store_true", help="Run one scan cycle and exit")
    parser.add_argument("--report-example", action="store_true", help="Generate an example report and exit")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_project_config()
    if args.mode:
        config["settings"]["trading"]["mode"] = args.mode
        enforce_live_trading_guard(config)

    root = Path(config["root"])
    settings = config["settings"]
    loggers = setup_logging(
        root / settings["paths"]["logs_dir"],
        settings.get("logging", {}).get("level", "INFO"),
        int(settings.get("logging", {}).get("max_bytes", 5_242_880)),
        int(settings.get("logging", {}).get("backup_count", 5)),
    )
    db_path = root / settings["storage"]["database_path"]
    db = Database(db_path)
    db.initialize()
    trade_logger = TradeLogger(db, loggers["signals"], loggers["orders"], loggers["news"])

    if args.report_example:
        paths = generate_example_report(db, root, config)
        loggers["analytics"].info("Generated example report: %s", paths)
        return 0

    mode = settings["trading"]["mode"]
    session_filter = SessionFilter(config["sessions"])
    session_state = session_filter.evaluate()
    if not session_state["allow_new_trades"]:
        loggers["technical"].info("No new trades allowed: %s", "; ".join(session_state["reasons"]))

    connection = None
    market_data = None
    account_equity = 10_000.0
    try:
        if mode in {"demo_live", "live"} or mt5_available():
            connection = MT5Connection(settings["mt5"])
            connection.initialize()
            if mode in {"demo_live", "live"}:
                snapshot = connection.ensure_account_safety(settings["trading"])
                account_equity = float(snapshot.equity or snapshot.balance or account_equity)
                loggers["technical"].info(
                    "Connected to MT5 account login=%s server=%s demo=%s",
                    snapshot.login,
                    snapshot.server,
                    snapshot.is_demo,
                )
            market_data = MT5MarketData(connection)
        elif mode == "paper":
            loggers["technical"].info("MT5 is unavailable; paper mode exits without market-data scan.")
            return 0

        run_scan_cycle(
            config=config,
            market_data=market_data,
            trade_logger=trade_logger,
            loggers=loggers,
            account_equity=account_equity,
            session_state=session_state,
        )
        return 0
    finally:
        if connection:
            connection.shutdown()
        db.close()


def run_scan_cycle(
    config: dict[str, Any],
    market_data: MT5MarketData,
    trade_logger: TradeLogger,
    loggers: dict[str, Any],
    account_equity: float,
    session_state: dict[str, Any],
) -> None:
    settings = config["settings"]
    strategy_cfg = settings["strategy"]
    symbols_cfg = config["symbols"]
    mode = settings["trading"]["mode"]
    available = market_data.get_available_symbols()
    mappings = SymbolMapper(symbols_cfg).resolve_all(available)
    signal_engine = SignalEngine(strategy_cfg, symbols_cfg)
    risk_manager = RiskManager(config["risk"])
    order_manager = OrderManager(
        market_data=market_data,
        trading_config=settings["trading"],
        risk_config=config["risk"],
        symbols_config=symbols_cfg,
    )
    news_client = NewsClient(settings.get("news", {}))
    calendar = EconomicCalendar(settings.get("news", {}))
    news_filter = NewsFilter(settings.get("news", {}), economic_calendar=calendar)
    calendar_events = calendar.fetch() if settings.get("news", {}).get("enabled", True) else []

    timeframes = [
        strategy_cfg.get("entry_timeframe", "M5"),
        strategy_cfg.get("confirmation_timeframe", "M15"),
        strategy_cfg.get("trend_timeframe", "H1"),
        strategy_cfg.get("context_timeframe", "H4"),
    ]
    bars_by_tf = strategy_cfg.get("history_bars", {})

    for logical, mapping in mappings.items():
        if not mapping.broker_symbol:
            loggers["errors"].warning("No broker symbol mapped for %s", logical)
            continue
        try:
            symbol_profile = symbols_cfg["symbols"][logical]
            articles = (
                news_client.fetch_for_symbol(
                    logical,
                    symbol_profile.get("queries", [logical]),
                    int(settings.get("news", {}).get("max_articles_per_symbol", 20)),
                )
                if settings.get("news", {}).get("enabled", True)
                else []
            )
            trade_logger.log_news(
                [
                    NewsRecord(
                        symbol_group=item.symbol_group,
                        title=item.title,
                        source=item.source,
                        published_at=item.published_at,
                        impact=item.impact,
                        sentiment=item.sentiment,
                        score=item.score,
                        url=item.url,
                        raw=item.raw,
                    )
                    for item in articles
                ]
            )
            news_context = news_filter.evaluate(logical, articles, calendar_events)
            frames = market_data.get_multi_timeframe_rates(mapping.broker_symbol, timeframes, bars_by_tf)
            tick = market_data.tick(mapping.broker_symbol)
            decision = signal_engine.evaluate(
                logical_symbol=logical,
                broker_symbol=mapping.broker_symbol,
                session=session_state.get("session"),
                frames=frames,
                news_context=news_context,
                market_context={"spread_points": tick.spread_points},
            )
            trade_logger.log_decision(decision)
            if decision.decision != "ACCEPTED" or not session_state.get("allow_new_trades"):
                continue

            info = market_data.symbol_info(mapping.broker_symbol)
            spec = SymbolTradingSpec.from_symbol_info(info, config["risk"].get("position_sizing", {}))
            entry_price = tick.ask if decision.direction == "BUY" else tick.bid
            stop_loss = float(decision.risk["stop_loss_price"])
            take_profit = float(decision.risk["take_profit_price"])
            lot = risk_manager.calculate_position_size(account_equity, entry_price, stop_loss, spec)
            risk_check = risk_manager.validate_trade(
                RiskState(equity=account_equity),
                decision.direction or "BUY",
                lot,
                entry_price,
                stop_loss,
                take_profit,
                spec,
            )
            if not risk_check.ok:
                decision.decision = "REJECTED"
                decision.rejected_reason = "RISK_CHECK_FAILED"
                decision.reasons.extend(risk_check.reasons)
                trade_logger.log_decision(decision)
                continue

            order = OrderRequest(
                symbol=mapping.broker_symbol,
                direction=decision.direction or "BUY",
                lot=lot,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                deviation_points=int(settings["trading"].get("max_slippage_points", 30)),
            )
            order_validation = order_manager.validate_order(order, symbol_info=info, tick=tick)
            if not order_validation.ok:
                decision.decision = "REJECTED"
                decision.rejected_reason = "ORDER_VALIDATION_FAILED"
                decision.reasons.extend(order_validation.reasons)
                trade_logger.log_decision(decision)
                loggers["orders"].warning(
                    "Order refused before send symbol=%s reasons=%s",
                    mapping.broker_symbol,
                    "; ".join(order_validation.reasons),
                )
                continue
            result = order_manager.send_order(order)
            trade_logger.log_trade(
                TradeRecord(
                    trade_id=str(result.get("order", result.get("order_id"))),
                    symbol=mapping.broker_symbol,
                    session=session_state.get("session"),
                    direction=order.direction,
                    lot=lot,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    entry_time=datetime.now(timezone.utc).isoformat(),
                    spread=tick.spread_points,
                    timeframe=strategy_cfg.get("entry_timeframe", "M5"),
                    h1_trend="see_decision_indicators",
                    rsi=decision.indicators.get("rsi"),
                    ema20=decision.indicators.get("ema20"),
                    ema50=decision.indicators.get("ema50_h1"),
                    ema200=decision.indicators.get("ema200_h1"),
                    atr=decision.indicators.get("atr"),
                    macd=decision.indicators.get("macd_hist"),
                    fibonacci_level=(decision.indicators.get("fibonacci") or {}).get("level"),
                    signal_reason="; ".join(decision.reasons),
                    news_active=news_context.get("active_news", []),
                    sentiment=news_context.get("sentiment"),
                    status="OPEN" if mode != "backtest" else "SIMULATED",
                    metadata={"order_result": result, "decision": decision.to_dict()},
                )
            )
        except BrokerValidationError as exc:
            loggers["orders"].warning(
                "Broker/order validation refused %s/%s: %s",
                logical,
                mapping.broker_symbol,
                exc,
            )
        except Exception as exc:  # keep scanning other symbols.
            loggers["errors"].exception("Scan failed for %s/%s: %s", logical, mapping.broker_symbol, exc)


def generate_example_report(db: Database, root: Path, config: dict[str, Any]) -> dict[str, str]:
    started = "2026-07-07T14:30:00+00:00"
    ended = "2026-07-07T22:00:00+00:00"
    db.insert_decision(
        {
            "created_at": "2026-07-07T15:02:00+00:00",
            "symbol": "XAUUSD",
            "session": "US",
            "direction": "BUY",
            "score": 8,
            "decision": "ACCEPTED",
            "reasons": [
                "H1 bullish trend",
                "M5 pullback around EMA20",
                "Price near Fibonacci 50.0% confluence",
                "No high-impact USD news in block window",
            ],
            "risk": {"risk_percent": 0.5, "lot_size": 0.02, "risk_reward": 1.5},
            "indicators": {"rsi": 53.4, "atr": 1.42, "adx": 24.1},
            "news": {"sentiment": "neutral", "blocked": False},
            "rejected_reason": None,
            "raw": {},
        }
    )
    db.insert_decision(
        {
            "created_at": "2026-07-07T16:12:00+00:00",
            "symbol": "BTC",
            "session": "US",
            "direction": "BUY",
            "score": 3,
            "decision": "REJECTED",
            "reasons": ["ADX too low", "Bollinger width/price too low"],
            "risk": {},
            "indicators": {"adx": 12.3},
            "news": {"sentiment": "neutral", "blocked": False},
            "rejected_reason": "ADX_TOO_LOW",
            "raw": {},
        }
    )
    db.insert_trade(
        TradeRecord(
            trade_id="example-001",
            symbol="XAUUSD",
            session="US",
            direction="BUY",
            lot=0.02,
            entry_price=2361.5,
            stop_loss=2359.8,
            take_profit=2364.05,
            entry_time="2026-07-07T15:03:00+00:00",
            exit_time="2026-07-07T15:26:00+00:00",
            exit_price=2364.05,
            pnl=51.0,
            duration_seconds=1380,
            spread=24,
            timeframe="M5",
            h1_trend="bullish",
            rsi=53.4,
            ema20=2360.9,
            ema50=2354.1,
            ema200=2338.7,
            atr=1.42,
            macd=0.18,
            fibonacci_level="50.0%",
            signal_reason="H1 trend + EMA20 pullback + Fibonacci confluence",
            news_active=[],
            sentiment="neutral",
            status="CLOSED",
        )
    )
    generator = SessionReportGenerator(db, root / config["settings"]["paths"]["reports_dir"])
    return generator.generate(
        session="US",
        started_at=started,
        ended_at=ended,
        mode="paper",
        symbols=["XAUUSD", "BTC", "DJ30"],
        config_snapshot=redacted_config(config),
    )


if __name__ == "__main__":
    raise SystemExit(main())

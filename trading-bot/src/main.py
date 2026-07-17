"""Command-line runner for paper, demo_live, live-guarded, and report workflows."""

from __future__ import annotations

import argparse
import tempfile
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any

from src.analytics.session_report import SessionReportGenerator
from src.analytics.trade_logger import TradeLogger
from src.mt5.connection import MT5Connection, mt5_available, mt5
from src.mt5.market_data import MT5MarketData
from src.mt5.order_manager import OrderManager, OrderRequest
from src.mt5.reconciliation import BrokerReconciliationService
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
from src.utils.identity import utc_now_iso
from src.utils.logger import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tradam demo-first scalping bot")
    parser.add_argument("--mode", choices=["backtest", "paper", "demo_live", "live"], help="Runtime mode override")
    parser.add_argument("--once", action="store_true", help="Run one scan cycle and exit")
    parser.add_argument("--report-example", action="store_true", help="Generate an example report and exit")
    parser.add_argument("--session-report", action="store_true", help="Generate a report from real stored bot activity")
    parser.add_argument("--reconcile-mt5", action="store_true", help="Reconcile MT5 history before generating a demo_live report")
    parser.add_argument("--report-session", default="US", help="Session name to display in the generated report")
    parser.add_argument("--report-run-id", help="Optional run_id filter for a real report")
    parser.add_argument("--report-session-id", help="Optional session_id filter for a real report")
    parser.add_argument("--report-start", help="Report start ISO datetime, for example 2026-07-17T00:00:00+00:00")
    parser.add_argument("--report-end", help="Report end ISO datetime, defaults to now in UTC")
    parser.add_argument("--cleanup-example-fixtures", action="store_true", help="Back up DB and remove legacy example-001 rows")
    parser.add_argument("--migrate-only", action="store_true", help="Initialize/migrate SQLite and exit")
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

    if args.migrate_only:
        print(f"Database migrated: {db.path}")
        return 0

    if args.cleanup_example_fixtures:
        backup_path = db.backup()
        deleted = db.cleanup_example_fixtures()
        loggers["analytics"].info("Backed up DB to %s and removed %s legacy example rows", backup_path, deleted)
        print(f"Backup: {backup_path}")
        print(f"Removed legacy example-001 rows: {deleted}")
        return 0

    if args.report_example:
        paths = generate_example_report(db, root, config)
        loggers["analytics"].info("Generated example report: %s", paths)
        return 0

    if args.session_report:
        paths = generate_real_session_report(db, root, config, args)
        loggers["analytics"].info("Generated real session report: %s", paths)
        return 0

    mode = settings["trading"]["mode"]
    run_id = db.create_run(mode=mode, source="bot", config=redacted_config(config))
    session_filter = SessionFilter(config["sessions"])
    session_state = session_filter.evaluate()
    session_id = db.create_trading_session(
        run_id=run_id,
        session=session_state.get("session"),
        mode=mode,
        source="bot",
        broker_timezone=config["sessions"].get("broker_timezone"),
        broker_utc_offset_minutes=settings.get("mt5", {}).get("broker_utc_offset_minutes"),
    )
    trade_logger.set_context(run_id=run_id, session_id=session_id, mode=mode, source="bot")
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
            run_id=run_id,
            session_id=session_id,
        )
        return 0
    finally:
        if "run_id" in locals():
            db.finish_run(run_id)
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
    run_id: str,
    session_id: str,
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
            for provider_status in news_client.provider_health:
                db_status = provider_status.to_dict()
                db_status.update({"run_id": run_id, "session_id": session_id})
                trade_logger.database.insert_news_provider_status(db_status)
            news_context = news_filter.evaluate(
                logical,
                articles,
                calendar_events,
                provider_health=[status.to_dict() for status in news_client.provider_health],
            )
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
            decision.run_id = run_id
            decision.session_id = session_id
            decision.mode = mode
            decision.source = "bot"
            trade_logger.log_decision(decision)
            if decision.decision != "ACCEPTED" or not session_state.get("allow_new_trades"):
                continue

            info = market_data.symbol_info(mapping.broker_symbol)
            log_symbol_profile(loggers, logical, mapping.broker_symbol, symbol_profile, info, tick)
            spec = SymbolTradingSpec.from_symbol_info(info, config["risk"].get("position_sizing", {}))
            entry_price = tick.ask if decision.direction == "BUY" else tick.bid
            stop_loss = float(decision.risk["stop_loss_price"])
            take_profit = float(decision.risk["take_profit_price"])
            sizing_diagnostics: dict[str, Any] = {}
            if mode == "demo_live" and mt5 is not None:
                try:
                    lot, sizing_diagnostics = risk_manager.calculate_position_size_with_broker(
                        mt5,
                        mapping.broker_symbol,
                        decision.direction or "BUY",
                        account_equity,
                        entry_price,
                        stop_loss,
                        spec,
                        risk_percent=float(config["risk"].get("risk", {}).get("risk_per_trade_percent", 0.25)),
                    )
                except Exception as exc:
                    decision.decision = "REJECTED"
                    decision.rejected_reason = "BROKER_RISK_SIZING_FAILED"
                    decision.reasons.append(str(exc))
                    trade_logger.log_decision(decision)
                    continue
            else:
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
                    run_id=run_id,
                    session_id=session_id,
                    signal_id=decision.signal_id,
                    internal_trade_id=str(result.get("order", result.get("order_id")) or result.get("deal") or f"{mapping.broker_symbol}-{utc_now_iso()}"),
                    mt5_order_ticket=str(result.get("order")) if result.get("order") else None,
                    mt5_deal_ticket=str(result.get("deal")) if result.get("deal") else None,
                    mt5_position_id=str(result.get("order")) if result.get("order") else None,
                    mode=mode,
                    source="bot",
                    symbol=mapping.broker_symbol,
                    session=session_state.get("session"),
                    direction=order.direction,
                    lot=lot,
                    entry_price=entry_price,
                    requested_price=entry_price,
                    actual_entry_price=float(result.get("price", entry_price) or entry_price),
                    entry_slippage=(float(result.get("price", entry_price) or entry_price) - entry_price),
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    entry_time=datetime.now(timezone.utc).isoformat(),
                    signal_time=decision.created_at_utc,
                    order_time=datetime.now(timezone.utc).isoformat(),
                    execution_time=datetime.now(timezone.utc).isoformat(),
                    spread=tick.spread_points,
                    spread_points=tick.spread_points,
                    spread_price=abs(tick.ask - tick.bid),
                    initial_volume=lot,
                    remaining_volume=lot,
                    initial_stop_loss=stop_loss,
                    final_stop_loss=stop_loss,
                    initial_risk_price=abs(entry_price - stop_loss),
                    initial_risk_amount=risk_check.risk_amount,
                    initial_risk_percent=risk_check.risk_percent,
                    pnl_gross=None,
                    pnl_net=None,
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
                    metadata={"order_result": result, "decision": decision.to_dict(), "sizing": sizing_diagnostics},
                )
            )
            trade_logger.log_position_event(
                {
                    "event_type": "POSITION_OPENED",
                    "mt5_position_id": str(result.get("order")) if result.get("order") else None,
                    "bid": tick.bid,
                    "ask": tick.ask,
                    "spread": tick.spread_points,
                    "volume": lot,
                    "payload": {"order_result": result},
                }
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


def log_symbol_profile(
    loggers: dict[str, Any],
    logical: str,
    broker_symbol: str,
    symbol_profile: dict[str, Any],
    info: dict[str, Any],
    tick: Any,
) -> None:
    loggers["technical"].info(
        "symbol_profile logical=%s mt5=%s asset_class=%s point=%s digits=%s contract_size=%s "
        "tick_size=%s tick_value=%s volume_min=%s volume_step=%s stop_level=%s spread_points=%s",
        logical,
        broker_symbol,
        symbol_profile.get("asset_class"),
        info.get("point"),
        info.get("digits"),
        info.get("trade_contract_size"),
        info.get("trade_tick_size"),
        info.get("trade_tick_value"),
        info.get("volume_min"),
        info.get("volume_step"),
        info.get("trade_stops_level"),
        getattr(tick, "spread_points", None),
    )


def generate_example_report(db: Database, root: Path, config: dict[str, Any]) -> dict[str, str]:
    del db
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_db = Database(Path(tmpdir) / "fixtures.sqlite")
        fixture_db.initialize()
        return _generate_example_report_with_db(fixture_db, root, config)


def _generate_example_report_with_db(db: Database, root: Path, config: dict[str, Any]) -> dict[str, str]:
    started = "2026-07-07T14:30:00+00:00"
    ended = "2026-07-07T22:00:00+00:00"
    run_id = "fixture_run"
    session_id = "fixture_session"
    db.insert_decision(
        {
            "run_id": run_id,
            "session_id": session_id,
            "signal_id": "fixture_signal_accepted",
            "mode": "paper",
            "source": "fixture",
            "is_fixture": True,
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
            "run_id": run_id,
            "session_id": session_id,
            "signal_id": "fixture_signal_rejected",
            "mode": "paper",
            "source": "fixture",
            "is_fixture": True,
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
            run_id=run_id,
            session_id=session_id,
            signal_id="fixture_signal_accepted",
            internal_trade_id="fixture_trade_example_001",
            mode="paper",
            source="fixture",
            is_fixture=True,
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
        run_id=run_id,
        session_id=session_id,
        include_fixtures=True,
    )


def generate_real_session_report(
    db: Database,
    root: Path,
    config: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, str]:
    started_at, ended_at = report_window(args.report_start, args.report_end)
    if args.reconcile_mt5 and config["settings"]["trading"]["mode"] == "demo_live":
        connection = MT5Connection(config["settings"]["mt5"])
        connection.initialize()
        try:
            connection.ensure_account_safety(config["settings"]["trading"])
            BrokerReconciliationService(db, mode="demo_live").full_session_reconciliation(
                parse_report_datetime(started_at),
                parse_report_datetime(ended_at),
                run_id=args.report_run_id,
                session_id=args.report_session_id,
                session_name=args.report_session,
            )
        finally:
            connection.shutdown()
    symbols = [
        symbol
        for symbol, profile in config["symbols"].get("symbols", {}).items()
        if profile.get("enabled", True)
    ]
    generator = SessionReportGenerator(db, root / config["settings"]["paths"]["reports_dir"])
    return generator.generate(
        session=args.report_session,
        started_at=started_at,
        ended_at=ended_at,
        mode=config["settings"]["trading"]["mode"],
        symbols=symbols,
        config_snapshot=redacted_config(config),
        run_id=args.report_run_id,
        session_id=args.report_session_id,
        include_fixtures=False,
    )


def report_window(start: str | None, end: str | None) -> tuple[str, str]:
    ended = parse_report_datetime(end) if end else datetime.now(timezone.utc)
    if start:
        started = parse_report_datetime(start)
    else:
        started = datetime.combine(ended.date(), time.min, tzinfo=timezone.utc)
    return started.isoformat(), ended.isoformat()


def parse_report_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


if __name__ == "__main__":
    raise SystemExit(main())

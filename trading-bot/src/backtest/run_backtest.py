"""CLI helper to run the simple backtester from CSV historical files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from src.backtest.simple_backtester import SimpleBacktester
from src.mt5.market_data import normalize_rates_dataframe
from src.utils.config import load_project_config, redacted_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a CSV-based strategy backtest")
    parser.add_argument("--data-dir", default="data/backtest", help="Directory containing SYMBOL_TIMEFRAME.csv files")
    parser.add_argument("--output", default="reports/backtest_summary.json", help="JSON output path")
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--symbols", nargs="*", default=["XAUUSD", "BTC", "DJ30"])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_project_config()
    root = Path(config["root"])
    data_dir = root / args.data_dir
    frames_by_symbol = load_csv_frames(
        data_dir,
        args.symbols,
        [
            config["settings"]["strategy"].get("entry_timeframe", "M5"),
            config["settings"]["strategy"].get("confirmation_timeframe", "M15"),
            config["settings"]["strategy"].get("trend_timeframe", "H1"),
        ],
    )
    backtester = SimpleBacktester(
        config["settings"]["strategy"],
        config["risk"],
        config["symbols"],
        initial_equity=args.initial_equity,
    )
    result = backtester.run(frames_by_symbol)
    payload = {
        "metrics": result.metrics,
        "performance_by_symbol": result.by_symbol,
        "performance_by_session": result.by_session,
        "performance_by_signal_type": result.by_signal_type,
        "trades": result.trades,
        "decision_count": len(result.decisions),
        "configuration": redacted_config(config),
    }
    output = root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, ensure_ascii=True, default=str), "utf-8")
    print(f"Backtest written to {output}")
    return 0


def load_csv_frames(data_dir: Path, symbols: list[str], timeframes: list[str]) -> dict[str, dict[str, pd.DataFrame]]:
    frames_by_symbol: dict[str, dict[str, pd.DataFrame]] = {}
    for symbol in symbols:
        frames: dict[str, pd.DataFrame] = {}
        for timeframe in timeframes:
            path = data_dir / f"{symbol}_{timeframe}.csv"
            if not path.exists():
                continue
            frame = pd.read_csv(path)
            if "time" in frame.columns:
                frame["time"] = pd.to_datetime(frame["time"], utc=True)
            frames[timeframe] = normalize_rates_dataframe(frame)
        if frames:
            frames_by_symbol[symbol] = frames
    return frames_by_symbol


if __name__ == "__main__":
    raise SystemExit(main())


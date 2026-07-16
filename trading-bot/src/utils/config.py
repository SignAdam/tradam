"""Configuration loading with explicit live-trading safety guards."""

from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from src.utils.exceptions import ConfigurationError, SafetyError


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def parse_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigurationError(f"Missing configuration file: {path}")
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigurationError(f"Configuration must be a YAML mapping: {path}")
    return data


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _env_or_none(name: str) -> str | None:
    value = os.getenv(name)
    return value if value not in {None, ""} else None


def apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    cfg = deepcopy(config)
    trading = cfg.setdefault("settings", {}).setdefault("trading", {})
    mt5 = cfg.setdefault("settings", {}).setdefault("mt5", {})
    logging = cfg.setdefault("settings", {}).setdefault("logging", {})
    storage = cfg.setdefault("settings", {}).setdefault("storage", {})

    if _env_or_none("TRADING_MODE"):
        trading["mode"] = os.environ["TRADING_MODE"].strip()
    trading["enable_live_trading"] = parse_bool(
        os.getenv("ENABLE_LIVE_TRADING"), trading.get("enable_live_trading", False)
    )
    trading["live_trading_confirmation"] = parse_bool(
        os.getenv("LIVE_TRADING_CONFIRMATION"),
        trading.get("live_trading_confirmation", False),
    )

    for env_name, key in {
        "MT5_LOGIN": "login",
        "MT5_PASSWORD": "password",
        "MT5_SERVER": "server",
        "MT5_PATH": "path",
    }.items():
        value = _env_or_none(env_name)
        if value is not None:
            mt5[key] = int(value) if key == "login" and value.isdigit() else value

    if _env_or_none("LOG_LEVEL"):
        logging["level"] = os.environ["LOG_LEVEL"].upper()
    if _env_or_none("DATABASE_PATH"):
        storage["database_path"] = os.environ["DATABASE_PATH"]
    return cfg


def load_project_config(root: Path | None = None) -> dict[str, Any]:
    root = root or PROJECT_ROOT
    load_dotenv(root / ".env")
    config = {
        "root": str(root),
        "settings": load_yaml(root / "config" / "settings.yaml"),
        "symbols": load_yaml(root / "config" / "symbols.yaml"),
        "sessions": load_yaml(root / "config" / "sessions.yaml"),
        "risk": load_yaml(root / "config" / "risk.yaml"),
    }
    config = apply_env_overrides(config)
    ensure_required_directories(config)
    enforce_live_trading_guard(config)
    return config


def ensure_required_directories(config: dict[str, Any]) -> None:
    root = Path(config["root"])
    paths = config["settings"].get("paths", {})
    for key in ("data_dir", "logs_dir", "reports_dir"):
        directory = root / paths.get(key, key.replace("_dir", ""))
        directory.mkdir(parents=True, exist_ok=True)


def enforce_live_trading_guard(config: dict[str, Any]) -> None:
    trading = config["settings"].get("trading", {})
    mode = trading.get("mode", "paper")
    enable_live = parse_bool(trading.get("enable_live_trading"), False)
    double_confirm = parse_bool(trading.get("live_trading_confirmation"), False)

    if mode not in {"backtest", "paper", "demo_live", "live"}:
        raise ConfigurationError(f"Unsupported trading mode: {mode}")
    if mode == "live" and not enable_live:
        raise SafetyError("Live trading is blocked: ENABLE_LIVE_TRADING=false.")
    if mode == "live" and not double_confirm:
        raise SafetyError("Live trading requires LIVE_TRADING_CONFIRMATION=true.")
    if enable_live and not double_confirm:
        raise SafetyError(
            "ENABLE_LIVE_TRADING=true also requires LIVE_TRADING_CONFIRMATION=true."
        )


def redacted_config(config: dict[str, Any]) -> dict[str, Any]:
    redacted = deepcopy(config)
    mt5 = redacted.get("settings", {}).get("mt5", {})
    if mt5.get("password"):
        mt5["password"] = "***"
    for env_name in (
        "ALPHA_VANTAGE_API_KEY",
        "MARKETAUX_API_KEY",
        "FINNHUB_API_KEY",
        "FMP_API_KEY",
        "NEWSAPI_API_KEY",
    ):
        if os.getenv(env_name):
            redacted.setdefault("env", {})[env_name] = "***"
    return redacted


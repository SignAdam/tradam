"""Structured file logging for technical, signal, order, news, and analytics logs."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_NAMES = ("technical", "signals", "orders", "news", "analytics", "errors")


def setup_logging(
    log_dir: str | Path,
    level: str = "INFO",
    max_bytes: int = 5_242_880,
    backup_count: int = 5,
) -> dict[str, logging.Logger]:
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)
    numeric_level = getattr(logging, level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    loggers: dict[str, logging.Logger] = {}
    for name in LOG_NAMES:
        logger = logging.getLogger(f"tradam.{name}")
        logger.setLevel(numeric_level)
        logger.propagate = False
        logger.handlers.clear()

        file_handler = RotatingFileHandler(
            directory / f"{name}.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        if name in {"technical", "errors"}:
            stream_handler = logging.StreamHandler()
            stream_handler.setFormatter(formatter)
            logger.addHandler(stream_handler)
        loggers[name] = logger
    return loggers


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"tradam.{name}")


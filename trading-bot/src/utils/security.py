"""Centralized redaction for logs, reports, and exported configuration."""

from __future__ import annotations

import os
import re
from copy import deepcopy
from pathlib import Path
from typing import Any


SENSITIVE_KEYS = {
    "password",
    "api_key",
    "apikey",
    "token",
    "secret",
    "mt5_password",
    "alpha_vantage_api_key",
    "marketaux_api_key",
    "finnhub_api_key",
    "fmp_api_key",
    "newsapi_api_key",
}


def redact_sensitive_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _redact_by_key(key, redact_sensitive_data(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item) for item in value)
    if isinstance(value, str):
        return redact_sensitive_string(value)
    return value


def _redact_by_key(key: str, value: Any) -> Any:
    normalized = key.lower().replace("-", "_")
    if normalized in SENSITIVE_KEYS or any(token in normalized for token in ("password", "secret", "api_key", "token")):
        return "***"
    if normalized in {"login", "mt5_login", "account_login"}:
        return mask_login(value)
    if normalized in {"server", "mt5_server"} and value:
        return mask_server(str(value))
    return value


def redact_sensitive_string(value: str) -> str:
    redacted = value
    home = str(Path.home())
    if home and home in redacted:
        redacted = redacted.replace(home, "<HOME>")
    username = os.environ.get("USERNAME") or os.environ.get("USER")
    if username:
        redacted = redacted.replace(username, "<USER>")
    redacted = re.sub(r"(?i)([A-Z]:\\Users\\)[^\\]+", r"\1<USER>", redacted)
    redacted = re.sub(r"(?i)(/Users/)[^/]+", r"\1<USER>", redacted)
    redacted = re.sub(r"(?i)(password|api[_-]?key|token|secret)=([^&\s]+)", r"\1=***", redacted)
    redacted = re.sub(r"\b(\d{6,})(\d{4})\b", r"***\2", redacted)
    return redacted


def mask_login(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    text = str(value)
    return f"***{text[-4:]}" if len(text) > 4 else "***"


def mask_server(value: str) -> str:
    if not value:
        return value
    if "-" in value:
        suffix = value.split("-")[-1]
        return f"***-{suffix}"
    return "***"


def redacted_copy(value: Any) -> Any:
    return redact_sensitive_data(deepcopy(value))

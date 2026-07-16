"""Trading-session filtering wrapper."""

from __future__ import annotations

from datetime import datetime

from src.utils.time_utils import session_state


class SessionFilter:
    def __init__(self, sessions_config: dict) -> None:
        self.sessions_config = sessions_config

    def evaluate(self, at: datetime | None = None) -> dict:
        return session_state(self.sessions_config, at=at)

    def can_open_new_trade(self, at: datetime | None = None) -> tuple[bool, list[str]]:
        state = self.evaluate(at)
        reasons = list(state.get("reasons", []))
        if not state.get("active"):
            reasons.append("No configured trading session is active")
        return bool(state.get("allow_new_trades")), reasons


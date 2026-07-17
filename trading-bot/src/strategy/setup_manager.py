"""Persistent setup identity, expiry, and cooldown management."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from src.utils.identity import utc_now_iso


@dataclass(frozen=True)
class TradingSetup:
    setup_id: str
    symbol: str
    strategy: str
    direction: str
    session: str
    source_candle: str
    structure_id: str
    detected_at: str
    expires_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SetupIdentityManager:
    def __init__(self, database: Any, symbols_config: dict[str, Any]) -> None:
        self.database = database
        self.symbols = symbols_config.get("symbols", symbols_config)

    def create(
        self,
        symbol: str,
        strategy: str,
        direction: str,
        session: str,
        source_candle: str,
        structure_id: str,
        detected_at: datetime | None = None,
        expiry_minutes: int = 8,
    ) -> TradingSetup:
        detected = (detected_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        identity = "|".join(
            [symbol, strategy, direction.upper(), session, source_candle, structure_id]
        )
        setup_id = "setup_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return TradingSetup(
            setup_id=setup_id,
            symbol=symbol,
            strategy=strategy,
            direction=direction.upper(),
            session=session,
            source_candle=source_candle,
            structure_id=structure_id,
            detected_at=detected.isoformat(),
            expires_at=(detected + timedelta(minutes=expiry_minutes)).isoformat(),
        )

    def register(self, setup: TradingSetup, run_id: str, session_id: str) -> bool:
        return bool(self.database.insert_setup({**setup.to_dict(), "run_id": run_id, "session_id": session_id}))

    def can_execute(self, setup: TradingSetup, now: datetime | None = None) -> tuple[bool, str | None]:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        expires = datetime.fromisoformat(setup.expires_at.replace("Z", "+00:00"))
        if moment > expires:
            return False, "SETUP_EXPIRED"
        row = self.database.get_setup(setup.setup_id)
        if row and row.get("executed_at_utc"):
            return False, "SETUP_ALREADY_EXECUTED"
        cooldown = self.database.latest_symbol_cooldown(setup.symbol)
        if cooldown and cooldown.get("cooldown_until_utc"):
            until = datetime.fromisoformat(str(cooldown["cooldown_until_utc"]).replace("Z", "+00:00"))
            if moment < until and cooldown.get("structure_id") == setup.structure_id:
                return False, "COOLDOWN_ACTIVE"
        return True, None

    def mark_executed(self, setup_id: str) -> None:
        self.database.mark_setup_executed(setup_id, utc_now_iso())

    def start_cooldown(
        self,
        symbol: str,
        outcome: str,
        structure_id: str,
        now: datetime | None = None,
    ) -> str:
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        config = self.symbols[symbol].get("cooldown", {})
        if outcome.upper() == "LOSS":
            minutes = int(config.get("after_loss_minutes", config.get("normal_minutes", 10)))
        elif outcome.upper() == "WIN":
            minutes = int(config.get("after_win_minutes", config.get("normal_minutes", 10)))
        else:
            minutes = int(config.get("normal_minutes", 10))
        until = (moment + timedelta(minutes=minutes)).isoformat()
        self.database.upsert_symbol_cooldown(symbol, structure_id, outcome.upper(), until)
        return until

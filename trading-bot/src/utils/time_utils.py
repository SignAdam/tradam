"""Timezone and trading-session helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class TimeWindow:
    name: str
    start: time
    end: time
    enabled: bool = True
    reason: str = ""


def parse_hhmm(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute))


def now_in_timezone(timezone_name: str) -> datetime:
    return datetime.now(ZoneInfo(timezone_name))


def is_time_in_window(moment: datetime, start: time, end: time) -> bool:
    current = moment.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def minutes_until_window_end(moment: datetime, end: time) -> int:
    end_dt = moment.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if end_dt < moment:
        end_dt += timedelta(days=1)
    return int((end_dt - moment).total_seconds() // 60)


def session_state(config: dict, at: datetime | None = None) -> dict:
    display_timezone = config.get("display_timezone", config.get("timezone", "UTC"))
    if at is None:
        now_utc = datetime.now(timezone.utc)
    elif at.tzinfo is None:
        now_utc = at.replace(tzinfo=timezone.utc)
    else:
        now_utc = at.astimezone(timezone.utc)
    display_moment = now_utc.astimezone(ZoneInfo(display_timezone))
    active_session = None
    session_moment: datetime | None = None
    reasons: list[str] = []
    close_positions = False
    allow_new_trades = False

    for name, session in config.get("sessions", {}).items():
        if not session.get("enabled", True):
            continue
        session_timezone = session.get("timezone", display_timezone)
        moment = now_utc.astimezone(ZoneInfo(session_timezone))
        start = parse_hhmm(session["start"])
        end = parse_hhmm(session["end"])
        if is_time_in_window(moment, start, end):
            minutes_left = minutes_until_window_end(moment, end)
            active_session = name
            session_moment = moment
            allow_new_trades = minutes_left > int(
                session.get("allow_new_trades_until_minutes_before_end", 0)
            )
            close_positions = minutes_left <= int(
                session.get("close_positions_before_end_minutes", 0)
            )
            if not allow_new_trades:
                reasons.append("Session close is too near for new trades")
            break

    for block in config.get("low_liquidity_blocks", []):
        if not block.get("enabled", True):
            continue
        block_timezone = block.get("timezone", display_timezone)
        moment = now_utc.astimezone(ZoneInfo(block_timezone))
        if block.get("weekday") is not None and int(block["weekday"]) != moment.weekday():
            continue
        if is_time_in_window(moment, parse_hhmm(block["start"]), parse_hhmm(block["end"])):
            reasons.append(block.get("reason", block.get("name", "Low liquidity block")))
            allow_new_trades = False

    return {
        "now": display_moment.isoformat(),
        "now_utc": now_utc.isoformat(),
        "now_display": display_moment.isoformat(),
        "session_local_time": session_moment.isoformat() if session_moment else None,
        "session": active_session,
        "active": active_session is not None,
        "allow_new_trades": bool(active_session and allow_new_trades and not reasons),
        "close_positions": close_positions,
        "blocked": bool(reasons),
        "reasons": reasons,
    }


def clock_snapshot(
    sessions_config: dict,
    at: datetime | None = None,
    broker_utc_offset_minutes: int | None = None,
) -> dict[str, str | None]:
    now_utc = (at or datetime.now(timezone.utc))
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    now_utc = now_utc.astimezone(timezone.utc)
    display_name = sessions_config.get("display_timezone", sessions_config.get("timezone", "UTC"))
    offset = (
        broker_utc_offset_minutes
        if broker_utc_offset_minutes is not None
        else sessions_config.get("broker_utc_offset_minutes")
    )
    broker_time = now_utc + timedelta(minutes=int(offset)) if offset is not None else None
    return {
        "utc": now_utc.isoformat(),
        "display_timezone": display_name,
        "display": now_utc.astimezone(ZoneInfo(display_name)).isoformat(),
        "broker": broker_time.isoformat() if broker_time else None,
        "broker_utc_offset_minutes": str(offset) if offset is not None else None,
    }

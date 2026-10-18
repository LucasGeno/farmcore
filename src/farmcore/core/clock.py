"""One clock for every process: live (wall time) or replay (a stored "now").

Replay state lives in the single `clock_state` row (id 1), so the web process, the bot process
and the CLI share one "now". The row is created from settings (`clock_mode`, `replay_start`
local) the first time it is read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from fastapi import Depends, HTTPException, Request
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from farmcore.core.models.kernel import ClockState
from farmcore.db import get_session
from farmcore.settings import Settings, get_settings

CLOCK_ROW_ID = 1


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, UTC, timezone-aware."""
        ...


class LiveClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ReplayClock:
    def __init__(self, now: datetime) -> None:
        self._now = _as_utc(now)

    def now(self) -> datetime:
        return self._now


def _as_utc(t: datetime) -> datetime:
    if t.tzinfo is None:
        raise ValueError(f"expected an aware datetime, got naive {t!r}")
    return t.astimezone(UTC)


def parse_local(s: str, tz: str) -> datetime:
    """Local wall time `YYYY-MM-DD HH:MM` (or `YYYY-MM-DDTHH:MM`) in `tz` -> aware UTC."""
    try:
        t = datetime.fromisoformat(s.strip())
    except ValueError as exc:
        raise ValueError(f"expected local time 'YYYY-MM-DD HH:MM', got {s!r}") from exc
    if t.tzinfo is None:
        t = t.replace(tzinfo=ZoneInfo(tz))
    return t.astimezone(UTC)


def clock_row(session: Session, settings: Settings | None = None) -> ClockState:
    """The `clock_state` row, created from settings if missing (flushed, not committed)."""
    row = session.get(ClockState, CLOCK_ROW_ID)
    if row is not None:
        return row
    if session.info.get("tour"):
        raise HTTPException(status_code=403, detail="Tour mode cannot initialize the clock.")
    settings = settings or get_settings()
    start = settings.replay_start
    if start.tzinfo is None:
        start = start.replace(tzinfo=ZoneInfo(settings.display_tz))
    session.execute(
        insert(ClockState)
        .values(id=CLOCK_ROW_ID, mode=settings.clock_mode, replay_now=_as_utc(start), job_runs={})
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return session.get(ClockState, CLOCK_ROW_ID, populate_existing=True)


def get_clock(session: Session, settings: Settings | None = None) -> Clock:
    row = clock_row(session, settings)
    if row.mode == "live" or row.replay_now is None:
        return LiveClock()
    return ReplayClock(row.replay_now)


def clock_dep(request: Request, session: Session = Depends(get_session)) -> Clock:
    """FastAPI dependency: the shared clock, read through the request's session."""
    return get_clock(session, request.app.state.settings)


def set_replay(session: Session, t: datetime, settings: Settings | None = None) -> None:
    """Switch to replay mode at `t` (aware). The caller commits."""
    row = clock_row(session, settings)
    row.mode = "replay"
    row.replay_now = _as_utc(t)
    session.flush()


def set_live(session: Session, settings: Settings | None = None) -> None:
    """Switch to live mode (replay_now is kept for the next replay). The caller commits."""
    clock_row(session, settings).mode = "live"
    session.flush()

"""Farm-scoped availability days. No row means available; `available=false` marks a Busy day.

The day is a local calendar date in the installation's display timezone, matching the forecast
plan. Availability is ordinary farm state shared by every signed-in device, not a browser
preference: reads are audited through `ScopedRepository` and writes require `planning:write`.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from farmcore.apps.forecast.models import ForecastAvailability
from farmcore.core.access import ScopedRepository

SCOPE = "fields:read"
WRITE_SCOPE = "planning:write"


def busy_days(repo: ScopedRepository, today: date, *, member: bool) -> set[date]:
    """Local Busy days from `today` on, for a farm member. Non-members see none."""
    if not member:
        return set()
    rows = repo.list(
        ForecastAvailability,
        ForecastAvailability.available.is_(False),
        ForecastAvailability.day >= today,
        scope=SCOPE,
        order_by=ForecastAvailability.day,
    )
    return {row.day for row in rows}


def day_state(repo: ScopedRepository, day: date) -> dict[str, object]:
    """The availability shown for a planned start day."""
    rows = repo.list(
        ForecastAvailability,
        ForecastAvailability.day == day,
        scope=SCOPE,
        limit=1,
    )
    available = rows[0].available if rows else True
    return {"day": day.isoformat(), "available": available}


def set_day(
    repo: ScopedRepository,
    day: date,
    *,
    available: bool,
    notes: str = "",
    now: datetime,
) -> None:
    """Record an explicit Free/Busy choice for one farm day, replacing an earlier one."""
    repo.check(WRITE_SCOPE, "forecast_availability")
    repo.session.execute(
        insert(ForecastAvailability)
        .values(
            farm_id=repo.farm_id,
            day=day,
            available=available,
            notes=notes,
            created_by=repo.actor,
            created_at=now,
            updated_by=repo.actor,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[ForecastAvailability.farm_id, ForecastAvailability.day],
            set_={
                "available": available,
                "notes": notes,
                "updated_by": repo.actor,
                "updated_at": now,
            },
        )
    )
    repo.session.flush()


def reset(session, farm) -> None:
    """Demo reset clears the demo farm's shared availability days."""
    session.execute(delete(ForecastAvailability).where(ForecastAvailability.farm_id == farm.id))

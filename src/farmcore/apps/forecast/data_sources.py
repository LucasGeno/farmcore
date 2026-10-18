"""Imported forecast runs available at the replay clock; prediction hours may be later."""

from sqlalchemy import func, select

from farmcore.apps.forecast.models import ForecastRun
from farmcore.apps.forecast.store import freshness
from farmcore.core.data_sources import SourceSummary


def summary(repo, now, parcel_id=None):
    count, last = repo.execute(
        select(func.count(ForecastRun.id), func.max(ForecastRun.available_at)).where(
            ForecastRun.farm_id == repo.farm_id,
            ForecastRun.available_at <= now,
            ForecastRun.issued_at <= now,
        ),
        scope="stations:read",
        resource="forecast_run",
    ).one()
    health = freshness(repo, now)
    notes = ["Farm-wide forecast · time is when the run became available."]
    if health["receipt"]:
        notes.append(f"Fetched {health['fetched']} · provider issue time unknown.")
        notes.append(f"Coverage ends {health['coverage_end']}.")
    if health["stale"]:
        notes.append("Stale forecast.")
    if health["missing"]:
        notes.append("Partial forecast inputs: " + ", ".join(health["missing"]) + " hours.")
    if health["failed"]:
        notes.append("Live forecast refresh unavailable; last valid forecast retained.")
    return SourceSummary(
        "forecast",
        "Forecast receipts" if health["receipt"] else "Imported forecast",
        "forecast runs",
        count,
        last,
        " ".join(notes),
    )

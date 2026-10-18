"""Imported forecast runs available at the replay clock; prediction hours may be later."""

from sqlalchemy import func, select

from farmcore.apps.forecast.models import ForecastRun
from farmcore.core.data_sources import SourceSummary


def summary(repo, now, parcel_id=None):
    count, last = repo.execute(
        select(func.count(ForecastRun.id), func.max(ForecastRun.available_at)).where(
            ForecastRun.farm_id == repo.farm_id, ForecastRun.available_at <= now
        ),
        scope="stations:read",
        resource="forecast_run",
    ).one()
    return SourceSummary(
        "forecast",
        "Imported forecast",
        "forecast runs",
        count,
        last,
        "Farm-wide forecast · time is when the run became available.",
    )

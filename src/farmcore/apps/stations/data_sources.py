"""Observed station values available by the replay clock, never future replay weather."""

from sqlalchemy import func, select

from farmcore.apps.stations.service import station_for_farm
from farmcore.core.data_sources import SourceSummary
from farmcore.core.models.canonical import Asset, Observation


def summary(repo, now, parcel_id=None):
    station = station_for_farm(repo)
    count, last = 0, None
    if station and station.props.get("archive_only"):
        archive = station.props["archive"]
        return SourceSummary(
            "stations",
            station.props["provider"],
            "half-hour archive rows",
            archive["rows"],
            note=f"{archive['first']} — {archive['last']} · {archive['notice']}",
        )
    if station:
        count, last = repo.execute(
            select(func.count(Observation.id), func.max(Observation.ts))
            .join(Asset, Asset.id == Observation.asset_id)
            .where(Asset.farm_id == repo.farm_id, Asset.id == station.id, Observation.ts <= now),
            scope="stations:read",
            resource="observation",
        ).one()
    return SourceSummary(
        "stations",
        "Météo-France station",
        "observation values",
        count,
        last,
        "Farm-wide station" + (f" · {station.name}" if station else ""),
    )

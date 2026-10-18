"""Source receipts retain their original parcel ids and replay availability after merges.

The saved-weather envelope records these even when weather was unavailable. Legacy receipts
without it cannot establish historical provenance: report unknown instead of using the merged
log's current time/parcel union or mistaking wall-time received_at for a replay sync.
"""

from datetime import datetime

from sqlalchemy import select

from farmcore.apps.operations import spray_weather
from farmcore.core.data_sources import SourceSummary
from farmcore.core.models.canonical import Log, LogParcel, LogSource


def receipt_summary(repo, now, parcel_id, *, source_type, key, label, unit, note):
    # Project only the small evidence envelope, never transcripts or the rest of the payload.
    stmt = (
        select(LogSource.raw_payload[spray_weather.NAMESPACE], Log.ts_start, LogSource.received_at)
        .join(Log, Log.id == LogSource.log_id)
        .where(
            Log.farm_id == repo.farm_id, Log.status == "done", LogSource.source_type == source_type
        )
    )
    if parcel_id is not None:
        stmt = stmt.where(
            select(LogParcel.log_id)
            .where(LogParcel.log_id == Log.id, LogParcel.parcel_id == parcel_id)
            .exists()
        )
    count, last, uncertain = 0, None, False
    for saved, started, received in repo.execute(stmt, scope="logs:read", resource="log_source"):
        entry = spray_weather.project(source_type, {spray_weather.NAMESPACE: saved})
        if entry.get("status") == spray_weather.UNSUPPORTED or not entry.get("data_clock"):
            uncertain |= started <= now or received <= now
            continue
        available = datetime.fromisoformat(entry["data_clock"])
        if available > now or (parcel_id is not None and parcel_id not in entry["parcel_ids"]):
            continue
        count += 1
        last = max(last, available) if last else available
    if uncertain:
        count, last = None, None
        note += " Older receipts lack original replay timing or parcel attribution; totals unknown."
    return SourceSummary(key, label, unit, count, last, note)


def summary(repo, now, parcel_id=None):
    return receipt_summary(
        repo,
        now,
        parcel_id,
        source_type="isoxml",
        key="operations",
        label="ISOXML machine files",
        unit="machine records",
        note="One record per task receipt, not per file.",
    )

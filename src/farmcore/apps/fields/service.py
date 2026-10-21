"""Parcel GeoJSON for the map: each parcel with its latest advice and last spray, both bounded
by the clock (`now`), so stepping the replay clock back hides later advice and logs."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from farmcore.apps.fields.seed import alias_number
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Log, LogParcel, Parcel

NO_ADVICE = "none"
CARD_URL = "/advisor/parcels/{id}"
# Map fill and legend, one place: decision -> (label, colour). N1 traffic lights, the shell's
# tokens: spray red, wait amber, protected green, none grey; each carries white text at >= 4.5:1.
# The icons are the core ones named after the decision (core/_icons.html). "No action" is an
# evaluated parcel with nothing to spray; a parcel with no advice row up to now also maps to
# `none` and says "No advice yet" (`advised` false).
DECISION_STYLES: dict[str, tuple[str, str]] = {
    "spray": ("Spray", "#b91c1c"),
    "wait": ("Wait", "#a45a00"),
    "protected": ("Protected", "#15803d"),
    NO_ADVICE: ("No action", "#4b5563"),
}
# Sunlight mode (N6): the same hues, darker, each >= 7:1 against white.
HIGH_CONTRAST_STYLES: dict[str, tuple[str, str]] = {
    "spray": ("Spray", "#8f0000"),
    "wait": ("Wait", "#6b3a00"),
    "protected": ("Protected", "#064a1f"),
    NO_ADVICE: ("No action", "#1f2937"),
}
NOT_ADVISED_LABEL = "No advice yet"


def styles_for(request) -> dict[str, tuple[str, str]]:
    """The map/legend colours for this viewer: the strong set with the Sunlight cookie."""
    return HIGH_CONTRAST_STYLES if request.cookies.get("sunlight") == "1" else DECISION_STYLES


def _last_sprays(repo: ScopedRepository, now: datetime) -> dict[int, datetime]:
    """parcel id -> `ts_start` of its latest done spray with `ts_start <= now`."""
    stmt = (
        select(LogParcel.parcel_id, func.max(Log.ts_start))
        .join(Log, Log.id == LogParcel.log_id)
        .where(
            Log.farm_id == repo.farm_id,
            Log.type == "spray",
            Log.status == "done",
            Log.ts_start <= now,
        )
        .group_by(LogParcel.parcel_id)
    )
    rows = repo.execute(stmt, scope="logs:read", resource="log")
    return {parcel_id: ts for parcel_id, ts in rows}


def parcels_feature_collection(
    repo: ScopedRepository, now: datetime, *, tz: str = "Europe/Paris"
) -> dict[str, Any]:
    """The farm's parcels as a GeoJSON FeatureCollection, ordered P1..P10.

    Properties: id, alias, name, area_ha, variety, decision (`none` without advice up to now),
    advised (an advice row exists up to now), last_spray (local date `YYYY-MM-DD` in `tz`, or
    None), card_url, label_point [lon, lat].
    """
    point = func.ST_PointOnSurface(Parcel.geom)
    stmt = select(
        Parcel,
        func.ST_AsGeoJSON(Parcel.geom).label("geometry"),
        func.ST_X(point).label("label_x"),
        func.ST_Y(point).label("label_y"),
    ).where(Parcel.farm_id == repo.farm_id)
    rows = repo.execute(stmt, scope="fields:read", resource="parcel").all()
    from farmcore.apps.advisor.service import latest_advice

    assessments = latest_advice(repo, now)
    decisions = {pid: row.decision for pid, row in assessments.items()}
    sprays = _last_sprays(repo, now)
    zone = ZoneInfo(tz)
    features = []
    for parcel, geometry, label_x, label_y in sorted(
        rows, key=lambda r: (alias_number(r[0].alias), r[0].alias)
    ):
        last = sprays.get(parcel.id)
        features.append(
            {
                "type": "Feature",
                "id": parcel.id,
                "geometry": json.loads(geometry),
                "properties": {
                    "id": parcel.id,
                    "alias": parcel.alias,
                    "name": parcel.name,
                    "area_ha": float(parcel.area_ha),
                    "variety": parcel.props.get("variety"),
                    "decision": decisions.get(parcel.id, NO_ADVICE),
                    "advised": parcel.id in decisions,
                    "assessment_status": (
                        (assessments[parcel.id].props or {}).get(
                            "assessment_status", "legacy-unknown"
                        )
                        if parcel.id in assessments
                        else "unavailable"
                    ),
                    "last_spray": last.astimezone(zone).date().isoformat() if last else None,
                    "card_url": CARD_URL.format(id=parcel.id),
                    "label_point": [label_x, label_y],
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}

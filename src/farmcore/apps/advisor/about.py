"""`GET /advisor/about`: what the advice is built from, in plain words.

Everything shown is read from stored data and the engine's own config (rule sources, assumed
flags); nothing is computed, so the page cannot disagree with the advice. Station and forecast
dates are bounded by the clock's now, like every other read.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select

from farmcore.apps.advisor import service
from farmcore.apps.advisor.engine.config import GOIDANICH_TABLE, AdvisorConfig
from farmcore.apps.advisor.routes import PARCEL_SCOPE, _now
from farmcore.apps.forecast.models import ForecastRun
from farmcore.apps.stations.service import station_for_farm
from farmcore.core.access import Repo
from farmcore.core.models.canonical import Asset, ExtId, Farm, Observation, Parcel, Source
from farmcore.web import render

router = APIRouter()

# Plain-language places for the regions the config knows; any other region shows its code only.
REGIONS = {
    "FRI12": "Saint-Émilion, Bordeaux (Gironde, NUTS-3 FRI12)",
    "ES418": "Valladolid, Ribera del Duero (NUTS-3 ES418)",
}
# The station network named by the station's own external-id source.
NETWORKS = {"meteofrance": "Météo-France", "inforiego": "InfoRiego · ITACyL"}
# The honest validation status, verbatim from the night-3 task.
VALIDATION = (
    "replayed on the 2024 and 2025 seasons of one farm against a calendar programme; "
    "not yet checked against observed disease records or other regions"
)


def _date(ts: datetime | str | None, zone: ZoneInfo) -> str | None:
    """A date in plain words. A text stamp is a source label with no verified time basis (the
    Spanish archive): its date is shown as written, with no zone conversion."""
    if ts is None:
        return None
    local = datetime.fromisoformat(ts) if isinstance(ts, str) else ts.astimezone(zone)
    return f"{local.day} {local:%b %Y}"


def date_range(first: str | None, last: str | None) -> str | None:
    """ "A to B", or just "A" when both ends are the same date."""
    if first is None or last is None:
        return first or last
    return first if first == last else f"{first} to {last}"


def _first_sentence(text: str) -> str:
    return text.split(". ", 1)[0].rstrip(".")


def _source_label(repo, parcels, station) -> dict[str, Any]:
    """The parcel layer's and the station network's own source rows, from their ext ids."""
    ids = [p.id for p in parcels]
    parcel_src = (
        repo.execute(
            select(ExtId.source).where(ExtId.model == "parcel", ExtId.res_id.in_(ids)).limit(1),
            scope=PARCEL_SCOPE,
            resource="ext_id",
        ).scalar()
        if ids
        else None
    )
    station_ext = (
        repo.execute(
            select(ExtId.source, ExtId.key)
            .where(ExtId.model == "asset", ExtId.res_id == station.id)
            .limit(1),
            scope="stations:read",
            resource="ext_id",
        ).first()
        if station
        else None
    )

    def row(code):
        return (
            repo.execute(
                select(Source.name, Source.licence).where(Source.code == code),
                scope=PARCEL_SCOPE,
                resource="source",
            ).first()
            if code
            else None
        )

    layer = row(parcel_src)
    network_src = station_ext[0] if station_ext else None
    network = NETWORKS.get(network_src) or (row(network_src) or [None])[0]
    label = None
    if layer:
        year = re.search(r"\d{4}", parcel_src or "")
        if parcel_src.startswith("ign-rpg"):
            label = f"RPG {year.group() if year else ''} (IGN)".replace("  ", " ")
        elif parcel_src.startswith("sigpac"):
            label = _first_sentence(layer.licence)
        else:
            label = layer.name
    return {
        "parcel_label": label,
        "network": network,
        "station_key": station_ext[1] if station_ext else None,
    }


def _cite(source: str) -> str:
    """The rule source without the repo-internal research-note pointer."""
    cleaned = re.sub(r";?\s*research/agronomy\.md\s*§\w+", "", source)
    return re.sub(r":\s*index bounded.*$", "", cleaned).strip()


def _assumed_counts(config: AdvisorConfig) -> tuple[int, int]:
    names = [*config.threshold_names(), *config.nested_threshold_names()]
    return sum(bool(config.threshold(n).assumed) for n in names), len(names)


def build(repo, now: datetime, tz: str) -> dict[str, Any]:
    zone = ZoneInfo(tz)
    farm = repo.get(Farm, repo.farm_id, scope=service.SCOPE)
    from farmcore.apps.review.service import resolve

    effective = resolve(repo, now, tz)
    config = effective.config
    unavailable = service.current_availability(repo, now, tz)[1]
    parcels = repo.list(Parcel, scope=PARCEL_SCOPE)
    varieties = sorted({p.props.get("variety") for p in parcels if (p.props or {}).get("variety")})

    station = station_for_farm(repo)
    first = last = None
    archive = (station.props or {}).get("archive") if station else None
    archive_range = (
        date_range(_date(archive["first"], zone), _date(archive["last"], zone)) if archive else None
    )
    if station and not (station.props or {}).get("archive_only"):
        first, last = repo.execute(
            select(func.min(Observation.ts), func.max(Observation.ts))
            .join(Asset, Asset.id == Observation.asset_id)
            .where(Asset.farm_id == repo.farm_id, Asset.id == station.id, Observation.ts <= now),
            scope="stations:read",
            resource="observation",
        ).one()
    runs, issued_first, available_last, label = repo.execute(
        select(
            func.count(ForecastRun.id),
            func.min(ForecastRun.issued_at),
            func.max(ForecastRun.available_at),
            func.max(ForecastRun.props["label"].astext),
        ).where(ForecastRun.farm_id == repo.farm_id, ForecastRun.available_at <= now),
        scope="stations:read",
        resource="forecast_run",
    ).one()
    labels = _source_label(repo, parcels, station)
    synthetic = labels["parcel_label"] is None and bool(parcels)
    assumed, total = _assumed_counts(config)
    return {
        "farm_name": farm.name,
        "region_code": config.region,
        "region": REGIONS.get(config.region, config.region),
        "varieties": varieties,
        "unavailable": unavailable,
        "station": {
            "name": station.name if station else None,
            "network": labels["network"],
            "code": labels["station_key"],
            "range": date_range(_date(first, zone), _date(last, zone)),
            "archive": archive_range,
        },
        "forecast": {
            "runs": runs,
            "range": date_range(_date(issued_first, zone), _date(available_last, zone)),
            "label": label,
        },
        "parcels": {
            "count": len(parcels),
            "synthetic": synthetic,
            "label": labels["parcel_label"],
        },
        "models": [
            {
                "key": "downy",
                "label": "Downy mildew",
                "rules": [
                    {
                        "name": "3-10 rule",
                        "what": (
                            f"Infection risk when about {config.downy_rain_min_mm.value:g} mm "
                            f"of rain falls within {config.downy_window_h.value} h "
                            f"at a mean temperature of {config.downy_temp_min_c.value:g} °C "
                            "or more."
                        ),
                        "source": _cite(config.threshold("downy_rain_min_mm").source),
                    },
                    {
                        "name": "Incubation (Goidanich table)",
                        "what": "After an infection the disease develops with temperature and "
                        "humidity; the days until symptoms are tracked hourly.",
                        "source": _cite(GOIDANICH_TABLE.source),
                    },
                ],
            },
            {
                "key": "powdery",
                "label": "Powdery mildew",
                "rules": [
                    {
                        "name": "Gubler–Thomas risk index",
                        "what": "A 0–100 index from warm hours; higher risk means a shorter "
                        "interval between sulphur passes.",
                        "source": _cite(config.threshold("gt_index_min").source),
                    }
                ],
            },
        ],
        "assumed": assumed,
        "thresholds": total,
        "validation": VALIDATION,
    }


@router.get("/about", response_class=HTMLResponse)
def about(request: Request, repo: Repo) -> HTMLResponse:
    tz = request.app.state.settings.display_tz
    return render(request, "advisor/about.html", **build(repo, _now(request, repo), tz))

"""Read the original VA07 daily labels, with no timestamp or weather inference."""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request

from farmcore.apps.demo_es import archive
from farmcore.core.access import Repo
from farmcore.core.models.canonical import Asset
from farmcore.core.registry import visible_manifests
from farmcore.web import render

router = APIRouter()


@router.get("/archive/{station_id}")
def archive_page(
    station_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
    request: Request,
    repo: Repo,
    day: date | None = None,
):
    if not any(m.name == "demo_es" for m in visible_manifests(request)):
        raise HTTPException(status_code=404, detail="Archive app is not enabled.")
    station = repo.get(Asset, station_id, scope="stations:read")
    if station is None or station.kind != "station" or not station.props.get("archive_only"):
        raise HTTPException(status_code=404, detail="No archive station on this farm.")
    if day is None:
        day = date.fromisoformat(station.props["archive"]["first"][:10])
    try:
        rows = archive.read(request.app.state.settings.data_dir / archive.FILE)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=503, detail="VA07 source archive file is unavailable."
        ) from exc
    readings = [row for row in rows if row["Fecha (AAAA-MM-DD)"] == day.isoformat()]
    return render(
        request,
        "demo_es/archive.html",
        station=station,
        archive=station.props["archive"],
        archive_url=None,
        day=day.isoformat(),
        readings=readings,
        variables=archive.VARIABLES,
    )

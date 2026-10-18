"""Read-only source panels composed only from visible apps' declared providers."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from farmcore.core import registry
from farmcore.core.access import Repo
from farmcore.core.clock import get_clock
from farmcore.core.manifest import import_string
from farmcore.core.models.canonical import Parcel
from farmcore.web import render

router = APIRouter()


def _panel(request: Request, repo: Repo, parcel_id: int | None = None) -> HTMLResponse:
    if parcel_id is not None and repo.get(Parcel, parcel_id, scope="fields:read") is None:
        raise HTTPException(status_code=404, detail="No such parcel on this farm.")
    now = get_clock(repo.session, request.app.state.settings).now()
    rows = [
        import_string(m.data_sources)(repo, now, parcel_id)
        for m in registry.visible_manifests(request)
        if m.data_sources
    ]
    return render(request, "core/_data_sources.html", sources=rows)


@router.get("/data-sources/", response_class=HTMLResponse)
def sources_page(request: Request) -> HTMLResponse:
    return render(request, "core/sources.html")


@router.get("/data-sources/today", response_class=HTMLResponse)
def today_sources(request: Request, repo: Repo) -> HTMLResponse:
    return _panel(request, repo)


@router.get("/data-sources/parcels/{parcel_id}", response_class=HTMLResponse)
def parcel_sources(parcel_id: int, request: Request, repo: Repo) -> HTMLResponse:
    return _panel(request, repo, parcel_id)

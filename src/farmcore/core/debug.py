"""Debug mode: `?debug=1` opens a drawer with the machinery behind a page, for admins only.

- `debug_enabled(request)`: admin and (`?debug=1`, or cookie `farmcore_debug=1` and not
  `?debug=0`). The advice card and the app panels share it.
- `base.html` includes `core/_debug_drawer.html`; without `debug_view` in the context it renders
  the self-loading slot `core/_debug_slot.html`, which loads
  `GET /debug/drawer?path=<page path and query, minus debug>&debug=<page ?debug>` once (the
  `/clock` pattern).
- `GET /debug/drawer` sets (`?debug=1`) or clears (`?debug=0`) the session cookie on its
  response, for admins only. Non-admins get an empty body and no cookie; admins with debug off
  a small "Debug" link to the page with `debug=1` (its other query parameters kept); admins with
  debug on the drawer: request (path, page parcel id), access (farm: the page's `?farm=` when
  it names a selectable farm, else `selected_farm`; grant, scopes, actor, purpose), clock (mode,
  now, job runs), the loaded manifests, raw JSON links, each app's `debug_panels` URLs as
  self-loading slots, the last 50 `access_log` rows and a link to the full `/chat/` LLM payload
  view. A panel or raw link with `{id}` loads only on its own app's `/<app>/…/parcels/{id}`
  pages, `{id}` filled from the path; panels and raw links carry the page's `?farm=`.

The core never imports app code: panels are URLs, manifest fields are read with `getattr`.
Every debug route is a GET that writes nothing but the `access_log` rows of its reads.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse, Response

from farmcore.core.access import (
    DEMO_SOURCE,
    FARM_PARAM,
    OWNER,
    FarmNotSeeded,
    ScopedRepository,
    ensure_owner_grant,
    selectable_farm,
    selected_farm,
)
from farmcore.core.clock import clock_row, get_clock
from farmcore.core.models.canonical import AccessLog, ExtId, Farm
from farmcore.core.registry import visible_manifests
from farmcore.core.routes_farms import safe_next
from farmcore.db import get_session
from farmcore.web import is_admin, render

router = APIRouter()

DEBUG_COOKIE = "farmcore_debug"
DEBUG_PARAM = "debug"
ACTOR = OWNER
PURPOSE = "debug"
ACCESS_LOG_ROWS = 50
_PARCEL_RE = re.compile(r"/parcels/(\d+)(?:[/.]|$)")
# Raw JSON views, shown when the app that serves them is loaded: app -> (label, url); `{id}`
# needs a parcel id on one of that app's own pages.
RAW_LINKS: dict[str, tuple[tuple[str, str], ...]] = {
    "fields": (("Parcels GeoJSON", "/fields/parcels.geojson"),),
    "advisor": (("Advice JSON", "/advisor/parcels/{id}.json"),),
    "operations": (("Register JSON", "/operations/register.json"),),
    "kpi": (("KPIs JSON", "/kpi/kpis.json"),),
}


def debug_enabled(request: Request) -> bool:
    """Admin and (`?debug=1`, or cookie `farmcore_debug=1` and not `?debug=0`)."""
    if not is_admin(request):
        return False
    q = request.query_params.get(DEBUG_PARAM)
    return q == "1" or (q != "0" and request.cookies.get(DEBUG_COOKIE) == "1")


def page_parcel_id(path: str) -> int | None:
    """The parcel id of a `/…/parcels/{id}` page, else None."""
    match = _PARCEL_RE.search(path)
    return int(match.group(1)) if match else None


def app_parcel_id(app: str, path: str) -> int | None:
    """The parcel id for app `app`'s `{id}` URLs: only on its own pages (`/<app>/…`), so the
    advisor panel loads on `/advisor/parcels/{id}` but not on `/operations/parcels/{id}`."""
    return page_parcel_id(path) if path.startswith(f"/{app}/") else None


def split_page(page: str) -> tuple[str, list[tuple[str, str]]]:
    """A local page URL -> its path and its query pairs, `debug` dropped."""
    parts = urlsplit(safe_next(page))
    query = parse_qsl(parts.query, keep_blank_values=True)
    return parts.path or "/", [(k, v) for k, v in query if k != DEBUG_PARAM]


def toggle_url(path: str, query: list[tuple[str, str]], value: str) -> str:
    """The page with `debug=<value>`, its other query parameters kept."""
    return f"{path}?{urlencode([*query, (DEBUG_PARAM, value)])}"


def page_farm(session: Session, query: list[tuple[str, str]]) -> Farm | None:
    """The farm the page's `?farm=` names, if it is selectable."""
    for key, value in query:
        if key == FARM_PARAM:
            return selectable_farm(session, value)
    return None


def with_farm(url: str, farm: Farm | None) -> str:
    """`url` with `farm=<id>` appended when the page chose a farm by query."""
    if farm is None:
        return url
    return f"{url}{'&' if '?' in url else '?'}{urlencode({FARM_PARAM: farm.id})}"


def fill(urls: list[str] | tuple[str, ...], parcel_id: int | None) -> list[str]:
    """`{id}` filled from the page parcel id; URLs needing `{id}` are dropped without one."""
    out = []
    for url in urls:
        if "{id}" in url:
            if parcel_id is None:
                continue
            url = url.replace("{id}", str(parcel_id))
        out.append(url)
    return out


def _manifest_view(m: Any) -> dict[str, Any]:
    return {
        "name": m.name,
        "version": m.version,
        "maturity": getattr(m, "maturity", ""),
        "nav_group": getattr(m, "nav_group", None),
        "today": len(getattr(m, "widgets", ())),
        "debug_panels": len(getattr(m, "debug_panels", ())),
        "tools": len(getattr(m, "tools", ())),
    }


def _access_view(repo: ScopedRepository) -> dict[str, Any]:
    keys = repo.execute(
        select(ExtId.source, ExtId.key)
        .where(ExtId.model == "farm", ExtId.res_id == repo.farm_id)
        .order_by(ExtId.source != DEMO_SOURCE, ExtId.id),
        scope="audit:read",
        resource="ext_id",
    ).all()
    rows = repo.execute(
        select(AccessLog)
        .where(AccessLog.farm_id == repo.farm_id)
        .order_by(AccessLog.id.desc())
        .limit(ACCESS_LOG_ROWS),
        scope="audit:read",
        resource="access_log",
    ).scalars()
    return {
        "farm_id": repo.farm_id,
        "farm_keys": [f"{s}:{k}" for s, k in keys],
        "grant_id": repo.grant.id,
        "grantee": repo.grant.grantee,
        "scopes": list(repo.grant.scopes),
        "actor": repo.actor,
        "purpose": repo.purpose,
        "access_log": [
            {
                "id": r.id,
                "ts": r.ts,
                "actor": r.actor,
                "grant_id": r.grant_id,
                "resource": r.resource,
                "scope": r.scope,
                "purpose": r.purpose,
            }
            for r in rows
        ],
    }


def _clock_view(request: Request, session: Session) -> dict[str, Any]:
    settings = request.app.state.settings
    row = clock_row(session, settings)
    runs = row.job_runs or {}
    last_step = runs.get("last_step") or {}
    return {
        "mode": row.mode,
        "now": get_clock(session, settings).now(),
        "tz": settings.display_tz,
        "last_run": sorted((runs.get("last_run") or {}).items()),
        "last_step_to": last_step.get("to"),
        "last_step_runs": last_step.get("runs", []),
    }


def _drawer(
    request: Request, session: Session, path: str, query: list[tuple[str, str]]
) -> HTMLResponse:
    parcel_id = page_parcel_id(path)
    manifests = list(getattr(request.app.state, "manifests", []))
    loaded = {m.name for m in manifests}
    chosen = page_farm(session, query)
    try:
        farm = chosen or selected_farm(request, session, request.app.state.settings)
    except FarmNotSeeded:
        access = None
    else:
        repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), ACTOR, PURPOSE)
        access = _access_view(repo)
    clock = _clock_view(request, session)
    session.commit()  # keeps the access_log rows of the reads above
    raw_links = [
        (label, with_farm(filled, chosen))
        for app, links in RAW_LINKS.items()
        if app in loaded
        for label, url in links
        for filled in fill((url,), app_parcel_id(app, path))
    ]
    panels = [
        {"app": m.name, "url": with_farm(url, chosen)}
        for m in visible_manifests(request)
        for url in fill(tuple(getattr(m, "debug_panels", ())), app_parcel_id(m.name, path))
    ]
    return render(
        request,
        "core/_debug_drawer.html",
        debug_view=True,
        debug_on=True,
        path=path,
        debug_off_url=toggle_url(path, query, "0"),
        parcel_id=parcel_id,
        access=access,
        clock=clock,
        apps=[_manifest_view(m) for m in manifests],
        raw_links=raw_links,
        panels=panels,
    )


@router.get("/debug/drawer", response_class=HTMLResponse)
def drawer(request: Request, path: str = "/", session: Session = Depends(get_session)) -> Response:
    if not is_admin(request):
        return HTMLResponse("")
    path, query = split_page(path)
    q = request.query_params.get(DEBUG_PARAM)
    if debug_enabled(request):
        response: Response = _drawer(request, session, path, query)
    else:
        response = render(
            request,
            "core/_debug_drawer.html",
            debug_view=True,
            debug_on=False,
            path=path,
            debug_on_url=toggle_url(path, query, "1"),
        )
    if q == "1":
        response.set_cookie(
            DEBUG_COOKIE,
            "1",
            path="/",
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
    elif q == "0":
        response.delete_cookie(
            DEBUG_COOKIE,
            path="/",
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
    return response

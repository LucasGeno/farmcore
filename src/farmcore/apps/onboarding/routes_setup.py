"""Onboarding steps after location: pre-fill crops, then offer app bundles by farm type.

`GET /onboarding/crops` shows every parcel with the crop its registry code names, the unknown
codes that stay unknown, and the region's common crops. `POST /onboarding/crops/assign` appends
crop seasons for the parcels the owner ticked (owner only). `GET /onboarding/bundles` is an app
grid with a search box; `POST /onboarding/bundles/{key}` switches a bundle's apps and their
dependencies on atomically (installation admin only). Animals are shown as coming later and
capture no data: there is no animal form or endpoint.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from farmcore.apps.onboarding import bundles, crops
from farmcore.core import registry
from farmcore.core.access import Repo, require_farm_owner
from farmcore.core.clock import get_clock
from farmcore.core.farm_location import time_zone_of
from farmcore.core.manifest import Manifest
from farmcore.core.routes_clock import require_admin
from farmcore.db import get_session
from farmcore.web import context_url, is_admin, render

router = APIRouter()

QUERY_MAX = 80


def _local_today(request: Request, repo: Repo) -> date:
    settings = request.app.state.settings
    now = get_clock(repo.session, settings).now()
    return now.astimezone(ZoneInfo(time_zone_of(repo.session, repo.farm_id, settings))).date()


def _crops_page(
    request: Request,
    repo: Repo,
    *,
    start: date | None = None,
    error: str | None = None,
    notice: str | None = None,
    status: int = 200,
) -> HTMLResponse:
    day = start or _local_today(request, repo)
    rows, country = crops.rows(repo, day)
    regional = crops.regional(country)
    regional_keys = {crop.key for crop in regional}
    response = render(
        request,
        "onboarding/crops.html",
        rows=rows,
        unknown=[row for row in rows if row.unknown_code],
        country=country,
        start=day.isoformat(),
        regional_crops=regional,
        other_crops=[crop for crop in crops.options(country) if crop.key not in regional_keys],
        error=error,
        notice=notice,
    )
    response.status_code = status
    return response


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        raise crops.CropError("Choose the date the crop season starts.") from None


@router.get("/crops", response_class=HTMLResponse)
def crops_page(
    request: Request,
    repo: Repo,
    assigned: int | None = None,
    covered: int | None = None,
) -> HTMLResponse:
    notice = None
    if assigned is not None:
        notice = f"Saved crops for {assigned} parcel(s)."
        if covered:
            notice += f" {covered} kept their existing crop."
    return _crops_page(request, repo, notice=notice)


@router.post("/crops/assign", dependencies=[Depends(require_farm_owner)])
async def assign_crops(
    request: Request, repo: Repo, starts_on: Annotated[str, Form()] = ""
) -> Response:
    form = await request.form()
    try:
        day = _parse_day(starts_on)
        chosen = [int(value) for value in form.getlist("parcel") if str(value).isdigit()]
        choices = {
            parcel_id: str(form.get(f"crop_{parcel_id}", "") or "").strip() for parcel_id in chosen
        }
        choices = {parcel_id: key for parcel_id, key in choices.items() if key}
        result = crops.assign(repo, starts_on=day, choices=choices)
    except crops.CropError as exc:
        return _crops_page(request, repo, start=_try_day(starts_on), error=str(exc), status=422)
    target = f"/onboarding/crops?assigned={result.assigned}&covered={len(result.covered)}"
    return RedirectResponse(context_url(request, target), status_code=303)


def _try_day(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except (AttributeError, ValueError):
        return None


def _ordered(request: Request) -> list[Manifest]:
    return list(getattr(request.app.state, "all_manifests", request.app.state.manifests))


def _bundle_cards(session: Session, request: Request, query: str) -> list[dict[str, Any]]:
    ordered = _ordered(request)
    states = registry.read_states(session, ordered)
    cards: list[dict[str, Any]] = []
    for bundle in bundles.BUNDLES:
        targets = registry.bundle_targets(ordered, bundle.apps)
        titles = [manifest.display_name for manifest in targets]
        if not bundles.matches(bundle, titles, query):
            continue
        installed = [
            m for m in targets if states.get(m.name, registry.INSTALLED) == registry.INSTALLED
        ]
        if targets and len(installed) == len(targets):
            status = "on"
        elif installed:
            status = "partial"
        else:
            status = "off"
        cards.append(
            {
                "key": bundle.key,
                "title": bundle.title,
                "summary": bundle.summary,
                "icon": bundle.icon,
                "apps": titles,
                "status": status,
            }
        )
    return cards


def _bundles_page(
    request: Request,
    session: Session,
    *,
    query: str = "",
    error: str | None = None,
    notice: str | None = None,
    status: int = 200,
) -> HTMLResponse:
    response = render(
        request,
        "onboarding/bundles.html",
        cards=_bundle_cards(session, request, query),
        q=query,
        error=error,
        notice=notice,
        admin=is_admin(request),
    )
    response.status_code = status
    return response


@router.get("/bundles", response_class=HTMLResponse)
def bundles_page(
    request: Request, session: Session = Depends(get_session), q: str = ""
) -> HTMLResponse:
    query = q.strip()[:QUERY_MAX]
    notice = None
    enabled = request.query_params.get("enabled")
    if enabled:
        found = bundles.bundle(enabled)
        if found is not None:
            notice = f"{found.title} apps are on."
    return _bundles_page(request, session, query=query, notice=notice)


@router.post("/bundles/{key}", dependencies=[Depends(require_admin)])
def choose_bundle(key: str, request: Request, session: Session = Depends(get_session)) -> Response:
    found = bundles.bundle(key)
    if found is None:
        raise HTTPException(status_code=404, detail="No such bundle.")
    ordered = _ordered(request)
    _, refusal = registry.switch_bundle(
        session, ordered, registry.read_states(session, ordered), found.apps
    )
    if refusal is not None:
        session.rollback()
        return _bundles_page(request, session, error=refusal.detail, status=409)
    session.commit()
    return RedirectResponse(context_url(request, f"/onboarding/bundles?enabled={key}"), 303)

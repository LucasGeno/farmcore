"""`GET /activity/`: the farm's grouped actions (`core.activity.activity_rows`) up to clock now,
newest first. Switched off on the Apps page → 404 at once (routes only drop at the next start)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Literal
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from farmcore.apps.activity import columns
from farmcore.core.access import Repo, selected_farm
from farmcore.core.activity import PAGE, ActivityFilters, ActivityType, activity_rows
from farmcore.core.clock import ReplayClock, get_clock
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.core.navigation import reading_destination, workspace_parent
from farmcore.core.routes_activity import live_apps
from farmcore.core.routes_farms import safe_next
from farmcore.core.urls import with_farm
from farmcore.web import render

router = APIRouter()
APP = "activity"
ONE_YEAR = 365 * 24 * 3600


def _require_on(request: Request) -> set[str]:
    apps = live_apps(request)
    if APP not in apps:
        raise HTTPException(status_code=404, detail="Not found.")
    return apps


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    repo: Repo,
    before: datetime | None = None,
    before_id: int = 0,
    parcel_id: Annotated[int | Literal[""] | None, Query()] = None,
    event_type: Annotated[ActivityType, Query(alias="type")] = "all",
    from_day: Annotated[date | Literal[""] | None, Query(alias="from")] = None,
    to_day: Annotated[date | Literal[""] | None, Query(alias="to")] = None,
) -> HTMLResponse:
    """`before` + `before_id` (from "Show older": the last row's time and newest event id): only
    rows after that row in the feed's order; without `before_id`, rows strictly older than
    `before`. A bad value is a 422."""
    context = _context(
        request,
        repo,
        _require_on(request),
        before,
        before_id,
        parcel_id,
        event_type,
        from_day,
        to_day,
    )
    return render(
        request,
        "activity/_feed.html" if request.headers.get("hx-request") else "activity/index.html",
        **context,
    )


@router.get("/parcels/{parcel_id}/workspace", response_class=HTMLResponse)
def workspace(
    request: Request,
    repo: Repo,
    parcel_id: int,
    before: datetime | None = None,
    before_id: int = 0,
    event_type: Annotated[ActivityType, Query(alias="type")] = "all",
    from_day: Annotated[date | Literal[""] | None, Query(alias="from")] = None,
    to_day: Annotated[date | Literal[""] | None, Query(alias="to")] = None,
) -> HTMLResponse:
    apps = _require_on(request)
    if not 1 <= parcel_id < 2**31 or repo.get(Parcel, parcel_id, scope="fields:read") is None:
        raise HTTPException(status_code=404, detail="No such parcel on this farm.")
    parent = workspace_parent(request, repo, parcel_id, "activity")
    context = _context(
        request,
        repo,
        apps,
        before,
        before_id,
        parcel_id,
        event_type,
        from_day,
        to_day,
        parent=parent,
        fixed=True,
    )
    polling = request.headers.get("HX-Trigger") == "activity-feed"
    response = render(
        request, "activity/_feed.html" if polling else "activity/_workspace.html", **context
    )
    return response


def _context(
    request,
    repo,
    apps,
    before,
    before_id,
    parcel_id,
    event_type,
    from_day,
    to_day,
    *,
    parent=None,
    fixed=False,
):
    """One grouped feed presenter for the farm page and checked parcel provider."""
    settings = request.app.state.settings
    clock = get_clock(repo.session, settings)
    now = clock.now()
    parcel_id = None if parcel_id == "" else parcel_id
    from_day, to_day = from_day or None, to_day or None
    if parcel_id is not None and not 1 <= parcel_id <= 2**63 - 1:
        raise HTTPException(status_code=422, detail="Choose a valid parcel.")
    unavailable_parcel = (
        parcel_id is not None and repo.get(Parcel, parcel_id, scope="fields:read") is None
    )
    if from_day and to_day and from_day > to_day:
        raise HTTPException(status_code=422, detail="From must be on or before Through.")
    zone = ZoneInfo(settings.display_tz)
    today = now.astimezone(zone).date()
    since = datetime.combine(from_day, time.min, zone) if from_day else None
    until = (
        datetime.combine(min(to_day, today) + timedelta(days=1), time.min, zone) if to_day else None
    )
    cursor = (before, before_id) if before is not None else None
    rows = activity_rows(
        repo,
        now,
        settings.display_tz,
        apps,
        limit=PAGE + 1,
        before=cursor,
        selection=ActivityFilters(parcel_id=parcel_id, kind=event_type, since=since, until=until),
    )
    params = {}
    if parcel_id is not None:
        params["parcel_id"] = str(parcel_id)
    if event_type != "all":
        params["type"] = event_type
    if from_day:
        params["from"] = from_day.isoformat()
    if to_day:
        params["to"] = to_day.isoformat()
    origins = request.query_params.getlist("return_to")
    destination = (
        reading_destination(request, repo, origins[0]) if not fixed and len(origins) == 1 else None
    )
    origin = (
        parse_qs(urlsplit(parent).query).get("return_to", [None])[0]
        if parent
        else destination.url
        if destination
        else None
    )
    base = f"/fields/parcels/{parcel_id}" if parent else "/activity/"
    if parent:
        params.pop("parcel_id", None)
        params["view"] = "activity"

    def url(state):
        values = {**state, **({"return_to": origin} if origin else {})}
        return with_farm(base + ("?" + urlencode(values) if values else ""), repo.farm_id)

    def provider(state):
        values = {k: v for k, v in state.items() if k not in {"view", "parcel_id"}}
        if parent:
            values["parent"] = url(state)
        return with_farm(
            f"/activity/parcels/{parcel_id}/workspace?" + urlencode(values), repo.farm_id
        )

    newest_url = url(params)
    current_params = dict(params)
    if before is not None:
        current_params.update(before=before.isoformat(), before_id=before_id)
    current_url = url(current_params)
    older = None
    if len(rows) > PAGE:
        rows = rows[:PAGE]
        last = rows[-1]
        older_params = {**params, "before": last["at"].isoformat(), "before_id": last["cursor_id"]}
        older = url(older_params)
    cookie = request.cookies.get(columns.COOKIE.format(farm_id=repo.farm_id))
    shown = columns.chosen(columns.parse_cookie(cookie))
    farm = repo.get(Farm, repo.farm_id, scope="logs:read")
    name = farm.name if farm is not None else ""
    parcels = repo.list(Parcel, scope="fields:read", order_by=Parcel.id)
    by_alias = {p.alias: p for p in parcels}
    # Aliases are unique within this authorized farm; never guess one parcel for a whole group.
    source = urlsplit(current_url)
    source_params = parse_qs(source.query)
    source_params.pop("return_to", None)
    source = source._replace(query=urlencode({k: values[0] for k, values in source_params.items()}))
    for row in rows:
        row["parcel_links"] = [
            {
                "alias": alias,
                "url": with_farm(
                    f"/fields/parcels/{by_alias[alias].id}?"
                    + urlencode(
                        {
                            "view": "activity",
                            "return_to": source._replace(
                                fragment=f"activity-row-{row['cursor_id']}"
                            ).geturl(),
                        }
                    ),
                    repo.farm_id,
                ),
            }
            for alias in row["parcels"]
            if alias in by_alias
        ]
    return dict(
        destination=destination,
        columns=shown,
        all_columns=columns.COLUMNS,
        always=columns.ALWAYS,
        rows=[{**r, "cells": columns.cells(r, name)} for r in rows],
        older=older,
        newest_url=newest_url,
        current_url=current_url,
        filtered=bool(event_type != "all" or from_day or to_day or (parcel_id and not fixed)),
        fixed_parcel=fixed,
        parent=current_url if parent else None,
        origin=origin,
        filter_action=base,
        clear_url=url(
            {"view": "activity"} if parent else {"parcel_id": parcel_id} if fixed else {}
        ),
        poll_url=provider(params) if fixed else newest_url,
        farm_id=repo.farm_id,
        can_export="operations" in apps,
        parcels=parcels,
        unavailable_parcel=unavailable_parcel,
        filter_values={
            "parcel_id": parcel_id,
            "kind": event_type,
            "start": from_day.isoformat() if from_day else "",
            "end": to_day.isoformat() if to_day else "",
        },
        type_options=(
            ("all", "All activities"),
            ("spray", "Sprays"),
            ("advice", "Advice"),
            ("sharing", "Sharing"),
            ("demo", "Demo"),
        ),
        today=today.isoformat(),
        timezone=settings.display_tz,
        paged=before is not None,
        replay_behind=isinstance(clock, ReplayClock) and now < datetime.now(UTC),
    )


@router.get("/columns")
def set_columns(
    request: Request, show: Annotated[list[str] | None, Query()] = None, next: str = "/activity/"
) -> RedirectResponse:
    """Remember the viewer's columns for the selected farm in a cookie, then go back. It only
    sets the cookie: nothing is read through the repository and nothing is written."""
    _require_on(request)
    settings = request.app.state.settings
    with request.app.state.session_factory() as session:
        farm_id = selected_farm(request, session, settings).id
    keys = [k for k in dict.fromkeys(show or []) if k in columns.KEYS]
    resp = RedirectResponse(safe_next(next), status_code=303)
    resp.set_cookie(
        columns.COOKIE.format(farm_id=farm_id),
        ",".join(keys),
        max_age=ONE_YEAR,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp

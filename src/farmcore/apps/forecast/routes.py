"""Spray-window fragments, mounted at `/forecast`:

- `GET /forecast/parcels/{id}`: the parcel's next five days (the advice card's slot, the map
  panel's parcel section), with the parcel's protection and planned sulphur from its latest
  advice row; unknown parcel: 404.
- `GET /forecast/today`: the farm's next five days (a Today widget), weather only.
- `GET /forecast/busy`: remember a browser-local, per-farm availability preference and redirect.

Both read the latest run issued by clock now (`store.latest_run`) and grade it with the advisor's
own rules (`ranking.rank_days`); no run → a "No forecast loaded" note. With the app switched off
on `/apps` (routes stay mounted until the next start) both return an empty fragment.
Availability changes only a cookie, never the database; no route fetches weather.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from farmcore.apps.advisor import service as advice
from farmcore.apps.advisor.navigation import parcel_link
from farmcore.apps.forecast import availability, planning, store
from farmcore.core.access import Repo, selected_farm
from farmcore.core.clock import get_clock
from farmcore.core.models.canonical import Parcel
from farmcore.core.models.kernel import ClockState
from farmcore.core.registry import visible_manifests
from farmcore.core.routes_farms import safe_next
from farmcore.web import render

router = APIRouter()

APP = "forecast"
PARCEL_SCOPE = "fields:read"


_utc_text = planning.utc_text
_day_label = planning.day_label


def _plan_view(request: Request, repo: Repo, *, sulphur=False):
    tz = request.app.state.settings.display_tz
    now = get_clock(repo.session, request.app.state.settings).now()
    busy = availability.parse(
        request.cookies.get(availability.COOKIE.format(farm_id=repo.farm_id)),
        now.astimezone(ZoneInfo(tz)).date(),
    )
    return planning.plan_days(repo, now, tz, busy, sulphur=sulphur)


def _plan_selected(request: Request, repo: Repo, day: str | None) -> dict[str, Any]:
    """The shared plan, its best weather, and the selected day (best by default)."""
    view = _plan_view(request, repo)
    selected = None
    if day:
        try:
            wanted = date.fromisoformat(day).isoformat()
        except ValueError:
            wanted = None
        selected = next((d for d in view["days"] if d["day"] == wanted), None)
    best = next((d for d in view["days"] if d["grade"] == "best"), None)
    if selected is None:
        selected = best or next((d for d in view["days"] if d["eligible"]), view["days"][0])
    # Build reading links only after the peer planner resolves explicit/default/best selection.
    links = {
        p["id"]: parcel_link(request, p["id"], "/forecast/plan#plan-heading")
        for p in view["undated"]
    }
    links.update(
        {
            p["id"]: parcel_link(
                request, p["id"], f"/forecast/plan?day={selected['day']}#plan-day-{selected['day']}"
            )
            for p in selected["parcels"]
        }
    )
    return {**view, "selected": selected, "best": best, "workspace_links": links}


def _window(
    request: Request,
    repo: Repo,
    *,
    protected_until: datetime | None = None,
    sulphur: bool = False,
    top: bool = False,
) -> dict[str, Any]:
    view = _plan_view(request, repo, sulphur=sulphur)
    preview = (
        planning.top_days(view["days"], 3)
        if top and view["run"]
        else [d for d in view["days"] if d["eligible"]][:5]
        if view["run"]
        else []
    )
    cover = (
        protected_until.astimezone(ZoneInfo(request.app.state.settings.display_tz)).date()
        if protected_until
        else None
    )
    for d in preview:
        d["protected"] = (
            f"{cover.day} {cover:%b}" if cover and date.fromisoformat(d["day"]) <= cover else None
        )
    return {
        **view,
        "days": preview,
        "best_later": not top
        and any(d["grade"] == "best" and d not in preview for d in view["days"]),
        "top": top,
    }


def _off(request: Request) -> bool:
    return not any(m.name == APP for m in visible_manifests(request))


@router.get("/busy")
def set_busy(
    request: Request, day: date, busy: Literal["0", "1"], next: str = "/forecast/today"
) -> RedirectResponse:
    """Set a browser preference only. No Repo: its reads would commit audit rows."""
    if _off(request):
        raise HTTPException(status_code=404, detail="Not found.")
    settings = request.app.state.settings
    zone = ZoneInfo(settings.display_tz)
    with request.app.state.session_factory() as session:
        farm_id = selected_farm(request, session, settings).id
        # get_clock creates a missing clock row. For this cookie-only route, use
        # the configured initial clock instead when the DB has no clock yet.
        row = session.get(ClockState, 1)
        if row is not None:
            now = get_clock(session, settings).now()
        elif settings.clock_mode == "live":
            now = datetime.now(UTC)
        else:
            now = settings.replay_start
            if now.tzinfo is None:
                now = now.replace(tzinfo=zone)
    today = now.astimezone(zone).date()
    name = availability.COOKIE.format(farm_id=farm_id)
    days = availability.parse(request.cookies.get(name), today)
    if day >= today:
        if busy == "1":
            days.add(day)
        else:
            days.discard(day)
    if len(days) > availability.MAX_DATES:
        raise HTTPException(status_code=422, detail="Too many busy dates saved in this browser.")
    response = RedirectResponse(safe_next(next), status_code=303)
    if days:
        response.set_cookie(
            name,
            availability.serialize(days),
            max_age=availability.ONE_YEAR,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
    else:
        response.delete_cookie(
            name, httponly=True, samesite="lax", secure=request.url.scheme == "https"
        )
    return response


@router.get("/parcels/{parcel_id}", response_class=HTMLResponse)
def parcel_window(parcel_id: int, request: Request, repo: Repo) -> HTMLResponse:
    if _off(request):
        return HTMLResponse("")
    if repo.get(Parcel, parcel_id, scope=PARCEL_SCOPE) is None:
        raise HTTPException(status_code=404, detail=f"parcel {parcel_id} not found")
    now = get_clock(repo.session, request.app.state.settings).now()
    latest = advice.latest_advice(repo, now, parcel_id=parcel_id).get(parcel_id)
    products = (latest.products or []) if latest else []
    view = _window(
        request,
        repo,
        protected_until=latest.protected_until if latest else None,
        # the engine's own sulphur rule (advisor.engine.decision): a planned powdery product
        sulphur=any("powdery" in (p.get("targets") or []) for p in products),
    )
    return render(
        request,
        "forecast/_window.html",
        **view,
        planned_treatment=next(
            (
                p
                for p in [*view["rescheduled"], *view["undated"]]
                if p["id"] == parcel_id and (p.get("to_day") or p.get("schedule_reason"))
            ),
            None,
        ),
        on_card=True,
        window_id=f"spray-window-{repo.farm_id}-parcel-{parcel_id}",
    )


@router.get("/today", response_class=HTMLResponse)
def today_window(request: Request, repo: Repo) -> HTMLResponse:
    if _off(request):
        return HTMLResponse("")
    return render(
        request,
        "forecast/_window.html",
        **_window(request, repo, top=True),
        on_card=False,
        window_id=f"spray-window-{repo.farm_id}-today",
    )


@router.get("/home-weather", response_class=HTMLResponse)
def home_weather(request: Request, repo: Repo) -> HTMLResponse:
    """Five forecast days, separate from the station's observed/archived readings."""
    from datetime import timedelta

    from farmcore.apps.forecast.weather import day_weather

    if _off(request):
        return HTMLResponse("")
    settings = request.app.state.settings
    now = get_clock(repo.session, settings).now()
    today = now.astimezone(ZoneInfo(settings.display_tz)).date()
    run = store.latest_run(repo, now)
    days = [
        {
            "day": day.isoformat(),
            "label": _day_label(day, today),
            **day_weather(run, day, settings.display_tz),
        }
        for day in (today + timedelta(days=i) for i in range(5))
    ]
    return render(
        request,
        "forecast/_home_weather.html",
        days=days,
        run=run,
        freshness=store.freshness(repo, now),
        issued=_utc_text(run.issued_at) if run else None,
        available=_utc_text(run.available_at) if run else None,
    )


@router.get("/plan", response_class=HTMLResponse)
def plan_page(request: Request, repo: Repo, day: str | None = None) -> HTMLResponse:
    if _off(request):
        raise HTTPException(status_code=404, detail="Plan is not enabled.")
    return render(request, "forecast/plan.html", **_plan_selected(request, repo, day))


@router.get("/plan/days", response_class=HTMLResponse)
def plan_fragment(request: Request, repo: Repo, day: str | None = None) -> HTMLResponse:
    if _off(request):
        return HTMLResponse("")
    return render(request, "forecast/_plan_days.html", **_plan_selected(request, repo, day))

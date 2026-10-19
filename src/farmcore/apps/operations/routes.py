"""Operations pages and exports, mounted at `/operations`.

- `GET /operations/`: recent done logs (up to the clock; pending drafts are not records) with
  source badges, parcels, products (the register cards' words: `register.product_summaries`),
  export links.
- `GET /operations/parcels/{id}`: the parcel's timeline page (loads the partial with HTMX).
- `GET /operations/parcels/{id}/timeline`: HTMX partial, events newest first (`logs:read`); up
  to `TIMELINE_LIMIT` record events plus the latest `ADVICE_LIMIT` advice events, so hourly
  advice never pushes sprays off the list. A record's newest entry carries its current products
  and parcels (older entries of it none, so a merged dose never sits on the voice entry).
  Unknown parcel: 404.
- `GET /operations/register`: the same register as a phone-friendly HTML table (the CSV's rows
  and columns, cell for cell), with the CSV and JSON download links.
- `GET /operations/register.csv|.json`: the Reg. 2023/564 spray register (`register:read`),
  each row with its spray's label and copper checks (`checks.py`): a `checks` list in the
  JSON, one `checks` column of words after the EU columns in the CSV; the page shows them as one
  line per check on each spray card. The owner exports also carry each row's saved spray
  weather (`register_rows(weather=True)`): a `saved_weather` list in the JSON, one last
  `saved_weather` column in the CSV; grant and DG AGRI reads keep the default projection.
- `GET /operations/production`: the farm's production mode (conventional / organic), the products
  that mode offers (organic: only those with a source, the others withheld with the reason) and
  the copper tracker (`production.py`). `POST /operations/production` (admin) switches the mode.
- `POST /operations/isoxml`: upload an ISOXML TaskData file (`logs:write`, admin token when set);
  each task merges into the matching voice log or becomes a new log (`isoxml.import_taskdata`).

Every read goes through the request's `Repo`; time-stamped reads are bounded by `clock.now()`.
Views are plain dicts built inside the route, so nothing lazy-loads after the session closes.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from farmcore.apps.operations import bbch, checks, isoxml, register
from farmcore.core.access import Repo, require_farm_owner
from farmcore.core.clock import get_clock
from farmcore.core.models.canonical import Event, Farm, Log, LogParcel, LogSource, Parcel
from farmcore.core.navigation import return_destination
from farmcore.web import context_url, render

router = APIRouter()

MAX_UPLOAD_BYTES = 20_000_000  # a TaskData zip with TLG logs; far above a demo file
RECENT_LOGS = 20
TIMELINE_LIMIT = 50  # record events (log.*, …)
ADVICE_LIMIT = 10  # advice.* events, capped on their own
PARCEL_SCOPE = "fields:read"  # parcel names and aliases belong to the fields app

# Source badge per source type (log_source.source_type) or the advisor.
BADGES: dict[str, tuple[str, str]] = {
    "voice": ("🎤", "voice"),
    "seed": ("🌱", "seed"),
    "advisor": ("🤖", "advisor"),
    "isoxml": ("🚜", "machine"),
    "file": ("📄", "file"),
}
KIND_LABELS: dict[str, str] = {
    "log.created": "Logged",
    "log.confirmed": "Confirmed",
    "log.merged": "Records merged",
    "log.cancelled": "Cancelled",
    "advice.evaluated": "Advice",
    "advice.changed": "Advice changed",
}


def _now(request: Request, repo: Repo) -> datetime:
    return get_clock(repo.session, request.app.state.settings).now()


def _badges(source_types: list[str]) -> list[dict[str, str]]:
    out = []
    for t in dict.fromkeys(source_types):
        icon, label = BADGES.get(t, ("•", t))
        out.append({"source": t, "icon": icon, "label": label})
    return out


def _event_sources(event: Event) -> list[str]:
    if event.kind.startswith("advice.") or event.source_app == "advisor":
        return ["advisor"]
    return [str(t) for t in (event.payload or {}).get("source_types", [])]


def _timeline_events(repo: Repo, parcel_id: int, now: datetime) -> list[Event]:
    """Newest first: up to TIMELINE_LIMIT record events and ADVICE_LIMIT advice events."""
    where = (Event.res_model == "parcel", Event.res_id == parcel_id, Event.occurred_at <= now)
    newest = (Event.occurred_at.desc(), Event.id.desc())
    is_advice = Event.kind.startswith("advice.")
    records = repo.list(
        Event, *where, ~is_advice, scope="logs:read", order_by=newest, limit=TIMELINE_LIMIT
    )
    advice = repo.list(
        Event, *where, is_advice, scope="logs:read", order_by=newest, limit=ADVICE_LIMIT
    )
    return sorted([*records, *advice], key=lambda e: (e.occurred_at, e.id), reverse=True)


def _record_summaries(repo: Repo, events: list[Event]) -> dict[int, dict[str, Any]]:
    """Product and parcel summary per done record the events name (`payload.log_id`), read now:
    a record's current products and parcels, so only its newest entry shows them."""
    ids = {(e.payload or {}).get("log_id") for e in events if e.kind.startswith("log.")}
    records = (
        # a pending draft is not a record until ✅ confirms it (as on the index and register)
        repo.list(Log, Log.id.in_(ids - {None}), Log.status == "done", scope="logs:read")
        if ids
        else []
    )
    if not records:
        return {}
    parcels = {p.id: p for p in repo.list(Parcel, scope=PARCEL_SCOPE)}
    parcels_of: dict[int, list[Parcel]] = {r.id: [] for r in records}
    for link in repo.list(LogParcel, LogParcel.log_id.in_(parcels_of), scope="logs:read"):
        if link.parcel_id in parcels:
            parcels_of[link.log_id].append(parcels[link.parcel_id])
    products_of = register.product_summaries(repo, records, scope="logs:read")
    return {
        r.id: {
            "products": products_of[r.id],
            "parcels": [p.alias for p in sorted(parcels_of[r.id], key=register.parcel_key)],
        }
        for r in records
    }


def _index_context(request: Request, repo: Repo) -> dict[str, Any]:
    now = _now(request, repo)
    recent = repo.list(
        Log,
        Log.status == "done",  # a pending draft is not a record until ✅ confirms it
        Log.ts_start <= now,
        scope="logs:read",
        order_by=(Log.ts_start.desc(), Log.id.desc()),
        limit=RECENT_LOGS,
    )
    ids = [log.id for log in recent]
    parcels = {
        p.id: p for p in sorted(repo.list(Parcel, scope=PARCEL_SCOPE), key=register.parcel_key)
    }
    parcels_of: dict[int, list[Parcel]] = {i: [] for i in ids}
    sources_of: dict[int, list[str]] = {i: [] for i in ids}
    if ids:
        for link in repo.list(LogParcel, LogParcel.log_id.in_(ids), scope="logs:read"):
            if link.parcel_id in parcels:
                parcels_of[link.log_id].append(parcels[link.parcel_id])
        for src in repo.list(
            LogSource, LogSource.log_id.in_(ids), scope="logs:read", order_by=LogSource.id
        ):
            sources_of[src.log_id].append(src.source_type)
    products_of = register.product_summaries(repo, recent, scope="logs:read")
    logs_view = [
        {
            "id": log.id,
            "name": log.name,
            "type": log.type,
            "status": log.status,
            "ts_start": log.ts_start,
            "parcels": [
                {"id": p.id, "alias": p.alias, "name": p.name}
                for p in sorted(parcels_of[log.id], key=register.parcel_key)
            ],
            "badges": _badges(sources_of[log.id]),
            "products": products_of[log.id],
        }
        for log in recent
    ]
    parcels_view = [{"id": p.id, "alias": p.alias, "name": p.name} for p in parcels.values()]
    return {"now": now, "logs": logs_view, "parcels": parcels_view, "bbch_source": bbch.SOURCE}


@router.get("/", response_class=HTMLResponse)
def index(request: Request, repo: Repo) -> HTMLResponse:
    return render(request, "operations/index.html", **_index_context(request, repo))


def _isoxml_view(result: isoxml.ImportResult) -> dict[str, Any]:
    tasks = []
    for t in result.tasks:
        badges = _badges(list(t.badges))
        tasks.append(
            {
                "task_id": t.task_id,
                "partfield": t.partfield,
                "parcel_id": t.parcel_id,
                "log_name": t.log_name,
                "merged": t.merged,
                "replaced": t.replaced,
                "unchanged": t.unchanged,
                "icons": " + ".join(b["icon"] for b in badges),
                "badges": badges,
            }
        )
    return {"tasks": tasks, "notes": list(result.notes)}


@router.post("/isoxml", response_class=HTMLResponse, dependencies=[Depends(require_farm_owner)])
def import_isoxml(
    request: Request, repo: Repo, file: Annotated[UploadFile, File()]
) -> HTMLResponse:
    """Import an uploaded TaskData file (`.xml` or `.zip`) at the clock's now. HTMX gets the
    result partial plus updated recent records out of band on success; a plain form post
    gets the full page; a bad file is a 400."""
    settings = request.app.state.settings
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    error, view = None, None
    if len(data) > MAX_UPLOAD_BYTES:
        error = f"the file is larger than {MAX_UPLOAD_BYTES // 1_000_000} MB"
    else:
        try:
            result = isoxml.import_taskdata(
                repo,
                data,
                now=_now(request, repo),
                actor=repo.actor,
                tz=settings.display_tz,
                weather_reader=getattr(request.app.state, "spray_weather_reader", None),
            )
            view = _isoxml_view(result)
        except isoxml.IsoxmlError as exc:
            error = str(exc)
    status = 400 if error else 200
    ctx = {"isoxml": view, "isoxml_error": error, "filename": file.filename}
    if request.headers.get("hx-request"):
        if not error:
            ctx.update(_index_context(request, repo), recent_records_oob=True)
        resp = render(request, "operations/_isoxml_result.html", **ctx)
    else:
        resp = render(request, "operations/index.html", **_index_context(request, repo), **ctx)
    resp.status_code = status
    return resp


@router.get("/parcels/{parcel_id}", response_class=HTMLResponse)
def parcel_page(parcel_id: int, request: Request, repo: Repo) -> HTMLResponse:
    parcel = repo.get(Parcel, parcel_id, scope=PARCEL_SCOPE)
    if parcel is None:
        raise HTTPException(status_code=404, detail=f"parcel {parcel_id} not found")
    view = {
        "id": parcel.id,
        "alias": parcel.alias,
        "name": parcel.name,
        "area_ha": parcel.area_ha,
        "iacs_id": parcel.iacs_id,
        "variety": (parcel.props or {}).get("variety"),
    }
    return render(
        request,
        "operations/parcel.html",
        parcel=view,
        return_destination=return_destination(request, repo, "/operations/", "Operations"),
    )


@router.get("/parcels/{parcel_id}/timeline", response_class=HTMLResponse)
def timeline_partial(parcel_id: int, request: Request, repo: Repo) -> HTMLResponse:
    if repo.get(Parcel, parcel_id, scope=PARCEL_SCOPE) is None:
        raise HTTPException(status_code=404, detail=f"parcel {parcel_id} not found")
    now = _now(request, repo)
    events = _timeline_events(repo, parcel_id, now)
    summaries = _record_summaries(repo, events)
    items = []
    for e in events:  # newest first: a record's summary goes on its newest entry only
        log_id = (e.payload or {}).get("log_id") if e.kind.startswith("log.") else None
        summary = summaries.pop(log_id, None) or {"products": [], "parcels": []}
        items.append(
            {
                "occurred_at": e.occurred_at,
                "label": KIND_LABELS.get(e.kind, e.kind),
                # A merge reads "Records merged" (two sources, one record), not "spray merged".
                "body": KIND_LABELS[e.kind] if e.kind == "log.merged" else e.body,
                "kind": e.kind,
                "badges": _badges(_event_sources(e)),
                **summary,
            }
        )
    return render(request, "operations/_timeline.html", items=items)


def _by_spray(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Register rows grouped per spray log, newest spray first (rows come in time order)."""
    groups: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row["log_id"], []).append(row)
    return list(groups.values())[::-1]


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, repo: Repo) -> HTMLResponse:
    """The register CSV as a table: the cells are read back from `register.to_csv`, so the page
    and the download never disagree. Each spray card lists its saved weather."""
    settings = request.app.state.settings
    tz = settings.display_tz
    now = _now(request, repo)
    rows = register.register_rows(repo, now, tz=tz, checks=True, weather=True)
    header, *cells = list(csv.reader(io.StringIO(register.to_csv(rows, weather=True, tz=tz))))
    flag = header.index("authorisation_placeholder")
    table = [{"cells": row, "placeholder": row[flag] == "yes"} for row in cells]
    sprays = _by_spray(rows)
    return render(
        request,
        "operations/register.html",
        now=now,
        header=header,
        rows=table,
        sprays=sprays,
        weather={items[0]["log_id"]: register.weather_lines(items, tz) for items in sprays},
        placeholders=sum(r["placeholder"] for r in table),
        cu_yearly=checks.fmt(checks.CU_YEARLY_KG_HA),
        cu_seven_years=checks.fmt(checks.CU_SEVEN_YEARS_KG_HA),
        cu_regulation=checks.CU_REGULATION,
        cu_regulation_url=checks.CU_REGULATION_URL,
    )


Season = Annotated[int | None, Query(ge=register.SEASON_MIN, le=register.SEASON_MAX)]
ParcelId = Annotated[int | None, Query(ge=1, le=register.PARCEL_ID_MAX)]


def _export_filter(
    request: Request, repo: Repo, season: int | None, parcel: int | None
) -> tuple[register.Window, Parcel | None]:
    tz = request.app.state.settings.display_tz
    try:
        return register.export_filter(
            repo, _now(request, repo), tz, season=season, parcel_id=parcel
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Parcel not found") from exc


@router.get("/register.csv")
def register_csv(
    request: Request, repo: Repo, season: Season = None, parcel: ParcelId = None
) -> Response:
    """The register; `season` / `parcel` narrow it to one season and parcel (D4)."""
    tz = request.app.state.settings.display_tz
    (since, until, before), chosen = _export_filter(request, repo, season, parcel)
    rows = register.register_rows(
        repo,
        until,
        tz=tz,
        checks=True,
        weather=True,
        since=since,
        before=before,
        parcel_id=chosen.id if chosen else None,
    )
    name = register.export_filename("spray-register", "csv", season, chosen)
    return Response(
        register.to_csv(rows, weather=True, tz=tz),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/register.json")
def register_json(
    request: Request, repo: Repo, season: Season = None, parcel: ParcelId = None
) -> JSONResponse:
    settings = request.app.state.settings
    (since, until, before), chosen = _export_filter(request, repo, season, parcel)
    rows: list[dict[str, Any]] = register.register_rows(
        repo,
        until,
        tz=settings.display_tz,
        checks=True,
        weather=True,
        aliases=True,
        since=since,
        before=before,
        parcel_id=chosen.id if chosen else None,
    )
    return JSONResponse(rows)


@router.get("/production", response_class=HTMLResponse)
def production_section(request: Request, repo: Repo) -> HTMLResponse:
    """The production fragment (N8, `production.py`), up to the clock."""
    from farmcore.apps.operations import production  # map_sections imports this module

    now = _now(request, repo)
    tz = request.app.state.settings.display_tz
    return render(
        request,
        "operations/_production.html",
        p=production.view(repo, now, tz),
        cu_limit=production.CU_LIMIT,
        cu_source=production.CU_SOURCE,
        cu_url=production.CU_URL,
        cu_approval=production.CU_APPROVAL,
        cu_approval_url=production.CU_APPROVAL_URL,
        no_source=production.NO_SOURCE,
    )


@router.post("/production", dependencies=[Depends(require_farm_owner)])
def set_production(
    request: Request, repo: Repo, mode: Annotated[str, Form()] = ""
) -> RedirectResponse:
    from farmcore.apps.operations import production

    if mode not in production.MODES:
        raise HTTPException(status_code=422, detail=f"mode must be one of {production.MODES}")
    farm = repo.get(Farm, repo.farm_id, scope="logs:write")
    farm.props = {**(farm.props or {}), "production": mode}
    return RedirectResponse(context_url(request, "/advisor/"), status_code=303)

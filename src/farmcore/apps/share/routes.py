"""`/share/`: the farmer's consent ledger — who may read the farm, for what, until when.

- `GET /share/`: the grants table (non-owner `data_grant` rows of the selected farm, newest first,
  `audit:read`), the access log and the export links. Admins also get the grant form and a Revoke
  button per active grant; anonymous visitors see the same page read-only.
- `POST /share/grants`, `POST /share/grants/{id}/revoke`: admin only (`require_admin`), 303 to
  `/share/`; invalid input is a plain-words 422.

Never renders a raw chat id, a `tg:` ref, a cookie or the admin token (actors go through
`service.mask_actor`).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.apps.operations import register
from farmcore.apps.share import config, ngsi, service
from farmcore.core.access import OWNER, Repo, ScopeDenied, ScopedRepository
from farmcore.core.clock import get_clock
from farmcore.core.models.canonical import DataGrant, LlmAudit, Observation, Parcel
from farmcore.core.routes_clock import require_admin
from farmcore.db import get_session
from farmcore.web import context_url, is_admin, operator_authenticated, render

router = APIRouter()

AUDIT_SCOPE = "audit:read"
STATIONS_SCOPE = "stations:read"
PARCEL_SCOPE = "fields:read"  # the Sharing export parcel filter (D4)
WHO_GRANTEES = "grantees"  # `?who=grantees`: the access log shows third-party reads only
PROBE_LIMIT = 24  # rows a station grant sees on observations.json / audit.json (one day hourly)
MAX_ID = 2**63 - 1  # PostgreSQL bigint, the `data_grant.id` type
END_OF_DAY = time(23, 59, 59)  # a grant "until 15 Nov" holds through that local day
# Register columns on the grantee page (the JSON keeps every `register.FIELDS` key).
GRANTEE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("date", "Date"),
    ("start_time", "Time"),
    ("parcel_alias", "Parcel"),
    ("product_name", "Product"),
    ("authorisation_no", "Authorisation"),
    ("active_substance", "Active substance"),
    ("dose", "Dose"),
    ("dose_unit", "Unit"),
    ("area_treated_ha", "Area (ha)"),
    ("bbch", "BBCH"),
)


def _tz(request: Request) -> str:
    return request.app.state.settings.display_tz


Season = Annotated[int | None, Query(ge=register.SEASON_MIN, le=register.SEASON_MAX)]
ParcelId = Annotated[int | None, Query(ge=1, le=register.PARCEL_ID_MAX)]
ASCII_INT = re.compile(r"[0-9]{1,19}")  # not str.isdigit: it accepts "²", which int() refuses
ALL_SEASONS = "all"  # Sharing's "All seasons" choice: the exports without a season


def _window(
    request: Request, repo: ScopedRepository, season: int | None, parcel: int | None
) -> tuple[datetime | None, datetime, datetime | None, int | None]:
    """(since, until, before, parcel id) for an export narrowed to `season` / `parcel` (D4);
    404 for a parcel the repository cannot see. `ScopeDenied` passes through."""
    now = get_clock(repo.session, request.app.state.settings).now()
    try:
        (since, until, before), chosen = register.export_filter(
            repo, now, _tz(request), season=season, parcel_id=parcel
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Parcel not found") from exc
    return since, until, before, chosen.id if chosen else None


def _exports(
    request: Request, repo: ScopedRepository, season: str | None, parcel: int | None
) -> dict[str, Any]:
    """Sharing's export choices: seasons with sprays (default: the current one, for the demo)
    or all seasons, the parcels (default: all), and the export links narrowed to them."""
    tz = _tz(request)
    choices = register.seasons(repo, _clock_now(request, repo), tz)
    chosen_season: int | None = choices[0]
    if season == ALL_SEASONS:
        chosen_season = None
    elif season and ASCII_INT.fullmatch(season) and int(season) in choices:
        chosen_season = int(season)
    parcels = sorted(repo.list(Parcel, scope=PARCEL_SCOPE), key=register.parcel_key)
    chosen = next((p for p in parcels if p.id == parcel), None)
    query = urlencode(
        {
            **({"season": chosen_season} if chosen_season is not None else {}),
            **({"parcel": chosen.id} if chosen else {}),
        }
    )
    return {
        "export_seasons": choices,
        "export_season": chosen_season,
        "all_seasons": ALL_SEASONS,
        "export_parcels": parcels,
        "export_parcel": chosen.id if chosen else None,
        "export_query": query,
        "export_csv_name": register.export_filename("spray-register", "csv", chosen_season, chosen),
        "export_ld_name": register.export_filename("farm-ngsi-ld", "json", chosen_season, chosen),
    }


def scope_items(scopes: list[str]) -> list[dict[str, str]]:
    return [{"scope": s, "words": config.SCOPE_WORDS.get(s, s)} for s in scopes]


def _grant_view(grant: DataGrant, now: datetime, tz: str) -> dict[str, Any]:
    return {
        "id": grant.id,
        "grantee": grant.grantee,
        "scopes": scope_items(grant.scopes),
        "purpose": grant.purpose,
        "valid_from": service.local_date(grant.valid_from, tz) if grant.valid_from else "",
        "valid_to": service.local_date(grant.valid_to, tz) if grant.valid_to else "no end date",
        "revoked_at": grant.revoked_at,
        "status": service.grant_status(grant, now),
    }


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    repo: Repo,
    scope: Literal["all", "sprays", "weather"] = "all",
    status: Literal["all", "active", "scheduled", "expired", "revoked"] = "all",
    season: Annotated[str | None, Query(max_length=4)] = None,  # a year or "all"
    parcel: Annotated[str, Query(max_length=19)] = "",  # the form's "All parcels" sends ""
) -> HTMLResponse:
    now = datetime.now(UTC)
    grants = repo.list(
        DataGrant, DataGrant.grantee != OWNER, scope=AUDIT_SCOPE, order_by=DataGrant.id.desc()
    )
    tz = _tz(request)
    grantees_only = request.query_params.get("who") == WHO_GRANTEES
    views = [_grant_view(g, now, tz) for g in grants if not service.is_owner_grant(g)]
    if scope != "all":
        wanted = {"stations:read"} if scope == "weather" else {"logs:read", "register:read"}
        views = [g for g in views if any(s["scope"] in wanted for s in g["scopes"])]
    if status != "all":
        views = [g for g in views if g["status"] == status]
    return render(
        request,
        "share/index.html",
        grants=views,
        scope_filter=scope,
        status_filter=status,
        farm_id=repo.farm_id,
        audit_open="who" in request.query_params,
        all_reads_url="/share/?"
        + urlencode({"scope": scope, "status": status, "who": "all"})
        + "#access-log",
        grantee_reads_url="/share/?"
        + urlencode({"scope": scope, "status": status, "who": WHO_GRANTEES})
        + "#access-log",
        access=service.access_rows(repo, grantees_only=grantees_only, tz=tz),
        grantees_only=grantees_only,
        access_limit=config.ACCESS_LOG_LIMIT,
        presets=[(key, label) for key, (label, _scopes) in config.PRESETS.items()],
        default_preset=config.DEFAULT_PRESET,
        default_grantee=config.DEFAULT_GRANTEE,
        default_purpose=config.DEFAULT_PURPOSE,
        default_valid_to=(
            now.astimezone(ZoneInfo(tz)).date() + timedelta(days=config.DEFAULT_VALID_DAYS)
        ).isoformat(),
        grantee_max_len=config.GRANTEE_MAX_LEN,
        purpose_max_len=config.PURPOSE_MAX_LEN,
        **_exports(request, repo, season, int(parcel) if ASCII_INT.fullmatch(parcel) else None),
    )


def _valid_to(value: str, tz: str) -> datetime:
    try:
        day = date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a date (use the date picker).") from exc
    return datetime.combine(day, END_OF_DAY, tzinfo=ZoneInfo(tz))


@router.post("/grants", dependencies=[Depends(require_admin)])
def create(
    request: Request,
    repo: Repo,
    grantee: str = Form(""),
    preset: str = Form(""),
    purpose: str = Form(""),
    valid_to: str = Form(""),
) -> RedirectResponse:
    tz = _tz(request)
    try:
        service.create_grant(
            repo,
            grantee=grantee,
            preset=preset,
            purpose=purpose,
            valid_to=_valid_to(valid_to, tz),
            tz=tz,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(context_url(request, "/share/"), status_code=303)


def _ld_json(entities: list[dict[str, Any]]) -> JSONResponse:
    return JSONResponse(entities, media_type=ngsi.MEDIA_TYPE)


@router.get("/ngsi-ld.json")
def export_ngsi(
    request: Request, repo: Repo, season: Season = None, parcel: ParcelId = None
) -> JSONResponse:
    """The selected farm's done sprays as `AgriParcelOperation` entities (`register:read`);
    `season` / `parcel` narrow them (D4)."""
    since, until, before, parcel_id = _window(request, repo, season, parcel)
    return _ld_json(ngsi.export(repo, until, since=since, before=before, parcel_id=parcel_id))


@router.post("/grants/{grant_id}/revoke", dependencies=[Depends(require_admin)])
def revoke(request: Request, repo: Repo, grant_id: int) -> RedirectResponse:
    try:
        service.revoke_grant(repo, grant_id, tz=_tz(request))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="Not found.") from exc
    return RedirectResponse(context_url(request, "/share/"), status_code=303)


# --- Grantee view: /share/as/{grant_id}/ ------------------------------------------------------


def get_grant_repo(
    grant_id: int, request: Request, session: Session = Depends(get_session, scope="function")
) -> Iterator[ScopedRepository]:
    """The grant-bound repository of `grant_id`. Unknown id or an owner grant → 404; a grant
    that is not active → 403 in plain words (no data). Commits after the route (the access_log
    rows of the reads persist), rolls back if it raises."""
    in_range = 0 < grant_id <= MAX_ID  # beyond bigint is no grant, not a database error
    grant = session.get(DataGrant, grant_id) if in_range else None  # the authorisation lookup
    if grant is None or service.is_owner_grant(grant):
        raise HTTPException(status_code=404, detail="Not found.")
    if (
        not operator_authenticated(request)
        and grant.farm_id not in request.app.state.settings.demo_farm_ids
    ):
        raise HTTPException(status_code=404, detail="Not found.")
    status = service.grant_status(grant, datetime.now(UTC))
    if status != "active":
        raise HTTPException(
            status_code=403, detail=service.denied_message(grant, status, _tz(request))
        )
    repo = service.grant_repo(session, grant)
    try:
        yield repo
    except BaseException:
        session.rollback()
        raise
    session.commit()


GrantRepo = Annotated[ScopedRepository, Depends(get_grant_repo, scope="function")]


def _clock_now(request: Request, repo: ScopedRepository) -> datetime:
    return get_clock(repo.session, request.app.state.settings).now()


def _banner(grant: DataGrant, tz: str) -> dict[str, Any]:
    return {
        "id": grant.id,
        "grantee": grant.grantee,
        "scopes": scope_items(grant.scopes),
        "purpose": grant.purpose,
        "until": service.local_date(grant.valid_to, tz) if grant.valid_to else None,
    }


def _observations(repo: ScopedRepository, now: datetime, limit: int) -> list[dict[str, Any]]:
    """The farm's latest station observations up to `now` (`stations:read`)."""
    rows = repo.list(
        Observation,
        Observation.ts <= now,
        scope=STATIONS_SCOPE,
        order_by=Observation.ts.desc(),
        limit=limit,
    )
    return [{"ts": o.ts.isoformat(), "variable": o.variable, "value": o.value} for o in rows]


def _audit(request: Request, repo: ScopedRepository, limit: int) -> list[dict[str, Any]]:
    """Model-call audit rows (`audit:read`): id, time, adapter, status — never the bodies."""
    require_admin(request)
    stmt = select(LlmAudit).order_by(LlmAudit.id.desc()).limit(limit)
    rows = repo.execute(stmt, scope=AUDIT_SCOPE, resource="llm_audit").scalars()
    return [
        {"id": r.id, "ts": r.ts.isoformat(), "adapter": r.adapter, "status": r.status} for r in rows
    ]


# What a denied grantee read says, per resource (plain words, no data).
NOT_SHARED: dict[str, str] = {
    "register": "Not shared: this grant does not include the spray register.",
    "stations": "Not shared: this grant does not include weather station data.",
    "audit": "Not shared: this grant does not include the model-call audit.",
}
# Probe panels on the grantee page: (key, title, "shared" words).
PROBES: tuple[tuple[str, str, str], ...] = (
    ("stations", "Weather station data", "Shared: this grant includes weather station data."),
    ("audit", "Model-call audit", "Shared: this grant includes the model-call audit."),
)


def _probe(request: Request, repo: ScopedRepository, key: str, now: datetime) -> bool:
    """Try one read; True when the grant allows it, False on `ScopeDenied` (nothing logged)."""
    try:
        if key == "stations":
            _observations(repo, now, limit=1)
        else:
            if not is_admin(request):
                return False
            _audit(request, repo, limit=1)
    except ScopeDenied:
        return False
    return True


def _register(
    repo: ScopedRepository,
    now: datetime,
    tz: str,
    *,
    aliases: bool = False,
    since: datetime | None = None,
    before: datetime | None = None,
    parcel_id: int | None = None,
) -> list[dict[str, Any]] | None:
    """The grant farm's register rows, or None when the grant does not cover the register."""
    try:
        return register.register_rows(
            repo, now, tz=tz, aliases=aliases, since=since, before=before, parcel_id=parcel_id
        )
    except ScopeDenied:
        return None


def _denied(key: str) -> HTTPException:
    return HTTPException(status_code=403, detail=NOT_SHARED[key])


@router.get("/as/{grant_id}/", response_class=HTMLResponse)
def grantee_page(request: Request, repo: GrantRepo) -> HTMLResponse:
    tz = _tz(request)
    now = _clock_now(request, repo)
    rows = _register(repo, now, tz)
    probes = []
    for key, title, yes in PROBES:
        shared = _probe(request, repo, key, now)
        probes.append(
            {
                "key": key,
                "title": title,
                "shared": shared,
                "message": yes if shared else NOT_SHARED[key],
            }
        )
    return render(
        request,
        "share/grantee.html",
        grant=_banner(repo.grant, tz),
        now=now,
        columns=GRANTEE_COLUMNS,
        register_shared=rows is not None,
        register_denied=NOT_SHARED["register"],
        rows=[[row[name] for name, _label in GRANTEE_COLUMNS] for row in rows or []],
        probes=probes,
    )


@router.get("/as/{grant_id}/register.json")
def grantee_register(
    request: Request, repo: GrantRepo, season: Season = None, parcel: ParcelId = None
) -> JSONResponse:
    try:
        since, until, before, parcel_id = _window(request, repo, season, parcel)
    except ScopeDenied as exc:
        raise _denied("register") from exc
    rows = _register(
        repo, until, _tz(request), aliases=True, since=since, before=before, parcel_id=parcel_id
    )
    if rows is None:
        raise _denied("register")
    return JSONResponse(rows)


@router.get("/as/{grant_id}/ngsi-ld.json")
def grantee_ngsi(
    request: Request, repo: GrantRepo, season: Season = None, parcel: ParcelId = None
) -> JSONResponse:
    try:
        since, until, before, parcel_id = _window(request, repo, season, parcel)
        return _ld_json(ngsi.export(repo, until, since=since, before=before, parcel_id=parcel_id))
    except ScopeDenied as exc:
        raise _denied("register") from exc


@router.get("/as/{grant_id}/observations.json")
def grantee_observations(request: Request, repo: GrantRepo) -> JSONResponse:
    try:
        return JSONResponse(_observations(repo, _clock_now(request, repo), PROBE_LIMIT))
    except ScopeDenied as exc:
        raise _denied("stations") from exc


@router.get("/as/{grant_id}/audit.json")
def grantee_audit(request: Request, repo: GrantRepo) -> JSONResponse:
    try:
        return JSONResponse(_audit(request, repo, PROBE_LIMIT))
    except ScopeDenied as exc:
        raise _denied("audit") from exc

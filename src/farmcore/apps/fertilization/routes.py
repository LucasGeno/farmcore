"""Fertilization pages, mounted at `/fertilization`.

- `GET /`: this season's confirmed applications, with nutrients per hectare where computable.
- `GET /new`, `POST /drafts`: the owner's record form (the Record sheet's form); it proposes a
  draft (`source_type` `manual`) and goes to `/drafts/<id>` to preview, confirm or cancel.
- `GET /drafts/{id}/edit`, `POST /drafts/{id}/revise`: answer what an open draft still needs.
- `GET /records/{id}`, `POST /records/{id}/corrections`: one record, its corrections, and a
  correction draft (reason required) reviewed on `/drafts/<id>`.
- `GET /drafts/review?draft=`: the review fragment of both draft kinds.
- `GET /map/parcels/{id}`: the map panel section of a parcel.

Every route answers 404 while the app is switched off on `/apps` (its routes stay mounted until
the next start); writes are owner-only, like every farm write.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select

from farmcore.apps.fertilization import records, views
from farmcore.core import drafts, routes_drafts
from farmcore.core.access import Repo, require_farm_owner
from farmcore.core.clock import get_clock
from farmcore.core.farm_location import time_zone_of
from farmcore.core.models.canonical import CropSeason, Log, Parcel
from farmcore.core.models.catalog import CatalogItem
from farmcore.core.models.corrections import OperationCorrection
from farmcore.core.registry import visible_manifests
from farmcore.web import context_url, render

APP = "fertilization"
LOGS_SCOPE = "logs:read"
FIELDS_SCOPE = "fields:read"


def enabled(request: Request) -> None:
    """A switched-off app's routes stay mounted until restart; they answer 404 meanwhile."""
    if APP not in {m.name for m in visible_manifests(request)}:
        raise HTTPException(404, "Not found.")


router = APIRouter(dependencies=[Depends(enabled)])
Owner = Depends(require_farm_owner)


def _tz(request: Request, repo: Repo) -> str:
    return time_zone_of(repo.session, repo.farm_id, request.app.state.settings)


def _now(request: Request, repo: Repo) -> datetime:
    return get_clock(repo.session, request.app.state.settings).now()


@router.get("/", response_class=HTMLResponse)
def index(request: Request, repo: Repo) -> HTMLResponse:
    tz = _tz(request, repo)
    return render(
        request,
        "fertilization/index.html",
        rows=views.season_rows(repo, now=_now(request, repo), tz=tz),
        nutrients=records.NUTRIENTS,
    )


# --- The record form ---------------------------------------------------------------------


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def payload_from_form(form, tz: str) -> dict[str, Any]:
    """The draft payload a form gives; a blank field is absent (asked for), never defaulted."""
    out: dict[str, Any] = {
        "parcel_ids": [int(v) for v in form.getlist("parcel_ids") if str(v).isdigit()]
    }
    if not _blank(form.get("applied_at")):
        try:
            local = datetime.fromisoformat(form["applied_at"])
        except ValueError as exc:
            raise ValueError("Give the date as YYYY-MM-DD HH:MM.") from exc
        # An instant in UTC, as a record's facts give it: an unchanged form changes nothing.
        out["ts_start"] = local.replace(tzinfo=ZoneInfo(tz)).astimezone(UTC).isoformat()
    if not _blank(form.get("fertiliser")):
        item = form.get("catalog_item_id")
        out["fertiliser"] = {
            "name": form["fertiliser"],
            **({"catalog_item_id": int(item)} if str(item or "").isdigit() else {}),
        }
    if not _blank(form.get("amount")):
        out["amount"] = {"value": form["amount"].replace(",", "."), "unit": form.get("unit", "")}
    nutrients = {
        n: form[f"nutrient_{n}"].replace(",", ".")
        for n in records.NUTRIENTS
        if not _blank(form.get(f"nutrient_{n}"))
    }
    if nutrients:
        out["composition"] = {
            "nutrients": nutrients,
            **({} if _blank(form.get("source")) else {"source": form["source"]}),
            **({} if _blank(form.get("reference")) else {"reference": form["reference"]}),
        }
    for key, field in (("density_kg_per_l", "density"), ("area_ha", "area_ha")):
        if not _blank(form.get(field)):
            source = form.get(f"{field}_source")
            out[key] = {
                "value": form[field].replace(",", "."),
                **({} if _blank(source) else {"source": source}),
            }
    if not _blank(form.get("notes")):
        out["notes"] = form["notes"]
    return out


def _echo(form) -> dict[str, Any]:
    """The typed form back, for a refused post; several parcels stay several."""
    return {**dict(form), "parcel_ids": form.getlist("parcel_ids")}


def _form_page(
    request: Request,
    repo: Repo,
    *,
    action: str,
    values: dict[str, Any],
    problem: str = "",
    status: int = 200,
    extra: dict[str, Any] | None = None,
) -> HTMLResponse:
    parcels = repo.list(Parcel, scope=FIELDS_SCOPE, order_by=Parcel.alias)
    items = repo.execute(
        records.offered_items(repo.farm_id).order_by(CatalogItem.name),
        scope=LOGS_SCOPE,
        resource="catalog_item",
    ).all()
    response = render(
        request,
        "fertilization/form.html",
        action=action,
        values=values,
        parcels=parcels,
        items=items,
        nutrients=records.NUTRIENTS,
        sources=("label", "analysis", "supplier", "farmer"),
        problem=problem,
        request_key=uuid.uuid4().hex,
        **(extra or {}),
    )
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _form_values(payload: dict[str, Any], tz: str) -> dict[str, Any]:
    """Form field values from a draft payload or a record's facts."""
    values: dict[str, Any] = {"parcel_ids": payload.get("parcel_ids", [])}
    if payload.get("ts_start"):
        local = datetime.fromisoformat(payload["ts_start"]).astimezone(ZoneInfo(tz))
        values["applied_at"] = local.strftime("%Y-%m-%dT%H:%M")
    fertiliser = payload.get("fertiliser") or {}
    values["fertiliser"] = fertiliser.get("name", "")
    values["catalog_item_id"] = fertiliser.get("catalog_item_id")
    amount = payload.get("amount") or {}
    values["amount"], values["unit"] = amount.get("value", ""), amount.get("unit", "kg/ha")
    composition = payload.get("composition") or {}
    for n, share in (composition.get("nutrients") or {}).items():
        values[f"nutrient_{n}"] = share
    values["source"] = composition.get("source", "")
    values["reference"] = composition.get("reference", "")
    for key, field in (("density_kg_per_l", "density"), ("area_ha", "area_ha")):
        factor = payload.get(key) or {}
        values[field], values[f"{field}_source"] = factor.get("value", ""), factor.get("source", "")
    values["notes"] = payload.get("notes", "")
    return values


@router.get("/new", response_class=HTMLResponse, dependencies=[Owner])
def new(request: Request, repo: Repo) -> HTMLResponse:
    return _form_page(
        request, repo, action="/fertilization/drafts", values={"unit": "kg/ha", "parcel_ids": []}
    )


async def _form(request: Request):
    return await request.form()


@router.post("/drafts", dependencies=[Owner])
def propose(request: Request, repo: Repo, form=Depends(_form)) -> Response:
    tz = _tz(request, repo)
    try:
        payload = payload_from_form(form, tz)
        draft, _ = drafts.propose(
            repo.session,
            visible_manifests(request),
            farm_id=repo.farm_id,
            actor=routes_drafts.request_actor(request),
            kind=records.KIND,
            payload=payload,
            provenance={"source_type": "manual"},
            request_key=str(form.get("request_key") or uuid.uuid4().hex)[:128],
        )
    except (ValueError, drafts.DraftError) as exc:
        repo.session.rollback()
        reason = exc.reason if isinstance(exc, drafts.DraftError) else str(exc)
        return _form_page(
            request,
            repo,
            action="/fertilization/drafts",
            values=_echo(form),
            problem=reason,
            status=422,
        )
    return RedirectResponse(context_url(request, f"/drafts/{draft.id}"), status_code=303)


@router.get("/drafts/{draft_id}/edit", response_class=HTMLResponse, dependencies=[Owner])
def edit(draft_id: int, request: Request, repo: Repo) -> HTMLResponse:
    draft = routes_drafts.load(request, repo, draft_id)
    if draft.kind != records.KIND:
        raise HTTPException(404, "Draft not found.")
    return _form_page(
        request,
        repo,
        action=f"/fertilization/drafts/{draft.id}/revise",
        values=_form_values(draft.payload, _tz(request, repo)),
        extra={"revision": draft.revision, "missing": records.incomplete(draft.payload)},
    )


@router.post("/drafts/{draft_id}/revise", dependencies=[Owner])
def revise(draft_id: int, request: Request, repo: Repo, form=Depends(_form)) -> Response:
    draft = routes_drafts.load(request, repo, draft_id)
    try:
        drafts.revise(
            repo.session,
            visible_manifests(request),
            farm_id=repo.farm_id,
            draft_id=draft.id,
            actor=routes_drafts.request_actor(request),
            payload=payload_from_form(form, _tz(request, repo)),
            revision=int(form.get("revision") or 0),
        )
    except (ValueError, drafts.DraftError) as exc:
        repo.session.rollback()
        if isinstance(exc, drafts.DraftNotFound):
            raise HTTPException(404, exc.reason) from exc
        reason = exc.reason if isinstance(exc, drafts.DraftError) else str(exc)
        return _form_page(
            request,
            repo,
            action=f"/fertilization/drafts/{draft_id}/revise",
            values=_echo(form),
            problem=reason,
            status=getattr(exc, "status_code", 422),
            extra={"revision": form.get("revision")},
        )
    return RedirectResponse(context_url(request, f"/drafts/{draft_id}"), status_code=303)


# --- Review fragment ---------------------------------------------------------------------


def _parcel_names(repo: Repo, ids: list[int]) -> list[str]:
    found = {p.id: p for p in repo.list(Parcel, Parcel.id.in_(ids), scope=FIELDS_SCOPE)}
    return [f"{found[i].alias} {found[i].name}" for i in ids if i in found]


@router.get("/drafts/review", response_class=HTMLResponse, dependencies=[Owner])
def draft_review(
    request: Request, repo: Repo, draft: Annotated[int, Query(ge=1, lt=2**63)]
) -> HTMLResponse:
    item = routes_drafts.load(request, repo, draft)
    tz = ZoneInfo(_tz(request, repo))
    if item.kind == records.KIND:
        record = item.payload
        before = None
    elif item.kind == records.CORRECTION_KIND:
        record = item.payload.get("after") or {}
        receipt = repo.session.scalar(
            select(OperationCorrection).where(
                OperationCorrection.farm_id == repo.farm_id,
                OperationCorrection.request_key == f"draft:{item.id}",
            )
        )
        if receipt is not None:  # confirmed: the record as it was, not as it is now
            before = receipt.before_snapshot
        else:
            log = repo.get(Log, item.payload["log_id"], scope=LOGS_SCOPE)
            before = records.record_facts(log) if log is not None else None
    else:
        raise HTTPException(404, "Draft not found.")

    def shown(facts: dict[str, Any] | None) -> dict[str, Any] | None:
        if facts is None:
            return None
        start = facts.get("ts_start")
        at = datetime.fromisoformat(start) if start else None
        seasons = facts.get("seasons")
        if seasons is None and at is not None:
            seasons = records.seasons_on(repo.session, repo.farm_id, facts["parcel_ids"], at)
        return {
            **facts,
            "parcels": _parcel_names(repo, facts.get("parcel_ids", [])),
            "local_start": at.astimezone(tz) if at else None,
            "season": _season_text(repo, seasons),
        }

    return render(
        request,
        "fertilization/_draft_review.html",
        draft=item,
        record=shown(record),
        before=shown(before),
        missing=records.incomplete(item.payload),
        reason=item.payload.get("reason", ""),
    )


def _season_text(repo: Repo, seasons: dict[str, int | None] | None) -> str:
    """The crop season of each parcel on the day, or "not recorded" (unknown, never guessed)."""
    ids = [i for i in (seasons or {}).values() if i is not None]
    found = {s.id: s for s in repo.list(CropSeason, CropSeason.id.in_(ids), scope=FIELDS_SCOPE)}
    names = []
    for season_id in (seasons or {}).values():
        row = found.get(season_id)
        if row is None:
            names.append("not recorded")
        else:
            names.append(f"{row.crop_name or row.crop_eppo or 'crop unknown'} from {row.starts_on}")
    return ", ".join(dict.fromkeys(names)) or "not recorded"


# --- One record and its corrections ------------------------------------------------------


def _done_record(repo: Repo, log_id: int) -> Log:
    log = repo.get(Log, log_id, scope=LOGS_SCOPE) if 1 <= log_id < 2**63 else None
    if log is None or log.type != records.LOG_TYPE or log.status != "done":
        raise HTTPException(404, "Record not found.")
    return log


@router.get("/records/{log_id}", response_class=HTMLResponse)
def record_page(log_id: int, request: Request, repo: Repo) -> HTMLResponse:
    return _record_page(request, repo, _done_record(repo, log_id))


def _record_page(
    request: Request, repo: Repo, log: Log, *, problem: str = "", status: int = 200, values=None
) -> HTMLResponse:
    tz = _tz(request, repo)
    facts = records.record_facts(log)
    corrections = repo.list(
        OperationCorrection,
        OperationCorrection.live_log_id == log.id,
        scope="corrections:read",
        order_by=OperationCorrection.id,
    )
    response = _form_page(
        request,
        repo,
        action=f"/fertilization/records/{log.id}/corrections",
        values=values or _form_values(facts, tz),
        problem=problem,
        status=status,
        extra={
            "record": log,
            "facts": facts,
            "parcel_names": _parcel_names(repo, facts["parcel_ids"]),
            "season": _season_text(
                repo, ((log.props or {}).get(records.PROPS_KEY) or {}).get("seasons")
            ),
            "corrections": corrections,
        },
    )
    return response


@router.post("/records/{log_id}/corrections", dependencies=[Owner])
def propose_correction(log_id: int, request: Request, repo: Repo, form=Depends(_form)) -> Response:
    log = _done_record(repo, log_id)
    tz = _tz(request, repo)
    try:
        current = records.record_facts(log)
        wanted = payload_from_form(form, tz)
        changes = {
            key: wanted.get(key)
            for key in records.CORRECTABLE
            if wanted.get(key) != current.get(key)
        }
        draft, _ = drafts.propose(
            repo.session,
            visible_manifests(request),
            farm_id=repo.farm_id,
            actor=routes_drafts.request_actor(request),
            kind=records.CORRECTION_KIND,
            payload={"log_id": log.id, "reason": form.get("reason", ""), "changes": changes},
            provenance={"source_type": "manual"},
            request_key=str(form.get("request_key") or uuid.uuid4().hex)[:128],
        )
    except (ValueError, drafts.DraftError) as exc:
        repo.session.rollback()
        reason = exc.reason if isinstance(exc, drafts.DraftError) else str(exc)
        log = _done_record(repo, log_id)
        return _record_page(request, repo, log, problem=reason, status=422, values=_echo(form))
    return RedirectResponse(context_url(request, f"/drafts/{draft.id}"), status_code=303)


# --- Map panel ---------------------------------------------------------------------------


@router.get("/map/parcels/{parcel_id}", response_class=HTMLResponse)
def parcel_section(parcel_id: int, request: Request, repo: Repo) -> HTMLResponse:
    if repo.get(Parcel, parcel_id, scope=FIELDS_SCOPE) is None:
        raise HTTPException(404, "Parcel not found.")
    rows = views.season_rows(
        repo, now=_now(request, repo), tz=_tz(request, repo), parcel_id=parcel_id
    )
    return render(request, "fertilization/_map_parcel.html", rows=rows)

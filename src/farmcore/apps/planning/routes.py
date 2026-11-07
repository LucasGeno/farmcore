"""Planned jobs: owner-only writes, member reads, explicit revision conflicts.

JSON endpoints are the public contract (`/jobs`, `/jobs/{id}.json`); the small form endpoints
serve the planning page and redirect with a plain error. Every write requires an explicit
`expected_revision`, so a second device cannot silently overwrite the first.
"""

from __future__ import annotations

from datetime import date, datetime, time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
)

from farmcore.apps.advisor import service as advice_service
from farmcore.apps.planning import service
from farmcore.apps.planning.models import PlanningJob
from farmcore.core.access import Repo, require_farm_member, require_farm_owner
from farmcore.core.clock import get_clock, parse_local
from farmcore.core.models.canonical import Advice
from farmcore.web import render

router = APIRouter()


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: StrictStr = Field(min_length=1, max_length=200)
    parcel_ids: list[StrictInt] = Field(min_length=1, max_length=80)
    assessment_id: StrictInt = Field(gt=0, le=2**63 - 1)
    product_id: StrictInt = Field(gt=0, le=2**63 - 1)
    scheduled_start: AwareDatetime
    duration_minutes: StrictInt = Field(ge=1, le=1440)
    notes: StrictStr = Field(default="", max_length=10000)

    @field_validator("parcel_ids")
    @classmethod
    def parcels_are_distinct(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("choose each parcel once")
        return value


class JobEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(ge=1)
    name: StrictStr | None = Field(default=None, max_length=200)
    parcel_ids: list[StrictInt] | None = None
    product_id: StrictInt | None = Field(default=None, gt=0, le=2**63 - 1)
    scheduled_start: AwareDatetime | None = None
    duration_minutes: StrictInt | None = Field(default=None, ge=1, le=1440)
    notes: StrictStr | None = Field(default=None, max_length=10000)

    @field_validator("parcel_ids")
    @classmethod
    def parcels_are_distinct(cls, value: list[int] | None) -> list[int] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("choose each parcel once")
        return value


class JobCancel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(ge=1)
    reason: StrictStr = Field(default="", max_length=2000)


def _now(request: Request, repo: Repo) -> datetime:
    return get_clock(repo.session, request.app.state.settings).now()


def _tz(request: Request) -> str:
    return request.app.state.settings.display_tz


def _local_input(value: datetime, tz: str) -> str:
    return value.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%dT%H:%M")


def _back(repo: Repo, error: str | None = None) -> RedirectResponse:
    values = {"farm": repo.farm_id}
    if error:
        values["error"] = error[:500]
    return RedirectResponse("/planning/?" + urlencode(values), status_code=303)


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, service.JobNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, service.JobConflict):
        return HTTPException(409, str(exc))
    return HTTPException(422, str(exc))


def _detail(repo: Repo, job_id: int, tz: str):
    job = repo.get(PlanningJob, job_id, scope=service.SCOPE)
    if job is None:
        raise HTTPException(404, "Planned job not found.")
    return service.job_json(repo, job, tz=tz)


@router.get("/jobs.json", dependencies=[Depends(require_farm_member)])
def jobs_json(request: Request, repo: Repo):
    return {"jobs": service.list_jobs(repo, tz=_tz(request))}


@router.get("/jobs/{job_id}.json", dependencies=[Depends(require_farm_member)])
def job_detail(request: Request, repo: Repo, job_id: int):
    return _detail(repo, job_id, _tz(request))


@router.post("/jobs", status_code=201, dependencies=[Depends(require_farm_owner)])
def create_job(request: Request, repo: Repo, body: JobCreate):
    try:
        job = service.create(
            repo,
            _now(request, repo),
            _tz(request),
            name=body.name,
            parcel_ids=body.parcel_ids,
            assessment_id=body.assessment_id,
            product_id=body.product_id,
            scheduled_start=body.scheduled_start,
            duration_minutes=body.duration_minutes,
            notes=body.notes,
        )
    except (service.JobError, ValueError, TypeError) as exc:
        raise _translate(exc) from None
    return service.job_json(repo, job, tz=_tz(request))


@router.patch("/jobs/{job_id}", dependencies=[Depends(require_farm_owner)])
def edit_job(request: Request, repo: Repo, job_id: int, body: JobEdit):
    changes = body.model_dump(exclude_unset=True)
    expected = changes.pop("expected_revision")
    try:
        job = service.locked(repo, job_id)
        job = service.edit(
            repo,
            _now(request, repo),
            _tz(request),
            job,
            expected_revision=expected,
            changes=changes,
        )
    except (service.JobError, service.JobNotFound, service.JobConflict) as exc:
        raise _translate(exc) from None
    return service.job_json(repo, job, tz=_tz(request))


@router.post("/jobs/{job_id}/cancel", dependencies=[Depends(require_farm_owner)])
def cancel_job(request: Request, repo: Repo, job_id: int, body: JobCancel):
    try:
        job = service.locked(repo, job_id)
        job = service.cancel(
            repo,
            _now(request, repo),
            _tz(request),
            job,
            expected_revision=body.expected_revision,
            reason=body.reason,
        )
    except (service.JobError, service.JobNotFound, service.JobConflict) as exc:
        raise _translate(exc) from None
    return service.job_json(repo, job, tz=_tz(request))


def _prefill(
    request: Request,
    repo: Repo,
    *,
    parcel: int | None,
    assessment: int | None,
    day: date | None,
) -> dict[str, object]:
    tz = _tz(request)
    now = _now(request, repo)
    advice_row = None
    if assessment is not None:
        advice_row = repo.get(Advice, assessment, scope="advice:read")
    if advice_row is None and parcel is not None:
        advice_row = advice_service.latest_advice(repo, now, tz=tz, parcel_id=parcel).get(parcel)
    start = None
    if advice_row is not None:
        start = advice_row.next_window_start
    if day is not None:
        candidate = datetime.combine(day, time(6, 0), tzinfo=ZoneInfo(tz))
        if start is None or start.astimezone(ZoneInfo(tz)).date() != day:
            start = candidate
    if start is None:
        start = now.astimezone(ZoneInfo(tz)).replace(minute=0, second=0, microsecond=0)
    return {
        "parcel_ids": [parcel] if parcel is not None else [],
        "assessment_id": advice_row.id if advice_row is not None else None,
        "scheduled_start": _local_input(start, tz),
        "name": "Planned spray",
        "duration_minutes": 90,
        "notes": "",
    }


def _page(request: Request, repo: Repo, *, form: dict[str, object] | None = None, error=None):
    now = _now(request, repo)
    return render(
        request,
        "planning/index.html",
        error=error,
        form=form or {},
        timezone=_tz(request),
        **service.page_context(repo, now, _tz(request)),
    )


@router.get("/", dependencies=[Depends(require_farm_member)])
def page(request: Request, repo: Repo, error: str | None = None):
    return _page(request, repo, error=error)


@router.get("/new", dependencies=[Depends(require_farm_member)])
def new_job(
    request: Request,
    repo: Repo,
    parcel: int | None = None,
    assessment: int | None = None,
    day: date | None = None,
):
    return _page(
        request,
        repo,
        form=_prefill(request, repo, parcel=parcel, assessment=assessment, day=day),
    )


@router.post("/jobs-form", dependencies=[Depends(require_farm_owner)])
async def create_form(request: Request, repo: Repo):
    form = await request.form()
    tz = _tz(request)
    try:
        start = parse_local(str(form.get("scheduled_start", "")), tz)
        body = JobCreate(
            name=str(form.get("name", "")),
            parcel_ids=[int(value) for value in form.getlist("parcel_ids") if str(value).strip()],
            assessment_id=int(str(form.get("assessment_id", "0"))),
            product_id=int(str(form.get("product_id", "0"))),
            scheduled_start=start,
            duration_minutes=int(str(form.get("duration_minutes", "0"))),
            notes=str(form.get("notes", "")),
        )
        service.create(
            repo,
            _now(request, repo),
            tz,
            **body.model_dump(),
        )
    except (ValueError, TypeError, service.JobError) as exc:
        return _back(repo, f"Check the job details: {exc}")
    return _back(repo)


@router.post("/jobs/{job_id}/edit-form", dependencies=[Depends(require_farm_owner)])
async def edit_form(request: Request, repo: Repo, job_id: int):
    form = await request.form()
    tz = _tz(request)
    try:
        body = JobEdit(
            expected_revision=int(str(form.get("expected_revision", "0"))),
            name=str(form.get("name", "")),
            parcel_ids=[int(value) for value in form.getlist("parcel_ids") if str(value).strip()],
            product_id=int(str(form.get("product_id", "0"))),
            scheduled_start=parse_local(str(form.get("scheduled_start", "")), tz),
            duration_minutes=int(str(form.get("duration_minutes", "0"))),
            notes=str(form.get("notes", "")),
        )
        job = service.locked(repo, job_id)
        service.edit(
            repo,
            _now(request, repo),
            tz,
            job,
            expected_revision=body.expected_revision,
            changes=body.model_dump(exclude_unset=True, exclude={"expected_revision"}),
        )
    except service.JobNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except service.JobConflict as exc:
        return _back(repo, str(exc))
    except (ValueError, TypeError, service.JobError) as exc:
        return _back(repo, f"Check the job details: {exc}")
    return _back(repo)


@router.post("/jobs/{job_id}/cancel-form", dependencies=[Depends(require_farm_owner)])
async def cancel_form(request: Request, repo: Repo, job_id: int):
    form = await request.form()
    try:
        body = JobCancel(
            expected_revision=int(str(form.get("expected_revision", "0"))),
            reason=str(form.get("reason", "")),
        )
        job = service.locked(repo, job_id)
        service.cancel(
            repo,
            _now(request, repo),
            _tz(request),
            job,
            expected_revision=body.expected_revision,
            reason=body.reason,
        )
    except service.JobNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except service.JobConflict as exc:
        return _back(repo, str(exc))
    except (ValueError, TypeError, service.JobError) as exc:
        return _back(repo, f"Check the cancellation: {exc}")
    return _back(repo)

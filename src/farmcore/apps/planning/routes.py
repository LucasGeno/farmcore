"""Planned jobs: owner-only writes, member reads, explicit revision conflicts.

JSON endpoints are the public contract (`/jobs`, `/jobs/{id}.json`); the small form endpoints
serve the planning page and redirect with a plain error. Every write requires an explicit
`expected_revision`, so a second device cannot silently overwrite the first.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
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


class AssignmentChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(ge=1)
    kind: Literal["machine", "operator"]
    resource_id: StrictInt | None = Field(default=None, gt=0, le=2**63 - 1)


class CompletionDose(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: StrictInt = Field(gt=0, le=2**63 - 1)
    value: Decimal = Field(gt=0, allow_inf_nan=False)
    unit: Literal["kg/ha", "L/ha", "l/ha", "g/ha", "ml/ha"]


class JobCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: StrictInt = Field(ge=1)
    request_key: StrictStr = Field(min_length=1, max_length=128)
    actual_start: AwareDatetime
    duration_minutes: StrictInt = Field(ge=1, le=1440)
    parcel_ids: list[StrictInt] = Field(min_length=1, max_length=80)
    quantities: list[CompletionDose] = Field(min_length=1, max_length=50)
    notes: StrictStr = Field(default="", max_length=10000)

    @field_validator("parcel_ids")
    @classmethod
    def parcels_are_distinct(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("choose each parcel once")
        return value

    @field_validator("request_key")
    @classmethod
    def key_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("request key must not be blank")
        return value


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
    if isinstance(exc, service.ResourceConflict):
        return HTTPException(409, str(exc))
    return HTTPException(422, str(exc))


def _detail(repo: Repo, job_id: int, tz: str, now: datetime):
    job = repo.get(PlanningJob, job_id, scope=service.SCOPE)
    if job is None:
        raise HTTPException(404, "Planned job not found.")
    return service.job_json(repo, job, now=now, tz=tz)


@router.get("/jobs.json", dependencies=[Depends(require_farm_member)])
def jobs_json(request: Request, repo: Repo):
    return {"jobs": service.list_jobs(repo, _now(request, repo), tz=_tz(request))}


@router.get("/jobs/{job_id}.json", dependencies=[Depends(require_farm_member)])
def job_detail(request: Request, repo: Repo, job_id: int):
    return _detail(repo, job_id, _tz(request), _now(request, repo))


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
    return service.job_json(repo, job, now=_now(request, repo), tz=_tz(request))


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
    return service.job_json(repo, job, now=_now(request, repo), tz=_tz(request))


@router.post("/jobs/{job_id}/assignments", dependencies=[Depends(require_farm_owner)])
def assign_job_resource(request: Request, repo: Repo, job_id: int, body: AssignmentChange):
    try:
        job = service.locked(repo, job_id)
        job = service.set_assignments(
            repo,
            _now(request, repo),
            _tz(request),
            job,
            expected_revision=body.expected_revision,
            changes={body.kind: body.resource_id},
        )
    except (service.JobError, service.JobNotFound, service.JobConflict) as exc:
        raise _translate(exc) from None
    except service.ResourceConflict as exc:
        raise _translate(exc) from None
    return service.job_json(repo, job, now=_now(request, repo), tz=_tz(request))


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
    return service.job_json(repo, job, now=_now(request, repo), tz=_tz(request))


def _completion_kwargs(body: JobCompletion) -> dict[str, object]:
    return {
        "expected_revision": body.expected_revision,
        "request_key": body.request_key,
        "actual_start": body.actual_start,
        "duration_minutes": body.duration_minutes,
        "parcel_ids": body.parcel_ids,
        "quantities": [dose.model_dump() for dose in body.quantities],
        "notes": body.notes,
    }


def _completion_body(form, tz: str) -> JobCompletion:
    products = form.getlist("product_id")
    values = form.getlist("dose_value")
    units = form.getlist("dose_unit")
    if not (len(products) == len(values) == len(units)):
        raise ValueError("each applied product needs a dose and a unit")
    quantities = []
    for product, value, unit in zip(products, values, units, strict=True):
        product_text, value_text = str(product).strip(), str(value).strip()
        if not product_text and not value_text:
            continue
        if not product_text or not value_text:
            raise ValueError("each applied product needs a dose and a unit")
        quantities.append({"product_id": int(product_text), "value": value_text, "unit": str(unit)})
    return JobCompletion(
        expected_revision=int(str(form.get("expected_revision", "0"))),
        request_key=str(form.get("request_key", "")),
        actual_start=parse_local(str(form.get("actual_start", "")), tz),
        duration_minutes=int(str(form.get("duration_minutes", "0"))),
        parcel_ids=[int(value) for value in form.getlist("parcel_ids") if str(value).strip()],
        quantities=quantities,
        notes=str(form.get("notes", "")),
    )


def _schedule_advice(request: Request, repo: Repo, tasks: BackgroundTasks) -> None:
    """Only a committed completion triggers a new evaluation; BackgroundTasks run after commit."""
    from farmcore.core import jobs

    tasks.add_task(
        jobs.run_now,
        request.app.state.session_factory,
        request.app.state.manifests,
        "advisor.evaluate",
        request.app.state.settings,
        farm_ids=(repo.farm_id,),
    )


@router.post("/jobs/{job_id}/completion/preview", dependencies=[Depends(require_farm_owner)])
def completion_preview(request: Request, repo: Repo, job_id: int, body: JobCompletion):
    try:
        row = service.job(repo, job_id)
        return service.completion_preview(
            repo,
            row,
            now=_now(request, repo),
            tz=_tz(request),
            **_completion_kwargs(body),
        )
    except (service.JobError, service.JobNotFound, service.JobConflict) as exc:
        raise _translate(exc) from None


@router.post("/jobs/{job_id}/complete", dependencies=[Depends(require_farm_owner)])
def complete_job(
    request: Request, repo: Repo, job_id: int, body: JobCompletion, tasks: BackgroundTasks
):
    try:
        row, _operation, replayed = service.complete(
            repo,
            _now(request, repo),
            _tz(request),
            job_id,
            reader=getattr(request.app.state, "spray_weather_reader", None),
            **_completion_kwargs(body),
        )
    except (service.JobError, service.JobNotFound, service.JobConflict) as exc:
        raise _translate(exc) from None
    if not replayed:
        _schedule_advice(request, repo, tasks)
    return service.job_json(repo, row, now=_now(request, repo), tz=_tz(request))


@router.post("/jobs/{job_id}/complete-preview-form", dependencies=[Depends(require_farm_owner)])
async def completion_preview_form(request: Request, repo: Repo, job_id: int):
    form = await request.form()
    try:
        body = _completion_body(form, _tz(request))
        row = service.job(repo, job_id)
        preview = service.completion_preview(
            repo,
            row,
            now=_now(request, repo),
            tz=_tz(request),
            **_completion_kwargs(body),
        )
    except service.JobNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except service.JobConflict as exc:
        return _back(repo, str(exc))
    except (ValueError, TypeError, service.JobError) as exc:
        return _back(repo, f"Check the completion details: {exc}")
    return render(
        request,
        "planning/completion_preview.html",
        preview=preview,
        completion_form={
            "job_id": job_id,
            "expected_revision": body.expected_revision,
            "request_key": body.request_key,
            "actual_start": str(form.get("actual_start", "")),
            "duration_minutes": body.duration_minutes,
            "parcel_ids": body.parcel_ids,
            "quantities": [
                {
                    "product_id": dose.product_id,
                    "value": str(dose.value),
                    "unit": dose.unit,
                }
                for dose in body.quantities
            ],
            "notes": body.notes,
        },
        timezone=_tz(request),
    )


@router.post("/jobs/{job_id}/complete-form", dependencies=[Depends(require_farm_owner)])
async def complete_form(request: Request, repo: Repo, job_id: int, tasks: BackgroundTasks):
    form = await request.form()
    try:
        body = _completion_body(form, _tz(request))
        _row, _operation, replayed = service.complete(
            repo,
            _now(request, repo),
            _tz(request),
            job_id,
            reader=getattr(request.app.state, "spray_weather_reader", None),
            **_completion_kwargs(body),
        )
    except service.JobNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except service.JobConflict as exc:
        return _back(repo, str(exc))
    except (ValueError, TypeError, service.JobError) as exc:
        return _back(repo, f"Check the completion details: {exc}")
    if not replayed:
        _schedule_advice(request, repo, tasks)
    return _back(repo)


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


@router.post("/jobs/{job_id}/assign-form", dependencies=[Depends(require_farm_owner)])
async def assign_form(request: Request, repo: Repo, job_id: int):
    form = await request.form()
    try:
        expected = int(str(form.get("expected_revision", "0")))
        changes: dict[str, int | None] = {}
        for kind in ("machine", "operator"):
            if f"{kind}_id" in form:
                raw = str(form.get(f"{kind}_id") or "").strip()
                changes[kind] = int(raw) if raw else None
        job = service.locked(repo, job_id)
        service.set_assignments(
            repo,
            _now(request, repo),
            _tz(request),
            job,
            expected_revision=expected,
            changes=changes,
        )
    except service.JobNotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except (service.JobConflict, service.ResourceConflict) as exc:
        return _back(repo, str(exc))
    except (ValueError, TypeError, service.JobError) as exc:
        return _back(repo, f"Check the resource booking: {exc}")
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

"""Persistent planned jobs: create, revise, cancel, and read them back as farm-scoped evidence.

A job freezes one stored assessment (`advice`) and its parameter version, the chosen product,
parcels, start and duration. Every mutation appends an immutable revision snapshot. Availability
is read from `forecast_availability` and recorded in that snapshot; it never silently becomes
"available". Planned work never writes a canonical log or stock row.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from farmcore.apps.advisor import service as advice_service
from farmcore.apps.fields.seed import alias_number
from farmcore.apps.forecast import availability
from farmcore.apps.operations import production
from farmcore.apps.operations.catalogue import products_for_farm
from farmcore.apps.planning import offers, resources
from farmcore.apps.planning.models import (
    PlanningJob,
    PlanningJobAssignment,
    PlanningJobParcel,
    PlanningJobRevision,
    PlanningMachine,
    PlanningOperator,
)
from farmcore.apps.review.models import ParameterVersion
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Advice, Farm, Parcel, Product

SCOPE = "planning:read"
WRITE = "planning:write"
MAX_DURATION = 1440


class JobError(ValueError):
    """Invalid planned-job input; the route answers 422."""


class JobNotFound(LookupError):
    """The job does not exist on this farm; the route answers 404."""


class JobConflict(RuntimeError):
    """The job is cancelled or the expected revision is stale; the route answers 409."""

    def __init__(self, message: str, current_revision: int | None = None) -> None:
        super().__init__(message)
        self.current_revision = current_revision


class ResourceConflict(RuntimeError):
    """A machine or operator is already booked for an overlapping window; the route answers 409."""


def _day(start: datetime, tz: str) -> date:
    return start.astimezone(ZoneInfo(tz)).date()


def _text(value: str, label: str, *, maximum: int) -> str:
    text = value.strip()
    if not text:
        raise JobError(f"{label} is required.")
    if len(text) > maximum:
        raise JobError(f"{label} is too long.")
    return text


def _parcels(repo: ScopedRepository, parcel_ids: list[int]) -> list[Parcel]:
    ids = list(dict.fromkeys(parcel_ids))
    if not ids:
        raise JobError("Choose at least one parcel.")
    if any(parcel_id <= 0 or parcel_id >= 2**63 for parcel_id in ids):
        raise JobError("Choose parcels from this farm.")
    rows = repo.list(Parcel, Parcel.id.in_(ids), scope="fields:read")
    if len(rows) != len(ids):
        raise JobError("A chosen parcel is not on this farm.")
    return sorted(rows, key=lambda p: (alias_number(p.alias), p.alias))


def _assessment(repo: ScopedRepository, assessment_id: int, now: datetime) -> Advice:
    row = repo.get(Advice, assessment_id, scope="advice:read")
    if row is None:
        raise JobError("Assessment is unavailable for this farm.")
    status = (row.props or {}).get("assessment_status")
    if status in ("limited", "unavailable"):
        raise JobError("Assessment is not evaluable; plan from current advice.")
    if row.decision not in ("spray", "wait"):
        raise JobError("Only spray or wait advice can start a planned job.")
    if row.evaluated_at > now:
        raise JobError("Assessment is from the future.")
    return row


def _version(repo: ScopedRepository, advice_row: Advice) -> tuple[int | None, str]:
    named = (advice_row.props or {}).get("config_version") or {}
    version_id = named.get("id")
    if version_id is not None and (
        not isinstance(version_id, int)
        or repo.get(ParameterVersion, version_id, scope="parameters:read") is None
    ):
        raise JobError("Parameter version is unavailable for this farm.")
    status = str(named.get("status") or "unversioned").strip() or "unversioned"
    return version_id, status


def _offered(repo: ScopedRepository) -> list[Product]:
    farm = repo.get(Farm, repo.farm_id, scope="logs:read")
    products = products_for_farm(repo, scope="logs:read")
    offered, _ = production.offers(products, production.mode(farm))
    return [offer.product for offer in offered]


def _product(repo: ScopedRepository, product_id: int) -> Product:
    if not 0 < product_id < 2**63:
        raise JobError("Choose a product from this farm's shortlist.")
    farm = repo.get(Farm, repo.farm_id, scope="logs:read")
    products = products_for_farm(repo, scope="logs:read")
    offered, _ = production.offers(products, production.mode(farm))
    for offer in offered:
        if offer.product.id == product_id:
            return offer.product
    if any(product.id == product_id for product in products):
        raise JobError(
            "Product is withheld in organic mode: no source in our product data shows its "
            "active substances are allowed in organic production."
        )
    raise JobError("Choose a product from this farm's shortlist.")


def _state(repo: ScopedRepository, start: datetime, tz: str) -> dict[str, object]:
    return availability.day_state(repo, _day(start, tz))


def _snapshot(
    job: PlanningJob,
    parcel_ids: list[int],
    state: dict[str, object],
    assignments: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "name": job.name,
        "status": job.status,
        "revision": job.revision,
        "assessment_id": job.assessment_id,
        "config_version": {"id": job.config_version_id, "status": job.config_version_status},
        "product_id": job.product_id,
        "scheduled_start": job.scheduled_start.isoformat(),
        "duration_minutes": job.duration_minutes,
        "parcels": parcel_ids,
        "availability": state,
        "assignments": assignments,
        "notes": job.notes,
    }


def _revision(
    job: PlanningJob,
    *,
    action: str,
    snapshot: dict[str, object],
    actor: str,
    reason: str,
    now: datetime,
) -> PlanningJobRevision:
    return PlanningJobRevision(
        farm_id=job.farm_id,
        job_id=job.id,
        revision=job.revision,
        action=action,
        status=job.status,
        snapshot=snapshot,
        actor=actor,
        reason=reason,
        created_at=now,
    )


def create(
    repo: ScopedRepository,
    now: datetime,
    tz: str,
    *,
    name: str,
    parcel_ids: list[int],
    assessment_id: int,
    product_id: int,
    scheduled_start: datetime,
    duration_minutes: int,
    notes: str = "",
) -> PlanningJob:
    if scheduled_start.tzinfo is None:
        raise JobError("Choose a start with a timezone.")
    repo.check(WRITE, "planning_job")
    if not 1 <= duration_minutes <= MAX_DURATION:
        raise JobError("Duration must be between 1 and 1440 minutes.")
    advice_row = _assessment(repo, assessment_id, now)
    parcels = _parcels(repo, parcel_ids)
    if advice_row.parcel_id not in {parcel.id for parcel in parcels}:
        raise JobError("The assessment's parcel must be one of the job's parcels.")
    _product(repo, product_id)
    version_id, version_status = _version(repo, advice_row)
    job = PlanningJob(
        farm_id=repo.farm_id,
        status="planned",
        revision=1,
        name=_text(name, "Job name", maximum=200),
        assessment_id=advice_row.id,
        config_version_id=version_id,
        config_version_status=version_status,
        product_id=product_id,
        scheduled_start=scheduled_start,
        duration_minutes=duration_minutes,
        notes=notes,
        created_by=repo.actor,
        created_at=now,
        updated_by=repo.actor,
        updated_at=now,
        props={},
    )
    repo.session.add(job)
    repo.session.flush()
    for parcel in parcels:
        repo.session.add(
            PlanningJobParcel(farm_id=repo.farm_id, job_id=job.id, parcel_id=parcel.id)
        )
    repo.session.flush()
    state = _state(repo, job.scheduled_start, tz)
    job.props = {"availability": state}
    job.updated_at = now
    parcel_ids_now = [parcel.id for parcel in parcels]
    repo.session.add(
        _revision(
            job,
            action="created",
            snapshot=_snapshot(job, parcel_ids_now, state, []),
            actor=repo.actor,
            reason="",
            now=now,
        )
    )
    repo.session.flush()
    return job


def locked(repo: ScopedRepository, job_id: int) -> PlanningJob:
    if not 0 < job_id < 2**63:
        raise JobNotFound("Planned job not found.")
    stmt = (
        select(PlanningJob)
        .where(PlanningJob.id == job_id, PlanningJob.farm_id == repo.farm_id)
        .with_for_update()
    )
    job = repo.execute(stmt, scope=SCOPE, resource="planning_job").scalar_one_or_none()
    if job is None:
        raise JobNotFound("Planned job not found.")
    return job


def edit(
    repo: ScopedRepository,
    now: datetime,
    tz: str,
    job: PlanningJob,
    *,
    expected_revision: int,
    changes: dict[str, object],
) -> PlanningJob:
    repo.check(WRITE, "planning_job")
    if job.status != "planned":
        raise JobConflict("Cancelled jobs cannot be edited.", job.revision)
    if expected_revision != job.revision:
        raise JobConflict(
            f"Revision conflict: the job is at revision {job.revision}.", job.revision
        )
    changes = {key: value for key, value in changes.items() if value is not None}
    if not changes:
        raise JobError("No changes were provided.")
    old_start, old_duration = job.scheduled_start, job.duration_minutes
    new_start = changes.get("scheduled_start", job.scheduled_start)
    new_duration = changes.get("duration_minutes", job.duration_minutes)
    if not isinstance(new_start, datetime):
        new_start = job.scheduled_start
    new_end = new_start + timedelta(minutes=int(new_duration))  # type: ignore[arg-type]
    if (old_start, old_duration) != (new_start, new_duration):
        for assignment in repo.list(
            PlanningJobAssignment, PlanningJobAssignment.job_id == job.id, scope=SCOPE
        ):
            kind, resource_id = _resource_label(assignment)
            message = _booking_conflict(
                repo,
                kind=kind,
                resource_id=resource_id,
                start=new_start,
                end=new_end,
                job_id=job.id,
                tz=tz,
            )
            if message:
                raise JobConflict(message, job.revision)

    advice_row = _assessment(repo, job.assessment_id, now)
    if "parcel_ids" in changes:
        parcels = _parcels(repo, list(changes["parcel_ids"]))  # type: ignore[arg-type]
        if advice_row.parcel_id not in {parcel.id for parcel in parcels}:
            raise JobError("The assessment's parcel must be one of the job's parcels.")
        repo.session.execute(delete(PlanningJobParcel).where(PlanningJobParcel.job_id == job.id))
        for parcel in parcels:
            repo.session.add(
                PlanningJobParcel(farm_id=repo.farm_id, job_id=job.id, parcel_id=parcel.id)
            )
        repo.session.flush()
    if "product_id" in changes:
        _product(repo, int(changes["product_id"]))  # type: ignore[arg-type]
        job.product_id = int(changes["product_id"])  # type: ignore[arg-type]
    if "scheduled_start" in changes:
        start = changes["scheduled_start"]
        if not isinstance(start, datetime) or start.tzinfo is None:
            raise JobError("Choose a start with a timezone.")
        job.scheduled_start = start
    if "duration_minutes" in changes:
        duration = int(changes["duration_minutes"])  # type: ignore[arg-type]
        if not 1 <= duration <= MAX_DURATION:
            raise JobError("Duration must be between 1 and 1440 minutes.")
        job.duration_minutes = duration
    if "name" in changes:
        job.name = _text(str(changes["name"]), "Job name", maximum=200)
    if "notes" in changes:
        job.notes = str(changes["notes"])

    if (old_start, old_duration) != (job.scheduled_start, job.duration_minutes):
        end = job.scheduled_start + timedelta(minutes=job.duration_minutes)
        for assignment in repo.list(
            PlanningJobAssignment, PlanningJobAssignment.job_id == job.id, scope=SCOPE
        ):
            assignment.starts_at = job.scheduled_start
            assignment.ends_at = end
            assignment.updated_by = repo.actor
            assignment.updated_at = now
        current_revision = job.revision
        try:
            repo.session.flush()
        except IntegrityError as exc:
            if "ex_planning_job_assignment" in str(exc.orig):
                repo.session.rollback()
                raise JobConflict(
                    "A booked resource already has another job at the new time.",
                    current_revision,
                ) from None
            raise

    job.revision += 1
    job.updated_by = repo.actor
    state = _state(repo, job.scheduled_start, tz)
    job.props = {**(job.props or {}), "availability": state}
    job.updated_at = now
    links = repo.session.scalars(
        select(PlanningJobParcel)
        .where(PlanningJobParcel.job_id == job.id)
        .order_by(PlanningJobParcel.parcel_id)
    ).all()
    repo.session.add(
        _revision(
            job,
            action="edited",
            snapshot=_snapshot(
                job,
                [link.parcel_id for link in links],
                state,
                _snapshot_assignments(_assigned_rows(repo, job.id)),
            ),
            actor=repo.actor,
            reason="",
            now=now,
        )
    )
    repo.session.flush()
    return job


def cancel(
    repo: ScopedRepository,
    now: datetime,
    tz: str,
    job: PlanningJob,
    *,
    expected_revision: int,
    reason: str = "",
) -> PlanningJob:
    repo.check(WRITE, "planning_job")
    if job.status != "planned":
        raise JobConflict("The job is already cancelled.", job.revision)
    if expected_revision != job.revision:
        raise JobConflict(
            f"Revision conflict: the job is at revision {job.revision}.", job.revision
        )
    job.status = "cancelled"
    job.cancelled_by = repo.actor
    job.cancelled_at = now
    job.revision += 1
    job.updated_by = repo.actor
    state = _state(repo, job.scheduled_start, tz)
    job.props = {**(job.props or {}), "availability": state}
    job.updated_at = now
    links = repo.session.scalars(
        select(PlanningJobParcel).where(PlanningJobParcel.job_id == job.id)
    ).all()
    assignments = _snapshot_assignments(_assigned_rows(repo, job.id))
    repo.session.add(
        _revision(
            job,
            action="cancelled",
            snapshot=_snapshot(job, [link.parcel_id for link in links], state, assignments),
            actor=repo.actor,
            reason=reason.strip(),
            now=now,
        )
    )
    repo.session.execute(
        delete(PlanningJobAssignment).where(PlanningJobAssignment.job_id == job.id)
    )
    repo.session.flush()
    return job


def _resource_label(assignment: PlanningJobAssignment) -> tuple[str, int]:
    if assignment.machine_id is not None:
        return "machine", assignment.machine_id
    return "operator", int(assignment.operator_id or 0)


def _assigned_rows(repo: ScopedRepository, job_id: int) -> list[dict[str, object]]:
    """Live assignments for one job, with the configured resource's display fields."""
    rows: list[dict[str, object]] = []
    for assignment in repo.list(
        PlanningJobAssignment, PlanningJobAssignment.job_id == job_id, scope=SCOPE
    ):
        kind, resource_id = _resource_label(assignment)
        model = PlanningMachine if kind == "machine" else PlanningOperator
        resource = repo.get(model, resource_id, scope=SCOPE)
        rows.append(
            {
                "kind": kind,
                "id": resource_id,
                "name": resource.name if resource else "Removed resource",
                "active": bool(resource.active) if resource else False,
                "work_rate_ha_per_hour": (
                    float(resource.work_rate_ha_per_hour)
                    if kind == "machine" and resource and resource.work_rate_ha_per_hour
                    else None
                ),
                "tank_litres": (
                    float(resource.tank_litres)
                    if kind == "machine" and resource and resource.tank_litres
                    else None
                ),
                "starts_at": assignment.starts_at,
                "ends_at": assignment.ends_at,
            }
        )
    return sorted(rows, key=lambda row: (str(row["kind"]), int(row["id"])))


def _snapshot_assignments(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "kind": row["kind"],
            "id": row["id"],
            "name": row["name"],
            "starts_at": row["starts_at"].isoformat(),  # type: ignore[union-attr]
            "ends_at": row["ends_at"].isoformat(),  # type: ignore[union-attr]
        }
        for row in rows
    ]


def _booking_conflict(
    repo: ScopedRepository,
    *,
    kind: str,
    resource_id: int,
    start: datetime,
    end: datetime,
    job_id: int,
    tz: str,
) -> str | None:
    """Another job already booking this resource over the half-open window, if any."""
    column = (
        PlanningJobAssignment.machine_id if kind == "machine" else PlanningJobAssignment.operator_id
    )
    for other in repo.list(PlanningJobAssignment, column == resource_id, scope=SCOPE):
        if other.job_id == job_id:
            continue
        if other.starts_at < end and start < other.ends_at:
            model = PlanningMachine if kind == "machine" else PlanningOperator
            resource = repo.get(model, resource_id, scope=SCOPE)
            name = resource.name if resource else "That resource"
            return (
                f"{name} is already booked "
                f"{other.starts_at.astimezone(ZoneInfo(tz)):%d %b %H:%M}–"
                f"{other.ends_at.astimezone(ZoneInfo(tz)):%d %b %H:%M}."
            )
    return None


def set_assignments(
    repo: ScopedRepository,
    now: datetime,
    tz: str,
    job: PlanningJob,
    *,
    expected_revision: int,
    changes: dict[str, int | None],
) -> PlanningJob:
    """Book (or clear) one machine and/or operator on the job's current window.

    A new revision records the booking. The database exclusion constraint is the final
    authority; the pre-check gives the farmer a clear conflict message without relying on it.
    """
    repo.check(WRITE, "planning_job")
    if job.status != "planned":
        raise JobConflict("Cancelled jobs cannot be rebooked.", job.revision)
    if expected_revision != job.revision:
        raise JobConflict(
            f"Revision conflict: the job is at revision {job.revision}.", job.revision
        )
    changes = {kind: value for kind, value in changes.items() if kind in ("machine", "operator")}
    if not changes:
        raise JobError("No resource changes were provided.")
    start = job.scheduled_start
    end = start + timedelta(minutes=job.duration_minutes)
    current = {
        _resource_label(row)[0]: row
        for row in repo.list(
            PlanningJobAssignment, PlanningJobAssignment.job_id == job.id, scope=SCOPE
        )
    }
    try:
        with repo.session.begin_nested():
            for kind, resource_id in changes.items():
                existing = current.get(kind)
                if resource_id is None:
                    if existing is not None:
                        repo.session.delete(existing)
                    continue
                model = PlanningMachine if kind == "machine" else PlanningOperator
                resource = repo.get(model, resource_id, scope=SCOPE)
                if resource is None:
                    raise JobError("That machine or operator is not configured on this farm.")
                if not resource.active:
                    raise JobError("That machine or operator is inactive.")
                message = _booking_conflict(
                    repo,
                    kind=kind,
                    resource_id=resource_id,
                    start=start,
                    end=end,
                    job_id=job.id,
                    tz=tz,
                )
                if message:
                    raise ResourceConflict(message)
                if existing is None:
                    repo.session.add(
                        PlanningJobAssignment(
                            farm_id=repo.farm_id,
                            job_id=job.id,
                            machine_id=resource_id if kind == "machine" else None,
                            operator_id=resource_id if kind == "operator" else None,
                            starts_at=start,
                            ends_at=end,
                            created_by=repo.actor,
                            created_at=now,
                            updated_by=repo.actor,
                            updated_at=now,
                        )
                    )
                else:
                    existing.starts_at = start
                    existing.ends_at = end
                    existing.updated_by = repo.actor
                    existing.updated_at = now
            repo.session.flush()
    except IntegrityError as exc:
        if "ex_planning_job_assignment" in str(exc.orig):
            repo.session.rollback()
            raise ResourceConflict(
                "That machine or operator is already booked for an overlapping time."
            ) from None
        raise

    job.revision += 1
    job.updated_by = repo.actor
    state = _state(repo, job.scheduled_start, tz)
    job.props = {**(job.props or {}), "availability": state}
    job.updated_at = now
    links = repo.session.scalars(
        select(PlanningJobParcel).where(PlanningJobParcel.job_id == job.id)
    ).all()
    repo.session.add(
        _revision(
            job,
            action="edited",
            snapshot=_snapshot(
                job,
                [link.parcel_id for link in links],
                state,
                _snapshot_assignments(_assigned_rows(repo, job.id)),
            ),
            actor=repo.actor,
            reason="",
            now=now,
        )
    )
    repo.session.flush()
    return job


def _job_parcels(repo: ScopedRepository, job_id: int) -> list[Parcel]:
    links = repo.list(PlanningJobParcel, PlanningJobParcel.job_id == job_id, scope=SCOPE)
    ids = [link.parcel_id for link in links]
    if not ids:
        return []
    return sorted(
        repo.list(Parcel, Parcel.id.in_(ids), scope="fields:read"),
        key=lambda p: (alias_number(p.alias), p.alias),
    )


def job_json(
    repo: ScopedRepository,
    job: PlanningJob,
    *,
    tz: str,
    now: datetime | None = None,
    forecast: offers.Forecast | None = None,
) -> dict[str, object]:
    advice_row = repo.get(Advice, job.assessment_id, scope="advice:read")
    product = repo.get(Product, job.product_id, scope="logs:read")
    parcels = _job_parcels(repo, job.id)
    assigned = _assigned_rows(repo, job.id)
    assessed_at = now or job.updated_at
    return {
        "id": job.id,
        "farm_id": job.farm_id,
        "status": job.status,
        "revision": job.revision,
        "name": job.name,
        "parcels": [
            {"id": parcel.id, "alias": parcel.alias, "name": parcel.name} for parcel in parcels
        ],
        "assessment": {
            "id": job.assessment_id,
            "parcel_id": advice_row.parcel_id if advice_row else None,
            "evaluated_at": advice_row.evaluated_at if advice_row else None,
            "decision": advice_row.decision if advice_row else None,
            "assessment_status": (
                (advice_row.props or {}).get("assessment_status") if advice_row else None
            ),
            "config_version": {
                "id": job.config_version_id,
                "status": job.config_version_status,
                "accuracy_claim": False,
            },
        },
        "product": (
            {
                "id": product.id,
                "name": product.name,
                "active_substance": product.active_substance,
                "authorisation_no": product.authorisation_no,
            }
            if product
            else None
        ),
        "scheduled_start": job.scheduled_start,
        "scheduled_start_local": job.scheduled_start.astimezone(ZoneInfo(tz)).strftime(
            "%Y-%m-%dT%H:%M"
        ),
        "scheduled_end": job.scheduled_start + timedelta(minutes=job.duration_minutes),
        "duration_minutes": job.duration_minutes,
        "notes": job.notes,
        "availability": _state(repo, job.scheduled_start, tz),
        "assignments": assigned,
        "resource_assessment": offers.job_assessment(
            repo,
            job,
            now=assessed_at,
            tz=tz,
            assigned=assigned,
            parcels=parcels,
            product=product,
            forecast=forecast,
        ),
        "created_by": job.created_by,
        "created_at": job.created_at,
        "updated_by": job.updated_by,
        "updated_at": job.updated_at,
        "cancelled_by": job.cancelled_by,
        "cancelled_at": job.cancelled_at,
    }


def list_jobs(repo: ScopedRepository, now: datetime, *, tz: str) -> list[dict[str, object]]:
    jobs = repo.list(
        PlanningJob,
        scope=SCOPE,
        order_by=(PlanningJob.scheduled_start, PlanningJob.id),
    )
    jobs.sort(key=lambda job: (job.status == "cancelled", job.scheduled_start, job.id))
    forecast = offers.forecast_context(repo, now, tz)
    return [job_json(repo, job, now=now, tz=tz, forecast=forecast) for job in jobs]


def page_context(repo: ScopedRepository, now: datetime, tz: str) -> dict[str, object]:
    parcels = sorted(
        repo.list(Parcel, scope="fields:read"),
        key=lambda p: (alias_number(p.alias), p.alias),
    )
    latest = advice_service.latest_advice(repo, now, tz=tz)
    assessments = [
        {
            "id": row.id,
            "parcel_id": parcel_id,
            "evaluated_at": row.evaluated_at.isoformat(),
            "decision": row.decision,
            "assessment_status": (row.props or {}).get("assessment_status"),
        }
        for parcel_id, row in sorted(latest.items())
        if row.decision in ("spray", "wait")
        and (row.props or {}).get("assessment_status") not in ("limited", "unavailable")
    ]
    return {
        "jobs": list_jobs(repo, now, tz=tz),
        "parcels": parcels,
        "products": _offered(repo),
        "assessments": assessments,
        **resources.context(repo),
    }

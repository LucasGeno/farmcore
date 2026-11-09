"""Persistent planned jobs: create, revise, cancel, and read them back as farm-scoped evidence.

A job freezes one stored assessment (`advice`) and its parameter version, the chosen product,
parcels, start and duration. Every mutation appends an immutable revision snapshot. Availability
is read from `forecast_availability` and recorded in that snapshot; it never silently becomes
"available". Planned work never writes a canonical log or stock row.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from farmcore.apps.advisor import service as advice_service
from farmcore.apps.fields.seed import alias_number
from farmcore.apps.forecast import availability
from farmcore.apps.operations import production, spray_weather
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
from farmcore.core import logs
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Advice, Farm, Log, LogSource, Parcel, Product

SCOPE = "planning:read"
WRITE = "planning:write"
MAX_DURATION = 1440
COMPLETION = "completion"
PLANNING_SOURCE = "planning"
RATE_UNITS = ("kg/ha", "L/ha", "l/ha", "g/ha", "ml/ha")
MAX_QUANTITIES = 50
MAX_NOTES = 10000


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
        .execution_options(populate_existing=True)
    )
    job = repo.execute(stmt, scope=SCOPE, resource="planning_job").scalar_one_or_none()
    if job is None:
        raise JobNotFound("Planned job not found.")
    return job


def job(repo: ScopedRepository, job_id: int) -> PlanningJob:
    """The job without a row lock, for a read-only preview."""
    if not 0 < job_id < 2**63:
        raise JobNotFound("Planned job not found.")
    row = repo.get(PlanningJob, job_id, scope=SCOPE)
    if row is None:
        raise JobNotFound("Planned job not found.")
    return row


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
    time_changed = (old_start, old_duration) != (new_start, new_duration)
    # Read the bookings before mutating the job: the access check flushes, and a later job
    # constraint failure must reach the handler below, not escape through an autoflush query.
    assignments = (
        repo.list(PlanningJobAssignment, PlanningJobAssignment.job_id == job.id, scope=SCOPE)
        if time_changed
        else []
    )
    if time_changed:
        for assignment in assignments:
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

    if time_changed:
        end = job.scheduled_start + timedelta(minutes=job.duration_minutes)
        for assignment in assignments:
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
        if "fk_planning_job_assignment_" in str(exc.orig):
            # The resource was removed after the read but before this flush.
            repo.session.rollback()
            raise ResourceConflict(
                "That machine or operator is no longer configured on this farm."
            ) from None
        raise  # pragma: no cover - other assignment constraints are pre-validated

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


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _raw_quantities(quantities: list[dict[str, object]]) -> list[dict[str, object]]:
    """Validate the submitted shape and normalise doses before any database lookup."""
    if not 1 <= len(quantities) <= MAX_QUANTITIES:
        raise JobError("Enter at least one applied product.")
    out: list[dict[str, object]] = []
    for item in quantities:
        try:
            product_id = int(item["product_id"])  # type: ignore[arg-type]
            value = Decimal(str(item["value"]))
            unit = str(item["unit"])
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise JobError(
                "Each applied product needs a product, a positive dose and a rate unit."
            ) from None
        if not 0 < product_id < 2**63:
            raise JobError("Choose products from this farm's shortlist.")
        if not value.is_finite() or value <= 0:
            raise JobError("A dose must be positive.")
        if unit not in RATE_UNITS:
            raise JobError("Use a spray rate unit: kg/ha, L/ha, l/ha, g/ha or ml/ha.")
        out.append({"product_id": product_id, "value": _decimal_text(value), "unit": unit})
    if len({int(q["product_id"]) for q in out}) != len(out):
        raise JobError("List each product once.")
    return sorted(out, key=lambda q: (int(q["product_id"]), str(q["unit"])))


def _fingerprint(
    *,
    request_key: str,
    actual_start: datetime,
    duration_minutes: int,
    parcel_ids: list[int],
    quantities: list[dict[str, object]],
    notes: str,
) -> str:
    value = {
        "request_key": request_key,
        "actual_start": actual_start.astimezone(UTC).isoformat(),
        "duration_minutes": int(duration_minutes),
        "parcel_ids": sorted(set(int(parcel_id) for parcel_id in parcel_ids)),
        "quantities": [[int(q["product_id"]), str(q["value"]), str(q["unit"])] for q in quantities],
        "notes": notes,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _completion_inputs(
    repo: ScopedRepository,
    *,
    now: datetime,
    actual_start: datetime,
    duration_minutes: int,
    parcel_ids: list[int],
    quantities: list[dict[str, object]],
    notes: str,
) -> tuple[list[Parcel], list[dict[str, object]]]:
    if actual_start.tzinfo is None:
        raise JobError("Choose an actual start with a timezone.")
    if not 1 <= duration_minutes <= MAX_DURATION:
        raise JobError("Duration must be between 1 and 1440 minutes.")
    if len(notes) > MAX_NOTES:
        raise JobError("Notes are too long.")
    actual_end = actual_start + timedelta(minutes=duration_minutes)
    if actual_start > now or actual_end > now:
        raise JobError("Actual work cannot start or end after the farm clock's now.")
    parcels = _parcels(repo, parcel_ids)
    doses = []
    for quantity in _raw_quantities(quantities):
        product = _product(repo, int(quantity["product_id"]))
        doses.append(
            {
                "product_id": product.id,
                "product": product.name,
                "value": quantity["value"],
                "unit": quantity["unit"],
            }
        )
    return parcels, doses


def _completion_target(job: PlanningJob, expected_revision: int) -> None:
    if job.status == "completed":
        raise JobConflict("This planned job is already completed.", job.revision)
    if job.status != "planned":
        raise JobConflict("Only planned jobs can be completed.", job.revision)
    if expected_revision != job.revision:
        raise JobConflict(
            f"Revision conflict: the job is at revision {job.revision}.", job.revision
        )


def _clock_text(value: datetime, tz: str) -> str:
    return value.astimezone(ZoneInfo(tz)).strftime("%d %b %H:%M")


def _difference_lines(
    job: PlanningJob,
    planned_parcels: list[Parcel],
    planned_product: Product | None,
    actual_start: datetime,
    duration_minutes: int,
    parcels: list[Parcel],
    doses: list[dict[str, object]],
    tz: str,
) -> list[str]:
    lines: list[str] = []
    if job.scheduled_start != actual_start:
        lines.append(
            f"Start: planned {_clock_text(job.scheduled_start, tz)}, "
            f"actual {_clock_text(actual_start, tz)}."
        )
    if job.duration_minutes != duration_minutes:
        lines.append(
            f"Duration: planned {job.duration_minutes} min, actual {duration_minutes} min."
        )
    planned_ids = {parcel.id for parcel in planned_parcels}
    actual_ids = {parcel.id for parcel in parcels}
    added = [p.alias for p in parcels if p.id not in planned_ids]
    removed = [p.alias for p in planned_parcels if p.id not in actual_ids]
    if added:
        lines.append(f"Parcels added: {', '.join(added)}.")
    if removed:
        lines.append(f"Parcels not treated: {', '.join(removed)}.")
    actual_products = [str(dose["product"]) for dose in doses]
    planned_name = planned_product.name if planned_product else None
    if actual_products != ([planned_name] if planned_name else []):
        lines.append(
            f"Products: planned {planned_name or 'unknown'}, actual {', '.join(actual_products)}."
        )
    return lines or ["Actual work matches the saved plan."]


def completion_preview(
    repo: ScopedRepository,
    job: PlanningJob,
    *,
    now: datetime,
    expected_revision: int,
    request_key: str,
    actual_start: datetime,
    duration_minutes: int,
    parcel_ids: list[int],
    quantities: list[dict[str, object]],
    notes: str = "",
    tz: str,
) -> dict[str, object]:
    """What completing this job would record, and how it differs from the saved plan. No writes."""
    repo.check(SCOPE, "planning_job")
    _completion_target(job, expected_revision)
    key = _text(request_key, "Request key", maximum=128)
    parcels, doses = _completion_inputs(
        repo,
        now=now,
        actual_start=actual_start,
        duration_minutes=duration_minutes,
        parcel_ids=parcel_ids,
        quantities=quantities,
        notes=notes,
    )
    planned_parcels = _job_parcels(repo, job.id)
    planned_product = repo.get(Product, job.product_id, scope="logs:read")
    end = actual_start + timedelta(minutes=duration_minutes)
    state = _state(repo, actual_start, tz)
    advice_row = repo.get(Advice, job.assessment_id, scope="advice:read")
    return {
        "job_id": job.id,
        "revision": job.revision,
        "plan_revision": job.revision,
        "request_key": key,
        "planned": {
            "scheduled_start": job.scheduled_start,
            "scheduled_end": job.scheduled_start + timedelta(minutes=job.duration_minutes),
            "duration_minutes": job.duration_minutes,
            "parcel_ids": [parcel.id for parcel in planned_parcels],
            "product": (
                {"id": planned_product.id, "name": planned_product.name}
                if planned_product
                else None
            ),
        },
        "actual": {
            "start": actual_start,
            "end": end,
            "duration_minutes": duration_minutes,
            "parcel_ids": sorted(parcel.id for parcel in parcels),
            "quantities": doses,
        },
        "differences": _difference_lines(
            job,
            planned_parcels,
            planned_product,
            actual_start,
            duration_minutes,
            parcels,
            doses,
            tz,
        ),
        "assessment": {
            "id": job.assessment_id,
            "evaluated_at": advice_row.evaluated_at if advice_row else None,
            "decision": advice_row.decision if advice_row else None,
            "config_version": {
                "id": job.config_version_id,
                "status": job.config_version_status,
                "accuracy_claim": False,
            },
        },
        "availability": state,
        "operation": {
            "type": "spray",
            "status": "done",
            "ts_start": actual_start,
            "ts_end": end,
            "parcel_ids": sorted(parcel.id for parcel in parcels),
            "quantities": doses,
            "source": PLANNING_SOURCE,
        },
    }


def complete(
    repo: ScopedRepository,
    now: datetime,
    tz: str,
    job_id: int,
    *,
    expected_revision: int,
    request_key: str,
    actual_start: datetime,
    duration_minutes: int,
    parcel_ids: list[int],
    quantities: list[dict[str, object]],
    notes: str = "",
    reader=None,
) -> tuple[PlanningJob, Log, bool]:
    """Complete the job into one confirmed operation, or return the same operation on a retry.

    The farm advisory lock serialises completions before the job row lock. A second session
    therefore sees the first completion instead of creating a second log or stock movement.
    """
    repo.check(WRITE, "planning_job")
    logs.lock_farm(repo.session, repo.farm_id)
    job_row = locked(repo, job_id)
    key = _text(request_key, "Request key", maximum=128)
    raw = _raw_quantities(quantities)
    fingerprint = _fingerprint(
        request_key=key,
        actual_start=actual_start,
        duration_minutes=duration_minutes,
        parcel_ids=parcel_ids,
        quantities=raw,
        notes=notes,
    )
    if job_row.status == "completed":
        if job_row.operation_request_key != key:
            raise JobConflict("This planned job is already completed.", job_row.revision)
        stored = (job_row.props or {}).get(COMPLETION) or {}
        if stored.get("fingerprint") != fingerprint:
            raise JobConflict("Request key already used for another completion.", job_row.revision)
        operation = repo.get(Log, job_row.operation_log_id, scope="logs:read")
        if operation is None:  # pragma: no cover - planning_0003 FK keeps the operation
            raise JobConflict("The linked operation is unavailable.", job_row.revision)
        return job_row, operation, True
    _completion_target(job_row, expected_revision)
    parcels, doses = _completion_inputs(
        repo,
        now=now,
        actual_start=actual_start,
        duration_minutes=duration_minutes,
        parcel_ids=parcel_ids,
        quantities=quantities,
        notes=notes,
    )
    end = actual_start + timedelta(minutes=duration_minutes)
    assignments = _snapshot_assignments(_assigned_rows(repo, job_row.id))
    external_key = f"job:{job_row.id}:{key}"
    operation, merged = logs.record_operation(
        repo.session,
        farm_id=repo.farm_id,
        type="spray",
        parcel_ids=[parcel.id for parcel in parcels],
        ts_start=actual_start,
        ts_end=end,
        quantities=[
            logs.QuantityInput(
                measure="rate",
                value=Decimal(str(dose["value"])),
                unit=str(dose["unit"]),
                product_id=int(dose["product_id"]),
                label=str(dose["product"]),
            )
            for dose in doses
        ],
        source=logs.SourceInput(
            source_type=PLANNING_SOURCE,
            external_key=external_key,
            raw_payload={
                "job_id": job_row.id,
                "request_key": key,
                "plan_revision": job_row.revision,
                "assessment_id": job_row.assessment_id,
                "config_version": {
                    "id": job_row.config_version_id,
                    "status": job_row.config_version_status,
                },
            },
            confidence=None,
            precision_rank=logs.RANK_PLANNING,
        ),
        actor=repo.actor,
        occurred_at=now,
        status="done",
        name=job_row.name,
        source_app="planning",
    )
    planning_source = next(
        (
            source
            for source in operation.sources
            if source.source_type == PLANNING_SOURCE and source.external_key == external_key
        ),
        None,
    )
    if planning_source is not None:
        # The immutable weather receipt belongs to this completion's own source and time,
        # before a merge could move other evidence onto the log.
        spray_weather.capture(
            repo,
            [
                spray_weather.Receipt(
                    planning_source.id, actual_start, tuple(parcel.id for parcel in parcels)
                )
            ],
            reader=reader,
            data_now=now,
            captured_at=datetime.now(UTC),
        )
    state = _state(repo, actual_start, tz)
    completion = {
        "request_key": key,
        "fingerprint": fingerprint,
        "actual_start": actual_start.isoformat(),
        "actual_end": end.isoformat(),
        "actual_start_local": actual_start.astimezone(ZoneInfo(tz)).strftime("%d %b %Y %H:%M"),
        "actual_end_local": end.astimezone(ZoneInfo(tz)).strftime("%d %b %Y %H:%M"),
        "duration_minutes": duration_minutes,
        "parcel_ids": sorted(parcel.id for parcel in parcels),
        "quantities": doses,
        "notes": notes,
        "operation_log_id": operation.id,
        "merged": merged,
        "availability": state,
    }
    job_row.status = "completed"
    job_row.completed_by = repo.actor
    job_row.completed_at = now
    job_row.operation_log_id = operation.id
    job_row.operation_request_key = key
    job_row.revision += 1
    job_row.updated_by = repo.actor
    job_row.updated_at = now
    job_row.props = {**(job_row.props or {}), COMPLETION: completion}
    # No autoflush between these mutations and the explicit flush: a constraint failure must
    # roll the canonical operation and the job update back together.
    with repo.session.no_autoflush:
        links = repo.session.scalars(
            select(PlanningJobParcel).where(PlanningJobParcel.job_id == job_row.id)
        ).all()
        snapshot = {
            **_snapshot(
                job_row,
                [link.parcel_id for link in links],
                state,
                assignments,
            ),
            COMPLETION: completion,
        }
        repo.session.add(
            _revision(
                job_row,
                action="completed",
                snapshot=snapshot,
                actor=repo.actor,
                reason=notes.strip()[:2000],
                now=now,
            )
        )
        # A completed job no longer reserves a machine or operator.
        repo.session.execute(
            delete(PlanningJobAssignment).where(PlanningJobAssignment.job_id == job_row.id)
        )
    try:
        repo.session.flush()
    except IntegrityError as exc:
        if "ix_planning_job_farm_request_key" in str(exc.orig):
            repo.session.rollback()
            raise JobConflict("This completion request key is already in use.") from None
        raise
    return job_row, operation, False


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
    saved_assignments = []
    if job.status != "planned":
        revisions = repo.list(
            PlanningJobRevision,
            PlanningJobRevision.job_id == job.id,
            PlanningJobRevision.revision == job.revision,
            scope=SCOPE,
        )
        if revisions:
            saved_assignments = revisions[0].snapshot.get("assignments", [])
    assessed_at = now or job.updated_at
    completion = (job.props or {}).get(COMPLETION)
    if isinstance(completion, dict):
        completion = {key: value for key, value in completion.items() if key != "fingerprint"}
    else:
        completion = None
    operation = None
    if job.operation_log_id is not None:
        row = repo.get(Log, job.operation_log_id, scope="logs:read")
        if row is not None:
            source_types = sorted(
                {
                    source.source_type
                    for source in repo.list(
                        LogSource, LogSource.log_id == row.id, scope="logs:read"
                    )
                }
            )
            operation = {
                "log_id": row.id,
                "type": row.type,
                "status": row.status,
                "name": row.name,
                "ts_start": row.ts_start,
                "ts_end": row.ts_end,
                "source_types": source_types,
            }
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
        "saved_assignments": saved_assignments,
        "resource_assessment": offers.job_assessment(
            repo,
            job,
            now=assessed_at,
            tz=tz,
            assigned=assigned,
            parcels=parcels,
            product=product,
            forecast=forecast,
        )
        if job.status == "planned"
        else None,
        "created_by": job.created_by,
        "created_at": job.created_at,
        "updated_by": job.updated_by,
        "updated_at": job.updated_at,
        "cancelled_by": job.cancelled_by,
        "cancelled_at": job.cancelled_at,
        "completed_by": job.completed_by,
        "completed_at": job.completed_at,
        "operation_log_id": job.operation_log_id,
        "completion": completion,
        "operation": operation,
    }


def list_jobs(repo: ScopedRepository, now: datetime, *, tz: str) -> list[dict[str, object]]:
    jobs = repo.list(
        PlanningJob,
        scope=SCOPE,
        order_by=(PlanningJob.scheduled_start, PlanningJob.id),
    )
    order = {"planned": 0, "completed": 1, "cancelled": 2}
    jobs.sort(key=lambda job: (order.get(job.status, 3), job.scheduled_start, job.id))
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
    jobs = list_jobs(repo, now, tz=tz)
    for item in jobs:
        if item["status"] == "planned":
            item["completion_request_key"] = secrets.token_urlsafe(18)
    return {
        "jobs": jobs,
        "parcels": parcels,
        "products": _offered(repo),
        "assessments": assessments,
        **resources.context(repo),
    }

"""Resource assessment for a planned job and its side-effect-free alternatives.

The states come from the pure `engine`; this module only reads the farm's stored forecast,
configured availability and existing bookings. It writes nothing: an alternative is a suggestion,
never a new job, booking or operation. Missing forecast, availability or capacity stays
``unknown``; the module never invents soil, dose, cost or probability advice.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on

from farmcore.apps.advisor import service as advice_service
from farmcore.apps.advisor.engine import AdvisorConfig
from farmcore.apps.forecast import availability as farm_availability
from farmcore.apps.forecast import store
from farmcore.apps.planning import engine, resources
from farmcore.apps.planning.models import (
    PlanningJob,
    PlanningJobAssignment,
    PlanningJobParcel,
    PlanningMachine,
    PlanningOperator,
)
from farmcore.core.access import ScopeDenied, ScopedRepository
from farmcore.core.models.canonical import Advice, Farm, Parcel, Product

SCOPE = "planning:read"
HORIZON_HOURS = 72


@dataclass(frozen=True, slots=True)
class Forecast:
    """What every job on one page may read: one run, one configuration, one reason."""

    run: store.StoredRun | None
    config: AdvisorConfig
    reason: str | None


def forecast_context(repo: ScopedRepository, now: datetime, tz: str) -> Forecast:
    farm = repo.get(Farm, repo.farm_id, scope=SCOPE)
    config = advice_service.config_for(farm, now, tz)
    try:
        from farmcore.apps.review.service import resolve

        config = resolve(repo, now, tz).config
    except ScopeDenied:
        pass
    try:
        run = store.latest_run(repo, now)
    except ScopeDenied:
        return Forecast(None, config, "Forecast access is not authorized for this grant.")
    return Forecast(run, config, None if run else "No forecast is available for this farm.")


def _windows(repo: ScopedRepository, kind: str, resource_id: int) -> list[engine.Window]:
    return [
        engine.Window(row.starts_at, row.ends_at, row.available)
        for row in resources.windows_for(repo, kind, resource_id)
    ]


def _bookings(
    repo: ScopedRepository, kind: str, resource_id: int, job_id: int
) -> list[engine.Span]:
    column = (
        PlanningJobAssignment.machine_id if kind == "machine" else PlanningJobAssignment.operator_id
    )
    return [
        engine.Span(row.starts_at, row.ends_at)
        for row in repo.list(
            PlanningJobAssignment,
            column == resource_id,
            PlanningJobAssignment.job_id != job_id,
            scope=SCOPE,
        )
    ]


def _assigned(assigned: list[dict[str, object]], kind: str) -> dict[str, object] | None:
    return next((row for row in assigned if row["kind"] == kind), None)


def _area(parcels: list[Parcel]) -> Decimal:
    return sum((parcel.area_ha for parcel in parcels), Decimal("0"))


def _sulphur(product) -> bool:
    targets = getattr(product, "targets", None) or []
    return any("powdery" in str(target) for target in targets)


def _state_json(state: engine.State | None) -> dict[str, str] | None:
    return None if state is None else {"status": state.status, "reason": state.reason}


def _weather(forecast: Forecast, span: engine.Span, *, sulphur: bool) -> engine.State:
    if forecast.run is None:
        return engine.State("unknown", forecast.reason or "No forecast is available.")
    return engine.weather_state(forecast.run.series, span, forecast.config, sulphur=sulphur)


def _capacity_json(capacity: engine.CapacityState | None) -> dict[str, object] | None:
    if capacity is None:
        return None
    return {
        "status": capacity.status,
        "reason": capacity.reason,
        "required_minutes": capacity.required_minutes,
    }


def _busy_days(repo: ScopedRepository, now: datetime, tz: str) -> set[date]:
    try:
        return farm_availability.busy_days(repo, now.astimezone(ZoneInfo(tz)).date(), member=True)
    except ScopeDenied:
        return set()


def _time_offer(
    repo: ScopedRepository,
    job: PlanningJob,
    now: datetime,
    tz: str,
    forecast: Forecast,
    machine_row: dict[str, object] | None,
    operator_row: dict[str, object] | None,
    capacity: engine.CapacityState,
    area: Decimal,
    *,
    sulphur: bool,
    busy_days: set[date],
) -> dict[str, object] | None:
    if forecast.run is None:
        return None
    if machine_row is None or operator_row is None or capacity.status != "fits":
        return None
    machine_windows = _windows(repo, "machine", int(machine_row["id"]))
    operator_windows = _windows(repo, "operator", int(operator_row["id"]))
    if not machine_windows or not operator_windows:
        return None
    span = engine.next_feasible_span(
        series=forecast.run.series,
        now=now,
        tz=tz,
        config=forecast.config,
        earliest=now,
        duration_minutes=job.duration_minutes,
        horizon_hours=HORIZON_HOURS,
        sulphur=sulphur,
        machine_windows=machine_windows,
        operator_windows=operator_windows,
        machine_bookings=_bookings(repo, "machine", int(machine_row["id"]), job.id),
        operator_bookings=_bookings(repo, "operator", int(operator_row["id"]), job.id),
        work_rate=machine_row.get("work_rate_ha_per_hour"),
        area_ha=area,
        busy_days=busy_days,
    )
    if span is None:
        return None
    return {
        "start": span.start,
        "end": span.end,
        "reason": "Earliest window where the forecast, configured availability and capacity agree.",
    }


def _machine_offers(
    repo: ScopedRepository,
    job: PlanningJob,
    tz: str,
    span: engine.Span,
    area: Decimal,
    current_id: int | None,
    *,
    weather: engine.State,
    operator_state: engine.State | None,
) -> list[dict[str, object]]:
    offers: list[dict[str, object]] = []
    for machine in resources.machines(repo):
        if not machine.active or machine.id == current_id:
            continue
        state = engine.availability(_windows(repo, "machine", machine.id), span, tz=tz)
        capacity = engine.capacity_state(
            machine.work_rate_ha_per_hour,
            area_ha=area,
            duration_minutes=job.duration_minutes,
        )
        booked = any(
            booking.overlaps(span) for booking in _bookings(repo, "machine", machine.id, job.id)
        )
        operator_known = operator_state is not None and operator_state.status != "unknown"
        if booked:
            status = "unavailable"
            reason = "This machine is already booked for an overlapping job."
        elif state.status == "busy":
            status = "unavailable"
            reason = state.reason
        elif weather.status == "closed":
            status = "unavailable"
            reason = weather.reason
        elif operator_state is not None and operator_state.status == "busy":
            status = "unavailable"
            reason = f"Operator: {operator_state.reason}"
        elif (
            capacity.status == "fits"
            and state.status == "available"
            and weather.status == "open"
            and operator_known
        ):
            status = "available"
            reason = "Weather, operator, configured availability and known capacity fit."
        elif capacity.status == "unknown":
            status = "unknown"
            reason = capacity.reason
        elif state.status == "unknown":
            status = "unknown"
            reason = state.reason
        elif weather.status == "unknown":
            status = "unknown"
            reason = f"Weather: {weather.reason}"
        elif operator_state is None or operator_state.status == "unknown":
            status = "unknown"
            reason = "No operator is booked; operator availability is unverified."
        else:
            status = "unavailable"
            reason = capacity.reason
        offers.append(
            {
                "id": machine.id,
                "name": machine.name,
                "work_rate_ha_per_hour": (
                    float(machine.work_rate_ha_per_hour) if machine.work_rate_ha_per_hour else None
                ),
                "status": status,
                "reason": reason,
            }
        )
    offers.sort(key=lambda offer: (offer["status"] != "available", str(offer["name"])))
    return offers


def _assigned_for(repo: ScopedRepository, job_id: int) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for assignment in repo.list(
        PlanningJobAssignment, PlanningJobAssignment.job_id == job_id, scope=SCOPE
    ):
        if assignment.machine_id is not None:
            kind, resource_id = "machine", assignment.machine_id
            resource = repo.get(PlanningMachine, resource_id, scope=SCOPE)
        else:
            kind, resource_id = "operator", int(assignment.operator_id or 0)
            resource = repo.get(PlanningOperator, resource_id, scope=SCOPE)
        rows.append(
            {
                "kind": kind,
                "id": resource_id,
                "name": resource.name if resource else "Removed resource",
                "work_rate_ha_per_hour": (
                    float(resource.work_rate_ha_per_hour)
                    if kind == "machine" and resource and resource.work_rate_ha_per_hour
                    else None
                ),
            }
        )
    return rows


def _job_parcels(repo: ScopedRepository, job_id: int) -> list[Parcel]:
    links = repo.list(PlanningJobParcel, PlanningJobParcel.job_id == job_id, scope=SCOPE)
    ids = [link.parcel_id for link in links]
    if not ids:
        return []
    return repo.list(Parcel, Parcel.id.in_(ids), scope="fields:read")


def _parcel_offers(
    repo: ScopedRepository, job: PlanningJob, now: datetime, span: engine.Span
) -> list[dict[str, object]]:
    """Other parcels with current advice, marked eligible for this job's window or not.

    Only a stored ``spray`` decision whose own dated advice window overlaps the proposed job
    window is an eligible alternative; a ``wait`` decision or a window elsewhere is shown as
    not eligible with its reason.
    """
    try:
        parcels = repo.list(Parcel, scope="fields:read", order_by=Parcel.id)
        rows = repo.execute(
            select(
                Advice.parcel_id,
                Advice.decision,
                Advice.props,
                Advice.next_window_start,
                Advice.next_window_end,
            )
            .where(Advice.farm_id == repo.farm_id, Advice.evaluated_at <= now)
            .ext(distinct_on(Advice.parcel_id))
            .order_by(Advice.parcel_id, Advice.evaluated_at.desc(), Advice.id.desc()),
            scope="advice:read",
            resource="advice",
        ).all()
        current = {
            link.parcel_id
            for link in repo.list(
                PlanningJobParcel, PlanningJobParcel.job_id == job.id, scope=SCOPE
            )
        }
    except ScopeDenied:
        return []
    latest = {
        parcel_id: (decision, props or {}, start, end)
        for parcel_id, decision, props, start, end in rows
    }
    offers: list[dict[str, object]] = []
    for parcel in parcels:
        if parcel.id in current:
            continue
        decision, props, start, end = latest.get(parcel.id, (None, {}, None, None))
        if decision not in ("spray", "wait"):
            continue
        if props.get("assessment_status") in ("limited", "unavailable"):
            status = "not_eligible"
            reason = "Its assessment is limited or unavailable."
        elif decision == "wait":
            status = "not_eligible"
            reason = "Current advice says wait for this parcel."
        elif start is None or end is None:
            status = "not_eligible"
            reason = "Current advice has no dated spray window."
        elif not (start < span.end and span.start < end):
            status = "not_eligible"
            reason = "Its advice window does not overlap this job's window."
        else:
            status = "eligible"
            reason = "Current advice says spray and its window overlaps this job."
        offers.append(
            {
                "id": parcel.id,
                "alias": parcel.alias,
                "name": parcel.name,
                "decision": decision,
                "status": status,
                "reason": reason,
            }
        )
    offers.sort(key=lambda offer: (offer["status"] != "eligible", str(offer["alias"])))
    return offers[:5]


def _states(
    repo: ScopedRepository,
    job: PlanningJob,
    *,
    now: datetime,
    tz: str,
    assigned: list[dict[str, object]],
    parcels: list[Parcel],
    product,
    forecast: Forecast,
    busy_days: set[date],
) -> tuple[
    engine.Assessment,
    engine.Span,
    Decimal,
    dict[str, object] | None,
    engine.CapacityState,
]:
    span = engine.Span(
        job.scheduled_start, job.scheduled_start + timedelta(minutes=job.duration_minutes)
    )
    area = _area(parcels)
    machine_row = _assigned(assigned, "machine")
    operator_row = _assigned(assigned, "operator")
    weather = _weather(forecast, span, sulphur=_sulphur(product))
    if machine_row is None:
        machine_state = engine.State(
            "unknown", "no machine is booked; availability and capacity are unknown."
        )
        capacity = engine.CapacityState(
            "unknown", "machine capacity is unknown until a machine is booked."
        )
    else:
        machine_state = engine.availability(
            _windows(repo, "machine", int(machine_row["id"])), span, tz=tz
        )
        capacity = engine.capacity_state(
            machine_row.get("work_rate_ha_per_hour"),
            area_ha=area,
            duration_minutes=job.duration_minutes,
        )
    if operator_row is None:
        operator_state = engine.State("unknown", "no operator is booked; availability is unknown.")
    else:
        operator_state = engine.availability(
            _windows(repo, "operator", int(operator_row["id"])), span, tz=tz
        )
    result = engine.assess(weather, machine_state, operator_state, capacity)
    if job.scheduled_start.astimezone(ZoneInfo(tz)).date() in busy_days:
        result = engine.Assessment(
            "infeasible",
            (*result.reasons, "Farmer availability: this day is marked Busy."),
            result.weather,
            result.machine,
            result.operator,
            result.capacity,
        )
    return result, span, area, machine_row, capacity


def _job_offers(
    repo: ScopedRepository,
    job: PlanningJob,
    now: datetime,
    tz: str,
    forecast: Forecast,
    target: engine.Span,
    busy_days: set[date],
) -> list[dict[str, object]]:
    """Other planned jobs that can run in the same window right now."""
    offers: list[dict[str, object]] = []
    try:
        jobs = repo.list(PlanningJob, PlanningJob.status == "planned", scope=SCOPE)
    except ScopeDenied:
        return []
    for other in jobs:
        if other.id == job.id:
            continue
        other_end = other.scheduled_start + timedelta(minutes=other.duration_minutes)
        if not (target.start < other_end and other.scheduled_start < target.end):
            continue
        product = repo.get(Product, other.product_id, scope="logs:read")
        result, _, _, _, _ = _states(
            repo,
            other,
            now=now,
            tz=tz,
            assigned=_assigned_for(repo, other.id),
            parcels=_job_parcels(repo, other.id),
            product=product,
            forecast=forecast,
            busy_days=busy_days,
        )
        if result.status != "feasible":
            continue
        offers.append(
            {
                "id": other.id,
                "name": other.name,
                "start": other.scheduled_start,
                "reason": "Another planned job is feasible in this window.",
            }
        )
        if len(offers) >= 3:
            break
    return offers


def job_assessment(
    repo: ScopedRepository,
    job: PlanningJob,
    *,
    now: datetime,
    tz: str,
    assigned: list[dict[str, object]],
    parcels: list[Parcel],
    product,
    forecast: Forecast | None = None,
) -> dict[str, object]:
    forecast = forecast or forecast_context(repo, now, tz)
    busy_days = _busy_days(repo, now, tz)
    result, span, area, machine_row, capacity = _states(
        repo,
        job,
        now=now,
        tz=tz,
        assigned=assigned,
        parcels=parcels,
        product=product,
        forecast=forecast,
        busy_days=busy_days,
    )
    operator_row = _assigned(assigned, "operator")
    offers = {
        "time": None,
        "jobs": [],
        "machines": _machine_offers(
            repo,
            job,
            tz,
            span,
            area,
            int(machine_row["id"]) if machine_row else None,
            weather=result.weather,
            operator_state=result.operator,
        ),
        "parcels": _parcel_offers(repo, job, now, span),
    }
    if result.status != "feasible":
        offers["jobs"] = _job_offers(repo, job, now, tz, forecast, span, busy_days)
        offers["time"] = _time_offer(
            repo,
            job,
            now,
            tz,
            forecast,
            machine_row,
            operator_row,
            capacity,
            area,
            sulphur=_sulphur(product),
            busy_days=busy_days,
        )
    return {
        "status": result.status,
        "reasons": list(result.reasons),
        "weather": _state_json(result.weather),
        "machine": _state_json(result.machine),
        "operator": _state_json(result.operator),
        "capacity": _capacity_json(result.capacity),
        "offers": offers,
    }

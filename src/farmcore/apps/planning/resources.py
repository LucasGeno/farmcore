"""Configured machines, operators and their availability windows.

Every read goes through `ScopedRepository` (`planning:read`) so a grant without planning scope
cannot see the farm's resources. Writes require `planning:write`. Capacity is optional: NULL
means unknown and is never replaced with an assumed value.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from farmcore.apps.planning.models import (
    MACHINE_KINDS,
    PlanningMachine,
    PlanningOperator,
    PlanningResourceWindow,
)
from farmcore.core.access import ScopedRepository

SCOPE = "planning:read"
WRITE = "planning:write"
RESOURCE_KINDS = ("machine", "operator")
MAX_NOTES = 2000


class ResourceError(ValueError):
    """Invalid resource input; the form route answers with a plain error."""


def _text(value: object, label: str, *, maximum: int = 120) -> str:
    text = str(value or "").strip()
    if not text:
        raise ResourceError(f"{label} is required.")
    if len(text) > maximum:
        raise ResourceError(f"{label} is too long.")
    return text


def _notes(value: object) -> str:
    text = str(value or "")
    if len(text) > MAX_NOTES:
        raise ResourceError("Notes are too long.")
    return text


def _capacity(value: object, label: str, *, maximum: Decimal) -> Decimal | None:
    text = str(value or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise ResourceError(f"{label} must be a number.") from None
    if not number.is_finite() or number <= 0:
        raise ResourceError(f"{label} must be greater than zero when provided.")
    if number > maximum:
        raise ResourceError(f"{label} is above the value this installation can store.")
    return number


def _name_taken(repo: ScopedRepository, model, name: str) -> bool:
    return (
        repo.session.scalar(
            select(model.id).where(model.farm_id == repo.farm_id, model.name == name).limit(1)
        )
        is not None
    )


def create_machine(
    repo: ScopedRepository,
    now: datetime,
    *,
    name: str,
    kind: str,
    work_rate_ha_per_hour: object = None,
    tank_litres: object = None,
    notes: str = "",
) -> PlanningMachine:
    repo.check(WRITE, "planning_machine")
    kind = str(kind or "").strip()
    if kind not in MACHINE_KINDS:
        raise ResourceError(f"Choose a machine kind from {', '.join(MACHINE_KINDS)}.")
    machine_name = _text(name, "Machine name")
    if _name_taken(repo, PlanningMachine, machine_name):
        raise ResourceError("A machine with this name already exists on this farm.")
    row = PlanningMachine(
        farm_id=repo.farm_id,
        name=machine_name,
        kind=kind,
        work_rate_ha_per_hour=_capacity(
            work_rate_ha_per_hour, "Work rate", maximum=Decimal("99999.999")
        ),
        tank_litres=_capacity(tank_litres, "Tank volume", maximum=Decimal("99999999.99")),
        active=True,
        notes=_notes(notes),
        created_by=repo.actor,
        created_at=now,
        updated_by=repo.actor,
        updated_at=now,
    )
    try:
        with repo.session.begin_nested():
            repo.session.add(row)
    except IntegrityError:
        raise ResourceError("A machine with this name already exists on this farm.") from None
    return row


def create_operator(
    repo: ScopedRepository, now: datetime, *, name: str, notes: str = ""
) -> PlanningOperator:
    repo.check(WRITE, "planning_operator")
    operator_name = _text(name, "Operator name")
    if _name_taken(repo, PlanningOperator, operator_name):
        raise ResourceError("An operator with this name already exists on this farm.")
    row = PlanningOperator(
        farm_id=repo.farm_id,
        name=operator_name,
        active=True,
        notes=_notes(notes),
        created_by=repo.actor,
        created_at=now,
        updated_by=repo.actor,
        updated_at=now,
    )
    try:
        with repo.session.begin_nested():
            repo.session.add(row)
    except IntegrityError:
        raise ResourceError("An operator with this name already exists on this farm.") from None
    return row


def parse_resource(value: object) -> tuple[str, int]:
    """Parse the form's ``machine:12`` / ``operator:5`` value."""
    text = str(value or "").strip()
    kind, _, raw = text.partition(":")
    if kind not in RESOURCE_KINDS or not raw.isdigit() or not 0 < int(raw) < 2**63:
        raise ResourceError("Choose a configured machine or operator.")
    return kind, int(raw)


def _resource_row(repo: ScopedRepository, kind: str, resource_id: int):
    model = PlanningMachine if kind == "machine" else PlanningOperator
    row = repo.get(model, resource_id, scope=SCOPE)
    if row is None:
        raise ResourceError("That machine or operator is not configured on this farm.")
    if not row.active:
        raise ResourceError("That machine or operator is inactive.")
    return row


def create_window(
    repo: ScopedRepository,
    now: datetime,
    *,
    kind: str,
    resource_id: int,
    starts_at: datetime,
    ends_at: datetime,
    available: bool,
    notes: str = "",
) -> PlanningResourceWindow:
    repo.check(WRITE, "planning_resource_window")
    if kind not in RESOURCE_KINDS:
        raise ResourceError("Choose a machine or an operator.")
    _resource_row(repo, kind, resource_id)
    if starts_at.tzinfo is None or ends_at.tzinfo is None:
        raise ResourceError("Choose availability times with a timezone.")
    if starts_at >= ends_at:
        raise ResourceError("Availability must end after it starts.")
    row = PlanningResourceWindow(
        farm_id=repo.farm_id,
        machine_id=resource_id if kind == "machine" else None,
        operator_id=resource_id if kind == "operator" else None,
        starts_at=starts_at,
        ends_at=ends_at,
        available=available,
        notes=_notes(notes),
        created_by=repo.actor,
        created_at=now,
        updated_by=repo.actor,
        updated_at=now,
    )
    repo.session.add(row)
    repo.session.flush()
    return row


def set_active(
    repo: ScopedRepository, now: datetime, *, kind: str, resource_id: int, active: bool
) -> None:
    if kind not in RESOURCE_KINDS:
        raise ResourceError("Choose a machine or an operator.")
    resource = "planning_machine" if kind == "machine" else "planning_operator"
    repo.check(WRITE, resource)
    row = _resource_row(repo, kind, resource_id)
    row.active = active
    row.updated_by = repo.actor
    row.updated_at = now
    repo.session.flush()


def machines(repo: ScopedRepository) -> list[PlanningMachine]:
    return repo.list(
        PlanningMachine, scope=SCOPE, order_by=(PlanningMachine.name, PlanningMachine.id)
    )


def operators(repo: ScopedRepository) -> list[PlanningOperator]:
    return repo.list(
        PlanningOperator, scope=SCOPE, order_by=(PlanningOperator.name, PlanningOperator.id)
    )


def windows(repo: ScopedRepository) -> list[PlanningResourceWindow]:
    return repo.list(
        PlanningResourceWindow,
        scope=SCOPE,
        order_by=(PlanningResourceWindow.starts_at, PlanningResourceWindow.id),
    )


def windows_for(
    repo: ScopedRepository, kind: str, resource_id: int
) -> list[PlanningResourceWindow]:
    column = (
        PlanningResourceWindow.machine_id
        if kind == "machine"
        else PlanningResourceWindow.operator_id
    )
    return repo.list(
        PlanningResourceWindow,
        column == resource_id,
        scope=SCOPE,
        order_by=(PlanningResourceWindow.starts_at, PlanningResourceWindow.id),
    )


def context(repo: ScopedRepository) -> dict[str, object]:
    configured_machines = machines(repo)
    configured_operators = operators(repo)
    labels = {
        **{f"machine:{row.id}": row.name for row in configured_machines},
        **{f"operator:{row.id}": row.name for row in configured_operators},
    }
    window_rows = [
        {
            "id": row.id,
            "resource": (
                f"machine:{row.machine_id}"
                if row.machine_id is not None
                else f"operator:{row.operator_id}"
            ),
            "label": labels.get(
                f"machine:{row.machine_id}"
                if row.machine_id is not None
                else f"operator:{row.operator_id}",
                "Removed resource",
            ),
            "starts_at": row.starts_at,
            "ends_at": row.ends_at,
            "available": row.available,
            "notes": row.notes,
        }
        for row in windows(repo)
    ]
    return {
        "machines": configured_machines,
        "operators": configured_operators,
        "resource_windows": window_rows,
    }

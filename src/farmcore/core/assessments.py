"""The assessment evidence envelope: one shape for every module's computed result.

An envelope (`assessment` table) holds what makes a result reproducible and comparable across
modules: farm and optional parcel, evaluation time, availability status (`evaluable`, `limited`,
`unavailable`), the configuration version (or, without a version, the configuration itself) and
the inputs read. The result is typed by its kind (`Manifest.assessment_kinds`): the kind's own
Pydantic model validates it on the way in and types it on the way out, so a module never has to
map its result onto another module's vocabulary (no frost result becomes a spray decision), and
the envelope never relaxes a kind's validation to fit another.

Rows are append-only: the database refuses any UPDATE (core_0012), so a saved assessment, and a
human review of it, always refer to the evidence that was actually evaluated.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from farmcore.core.manifest import AssessmentKind, import_string
from farmcore.core.models.assessment import ASSESSMENT_STATUSES, Assessment


class AssessmentInvalid(ValueError):
    """A result that its kind does not accept, or an envelope read as another kind."""


def _model(kind: AssessmentKind) -> type[BaseModel]:
    model = import_string(kind.result)
    if not (isinstance(model, type) and issubclass(model, BaseModel)):
        raise TypeError(f"assessment kind {kind.key!r}: {kind.result!r} is not a Pydantic model")
    return model


def envelope(
    kind: AssessmentKind,
    *,
    farm_id: int,
    parcel_id: int | None,
    evaluated_at: datetime,
    status: str,
    config_version: dict[str, Any],
    input_snapshot: dict[str, Any],
    result: Any,
    effective_config: dict[str, Any] | None = None,
) -> Assessment:
    """A new, unsaved envelope whose result `kind` validated; `AssessmentInvalid` otherwise."""
    if status not in ASSESSMENT_STATUSES:
        raise AssessmentInvalid(f"unknown assessment status {status!r}")
    try:
        typed = _model(kind).model_validate(result)
    except ValidationError as exc:
        raise AssessmentInvalid(f"{kind.key}: {exc}") from exc
    return Assessment(
        farm_id=farm_id,
        parcel_id=parcel_id,
        kind=kind.key,
        kind_version=kind.version,
        evaluated_at=evaluated_at,
        status=status,
        config_version=config_version,
        effective_config=effective_config,
        input_snapshot=input_snapshot,
        result=typed.model_dump(mode="json"),
    )


def result_of(row: Assessment, kind: AssessmentKind) -> BaseModel:
    """The saved result typed by `kind`; refused unless the row is of that kind and version."""
    if (row.kind, row.kind_version) != (kind.key, kind.version):
        raise AssessmentInvalid(
            f"assessment {row.id} is {row.kind} v{row.kind_version}, not {kind.key} v{kind.version}"
        )
    try:
        return _model(kind).model_validate(row.result)
    except ValidationError as exc:
        raise AssessmentInvalid(f"{kind.key}: {exc}") from exc

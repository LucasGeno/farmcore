"""Validated app data of confirmed field observations and irrigation facts.

An irrigation is a depth (mm) or an entered volume (m3, L), not a product rate: the canonical
`quantity` rows stay empty for `irrigate` and `observation` logs. An irrigation's amount and unit
have one reader, its one entered `typed_quantity` (core_0017 backfilled the irrigations confirmed
before capture wrote one); `log.props["irrigation"]` keeps the same fact with the gross/net basis,
which only props carry. `record_irrigation` is the one writer of both copies, for capture and any
correction. These models are the writer's shape and the reader's guard: a record whose facts do
not validate is not shown as a fact, never guessed.

No efficiency is stored or derived: `basis` is only the gross/net word the farmer said, and an
absent basis stays absent. No irrigation advice and no spray stock follow from these records.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session, object_session

from farmcore.core.models.canonical import Log
from farmcore.core.models.catalog import TypedQuantity, enter_quantity

IRRIGATION = "irrigation"
OBSERVATION = "observation"
UNITS = ("mm", "m3", "L")
BASES = ("gross", "net")
MAX_TEXT = 2000
LABELS = {"irrigate": "Irrigation", "observation": "Observation"}


class IrrigationFact(BaseModel):
    """The entered amount, its unit and the gross/net basis when the farmer said one."""

    model_config = ConfigDict(extra="forbid")

    amount: str = Field(min_length=1, max_length=64)
    unit: Literal["mm", "m3", "L"]
    basis: Literal["gross", "net"] | None = None

    @field_validator("amount")
    @classmethod
    def positive(cls, value: str) -> str:
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("Irrigation amount must be a number.") from exc
        if not number.is_finite() or number <= 0:
            raise ValueError("Irrigation amount must be positive.")
        return value


class ObservationFact(BaseModel):
    """What was seen, in the farmer's own words."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_TEXT)


def amount_text(value: float | Decimal | str) -> str:
    """Exact fixed notation with trailing zeros trimmed (`20`, `12.5`); no rounding."""
    text = format(Decimal(str(value)), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def irrigation(log: Log) -> IrrigationFact | None:
    """The irrigation's amount and unit from its one entered quantity, with the basis from props;
    None when there is no single entered quantity or the fact does not validate.

    `log` must be attached to a session: the entered quantity is read through it, one query per
    log, keyed on a log the caller already read under its scope. A detached log reads as None
    (details unavailable)."""
    session = object_session(log)
    entered = (
        session.scalars(
            select(TypedQuantity)
            .where(TypedQuantity.log_id == log.id, TypedQuantity.entered_measure.is_(None))
            .limit(2)
        ).all()
        if session is not None
        else []
    )
    if len(entered) != 1:
        return None
    raw = (log.props or {}).get(IRRIGATION)
    basis = raw.get("basis") if isinstance(raw, dict) else None
    try:
        return IrrigationFact(
            amount=format(entered[0].entered_value, "f"), unit=entered[0].entered_unit, basis=basis
        )
    except ValueError:
        return None


def record_irrigation(session: Session, log: Log, fact: IrrigationFact) -> None:
    """Write an irrigation's amount to both copies, its entered quantity and `props`, replacing
    what it had: inside a savepoint, so a failure leaves neither changed."""
    with session.begin_nested():
        session.execute(
            delete(TypedQuantity).where(
                TypedQuantity.log_id == log.id, TypedQuantity.entered_measure.is_(None)
            )
        )
        enter_quantity(session, log_id=log.id, value=Decimal(fact.amount), unit=fact.unit)
        log.props = {**(log.props or {}), IRRIGATION: fact.model_dump(exclude_none=True)}


def observation(props: dict[str, Any] | None) -> ObservationFact | None:
    raw = (props or {}).get(OBSERVATION)
    if not isinstance(raw, dict):
        return None
    try:
        return ObservationFact.model_validate(raw)
    except ValueError:
        return None


def label(log: Log) -> str | None:
    """`Irrigation` / `Observation` for a default log name; None leaves other types alone."""
    return LABELS.get(log.type)


def summary(log: Log) -> str | None:
    """One line of the confirmed fact for record lists, or None when the type carries none."""
    if log.type == "irrigate":
        fact = irrigation(log)
        if fact is None:
            return "Irrigation (details unavailable)"
        basis = f" {fact.basis}" if fact.basis else " (basis not stated)"
        return f"Irrigation {fact.amount} {fact.unit}{basis}"
    if log.type == "observation":
        fact = observation(log.props)
        if fact is None:
            return "Observation (details unavailable)"
        text = fact.text if len(fact.text) <= 160 else fact.text[:159] + "…"
        return f"Observation: {text}"
    return None

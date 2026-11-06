"""Validated app data of confirmed field observations and irrigation facts (`log.props`).

An irrigation is a depth (mm) or an entered volume (m3, L), not a product rate: the canonical
`quantity` rows stay empty for `irrigate` and `observation` logs, and the fact lives in the log's
`props` (the canonical model's slot for app data). These models are the only writer's shape and
the reader's guard: a row whose props do not validate is not shown as a fact, never guessed.

No efficiency is stored or derived: `basis` is only the gross/net word the farmer said, and an
absent basis stays absent. No irrigation advice and no spray stock follow from these records.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from farmcore.core.models.canonical import Log

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


def irrigation(props: dict[str, Any] | None) -> IrrigationFact | None:
    raw = (props or {}).get(IRRIGATION)
    if not isinstance(raw, dict):
        return None
    try:
        return IrrigationFact.model_validate(raw)
    except ValueError:
        return None


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
        fact = irrigation(log.props)
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

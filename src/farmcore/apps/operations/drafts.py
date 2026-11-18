"""Draft kinds of the Records app: spray, irrigation and observation (`core/drafts.py`).

The validator checks a proposed record against the farm (its parcels, known products, an aware
date); the confirm service writes it through the canonical log pipeline (`core/logs.py`): a
pending log created and confirmed in the same transaction, so the spray merge rule and the
record-type policies apply exactly as for a chat confirmation, and no pending log outlives the
call. The draft's provenance, its id and both actors stay on the record's `log_source`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.apps.operations import facts
from farmcore.apps.operations.corrections import Dose
from farmcore.core import logs
from farmcore.core.logs import EnteredQuantity, QuantityInput, SourceInput
from farmcore.core.models.canonical import Parcel, Product
from farmcore.core.models.drafts import Draft

SOURCE_APP = "operations"
RecordId = Annotated[int, Field(gt=0, lt=2**63)]
RANKS = {"voice": logs.RANK_VOICE, "file": logs.RANK_FILE, "isoxml": logs.RANK_MACHINE}
LOG_TYPES = {"spray": "spray", "irrigation": "irrigate", "observation": "observation"}


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parcel_ids: list[RecordId] = Field(min_length=1, max_length=100)
    ts_start: datetime
    ts_end: datetime | None = None

    @field_validator("ts_start", "ts_end")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Supply a timezone.")
        return value


class SprayDraft(_Record):
    doses: list[Dose] = Field(min_length=1, max_length=50)


class IrrigationDraft(_Record):
    fact: facts.IrrigationFact


class ObservationDraft(_Record):
    fact: facts.ObservationFact


MODELS: dict[str, type[_Record]] = {
    "spray": SprayDraft,
    "irrigation": IrrigationDraft,
    "observation": ObservationDraft,
}


def _checked(session: Session, farm_id: int, kind: str, payload: dict) -> _Record:
    record = MODELS[kind].model_validate(payload)
    ids = set(record.parcel_ids)
    found = set(
        session.scalars(select(Parcel.id).where(Parcel.id.in_(ids), Parcel.farm_id == farm_id))
    )
    if found != ids:
        raise ValueError("Some parcels are not on this farm.")
    if isinstance(record, SprayDraft):
        products = {d.product_id for d in record.doses}
        known = set(session.scalars(select(Product.id).where(Product.id.in_(products))))
        if known != products:
            raise ValueError("Some products are unknown.")
    return record


def _validator(kind: str):
    def validate(session: Session, farm_id: int, payload: dict) -> dict:
        return _checked(session, farm_id, kind, payload).model_dump(mode="json")

    validate.__name__ = f"validate_{kind}"
    return validate


validate_spray = _validator("spray")
validate_irrigation = _validator("irrigation")
validate_observation = _validator("observation")


def _fields(record: _Record):
    """(quantities, props) the log takes for this record."""
    if isinstance(record, SprayDraft):
        quantities = [
            QuantityInput("rate", Decimal(d.value), d.unit, d.product_id, d.label)
            for d in record.doses
        ]
        return quantities, None
    if isinstance(record, IrrigationDraft):
        amount = EnteredQuantity(Decimal(record.fact.amount), record.fact.unit)
        return [amount], {facts.IRRIGATION: record.fact.model_dump(exclude_none=True)}
    return [], {facts.OBSERVATION: record.fact.model_dump()}


def confirm(session: Session, draft: Draft, *, actor: str, now: datetime) -> tuple[str, int]:
    """Write the draft as a done log (merged into a matching spray); returns ("log", id)."""
    record = _checked(session, draft.farm_id, draft.kind, draft.payload)
    if record.ts_start > now:
        raise ValueError("This record is dated after the current time; nothing was recorded.")
    provenance = dict(draft.provenance)
    source_type = provenance.get("source_type", "file")
    quantities, props = _fields(record)
    log, held = logs.create_pending_once(
        session,
        farm_id=draft.farm_id,
        type=LOG_TYPES[draft.kind],
        parcel_ids=record.parcel_ids,
        ts_start=record.ts_start,
        ts_end=record.ts_end,
        quantities=quantities,
        source=SourceInput(
            source_type=source_type,
            external_key=f"draft:{draft.id}",
            raw_payload={
                **provenance,
                "draft_id": draft.id,
                "draft_kind": draft.kind,
                "draft_revision": draft.revision,
                "proposed_by": draft.actor,
                "confirmed_by": actor,
            },
            confidence=None,
            precision_rank=RANKS[source_type],
        ),
        actor=actor,
        occurred_at=now,
        source_app=SOURCE_APP,
    )
    if held:  # the draft's source is already on a record: never write it twice
        return "log", log.id
    if props is not None:
        log.props = props
    log, _merged = logs.confirm(session, log.id, actor, now, source_app=SOURCE_APP)
    return "log", log.id

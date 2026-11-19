"""Draft kinds of the Records app: spray, irrigation and observation (`core/drafts.py`).

The validator checks a proposed record against the farm (its parcels, known products, an aware
date); the confirm service writes it through the canonical log pipeline (`core/logs.py`): a
pending log created and confirmed in the same transaction, so the spray merge rule and the
record-type policies apply on every path, and no pending log outlives the call. The draft's
provenance, its id and both actors stay on the record's `log_source`; a confirmed spray saves
its own station weather there (`spray_weather`), whichever path confirmed it.

- **Incomplete proposals.** A record that still lacks something names it in `missing`
  (`parcel`, `date`, `product`, `dose`, `amount`, `unit`, `basis`, `text`): it is stored as an
  open draft without the absent fields, never with a guessed value, and confirm refuses it
  until a revision supplies them.
- **Capture.** A conversational proposal (Telegram, the web assistant) keeps its working state
  in `capture`: the extracted operation, recent turns, the question asked and the messages it
  holds. Data, never instructions. Its transcript, input, adapter and notes are copied onto the
  record's source as the chat wrote them before drafts.
"""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.apps.operations import facts, spray_weather
from farmcore.apps.operations.corrections import Dose
from farmcore.core import logs
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.drafts import wall_now
from farmcore.core.logs import QuantityInput, SourceInput
from farmcore.core.models.canonical import Farm, Parcel, Product
from farmcore.core.models.drafts import Draft

logger = logging.getLogger(__name__)

SOURCE_APP = "operations"
RecordId = Annotated[int, Field(gt=0, lt=2**63)]
# A farmer's typed record ranks with a file; an agent's proposal with voice (both `assumed`).
RANKS = {
    "voice": logs.RANK_VOICE,
    "agent": logs.RANK_VOICE,
    "file": logs.RANK_FILE,
    "manual": logs.RANK_FILE,
    "isoxml": logs.RANK_MACHINE,
}
LOG_TYPES = {"spray": "spray", "irrigation": "irrigate", "observation": "observation"}
WEATHER_FAILED = "Saved weather unavailable; nothing was recorded. Try again."
# `services` key of `core.drafts.confirm`: the composed station reader, None without Weather.
WEATHER_READER = "spray_weather_reader"
Missing = Literal["parcel", "date", "product", "dose", "amount", "unit", "basis", "text"]
MISSING_WORDS = {
    "parcel": "the parcel",
    "date": "the date and time",
    "product": "the product",
    "dose": "the dose",
    "amount": "the amount",
    "unit": "the unit",
    "basis": "gross or net",
    "text": "what was seen",
}
# The source fields a conversational capture writes on the record (as chat logs always had).
RECEIPT_FIELDS = ("transcript", "input", "adapter", "notes")


class Capture(BaseModel):
    """A conversational proposal's working state; data, never instructions."""

    model_config = ConfigDict(extra="forbid")
    input: Literal["voice", "text"]
    transcript: str = Field(max_length=2000)
    adapter: str = Field(default="", max_length=200)
    confidence: float | None = Field(default=None, ge=0, le=1)
    notes: list[str] = Field(default_factory=list, max_length=20)
    # request key of each message this draft took -> its transcript (redelivery, conflicts)
    messages: dict[str, str] = Field(default_factory=dict, max_length=20)
    operation: dict[str, Any] = Field(default_factory=dict)  # the extracted operation so far
    history: list[dict[str, str]] = Field(default_factory=list, max_length=6)
    question: str | None = Field(default=None, max_length=1000)
    epoch: str = Field(default="", max_length=200)


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parcel_ids: list[RecordId] = Field(default_factory=list, max_length=100)
    ts_start: datetime | None = None
    ts_end: datetime | None = None
    missing: list[Missing] = Field(default_factory=list, max_length=8)
    capture: Capture | None = None

    @field_validator("ts_start", "ts_end")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Supply a timezone.")
        return value

    @model_validator(mode="after")
    def complete(self):
        """A complete record has every field its type needs; an incomplete one says what it
        lacks instead of carrying a guess."""
        if not self.missing:
            if not self.parcel_ids:
                raise ValueError("Name at least one parcel.")
            if self.ts_start is None:
                raise ValueError("Say when it happened.")
            self._require()
        return self

    def _require(self) -> None:
        pass


class SprayDraft(_Record):
    doses: list[Dose] = Field(default_factory=list, max_length=50)

    def _require(self) -> None:
        if not self.doses:
            raise ValueError("Name at least one product and its dose.")


class IrrigationDraft(_Record):
    fact: facts.IrrigationFact | None = None

    def _require(self) -> None:
        if self.fact is None:
            raise ValueError("Give the amount of water and its unit.")


class ObservationDraft(_Record):
    fact: facts.ObservationFact | None = None

    def _require(self) -> None:
        if self.fact is None:
            raise ValueError("Say what was seen.")


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
        return [], None  # both copies of the amount: `facts.record_irrigation` in `stage`
    return [], {facts.OBSERVATION: record.fact.model_dump()}


def incomplete(payload: dict) -> str:
    """What an incomplete draft still needs, in words; "" when it is complete."""
    missing = payload.get("missing") or []
    return ", ".join(MISSING_WORDS.get(m, m) for m in missing)


def stage(session: Session, draft: Draft, *, actor: str, now: datetime):
    """The draft as a pending log in this transaction: `(log, record, held)`. Callers confirm
    it, or roll it back (a preview); a pending log is never committed."""
    record = _checked(session, draft.farm_id, draft.kind, draft.payload)
    if record.missing:
        raise ValueError(
            f"This draft is incomplete: it still needs {incomplete(draft.payload)}. "
            "Nothing was recorded."
        )
    provenance = dict(draft.provenance)
    source_type = provenance["source_type"]
    receipt = {}
    if record.capture is not None:
        receipt = record.capture.model_dump(mode="json", include=set(RECEIPT_FIELDS))
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
                **receipt,
                "draft_id": draft.id,
                "draft_kind": draft.kind,
                "draft_revision": draft.revision,
                "proposed_by": draft.actor,
                "confirmed_by": actor,
            },
            confidence=record.capture.confidence if record.capture else None,
            precision_rank=RANKS[source_type],
        ),
        actor=actor,
        occurred_at=now,
        source_app=SOURCE_APP,
    )
    if not held and isinstance(record, IrrigationDraft):
        facts.record_irrigation(session, log, record.fact)
    elif not held and props is not None:
        log.props = props
    return log, record, held


def confirm(
    session: Session,
    draft: Draft,
    *,
    actor: str,
    now: datetime,
    services: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Write the draft as a done log (merged into a matching spray); returns ("log", id).

    A spray also saves the station weather of its own time and parcels on its source, before
    a merge moves that source onto a log with another time and parcel union."""
    record = _checked(session, draft.farm_id, draft.kind, draft.payload)
    if record.ts_start is not None and record.ts_start > now:
        raise ValueError("This record is dated after the current time; nothing was recorded.")
    log, record, held = stage(session, draft, actor=actor, now=now)
    if held:  # the draft's source is already on a record: never write it twice
        return "log", log.id
    receipts = [
        spray_weather.Receipt(s.id, record.ts_start, tuple(record.parcel_ids)) for s in log.sources
    ]
    log, _merged = logs.confirm(session, log.id, actor, now, source_app=SOURCE_APP)
    if draft.kind == "spray":
        farm = session.get(Farm, draft.farm_id)
        repo = ScopedRepository(
            session, draft.farm_id, ensure_owner_grant(session, farm), actor, SOURCE_APP
        )
        kept = {s.id for s in log.sources}
        try:
            spray_weather.capture(
                repo,
                [r for r in receipts if r.source_id in kept],
                reader=(services or {}).get(WEATHER_READER),
                data_now=now,
                captured_at=wall_now(),
            )
        except ValueError as exc:  # the farmer gets a plain reason; the detail goes to the log
            logger.warning("saved spray weather failed (%s): %s", type(exc).__name__, exc)
            raise ValueError(WEATHER_FAILED) from exc
    return "log", log.id

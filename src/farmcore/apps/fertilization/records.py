"""Fertilization records: draft kinds `fertilization` and `fertilization-correction`.

A fertiliser application is proposed as a draft (`core/drafts.py`) and confirmed by the owner. The
confirm service writes one done `fertilise` log through `core/logs.py`: the amount exactly as
entered, as a `typed_quantity` naming the fertiliser's catalog item (its stock counts it), and
in `props` the fertiliser, its nutrient composition with where it came from, the crop season of
each parcel on that day, and the conversion factors the owner entered.

- **Nothing is inferred.** The validator, not the proposer, decides what a draft still lacks
  (`missing`): the parcel, the date, the fertiliser, the amount, the composition, a treated area
  for a per-hectare amount on a parcel whose area is unknown, a density for an amount in litres
  (a declared composition is a share by mass, so litres need kg/l), and a density when the
  amount is a mass and the farm stocks the fertiliser in litres. A draft that lacks something
  stays open, names it, and cannot be confirmed; nothing is guessed from a product name or
  defaulted. A composition always says where it comes from (the label, a lab analysis, the
  supplier's sheet or the farmer); one without that is refused.
- **Stock moves once.** Stock is never decremented: `inventory` computes it from the done
  records that name the item, so a draft (proposed, planned or open) never consumes it, a
  retried or concurrent confirm records once (the draft's farm lock and row lock), a rolled-back
  confirm leaves nothing, and a correction moves stock by exactly its delta.
- **Corrections** are drafts of kind `fertilization-correction`: the owner reviews the record
  before and after, then confirms. Confirm refuses if the record changed since the correction
  was proposed (two corrections of one record: the second is refused), replaces the record's
  facts and keeps an `operation_correction` receipt with the original values, the reason and
  both actors.

No rate recommendation, soil model or regulatory filing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from farmcore.apps.inventory.models import InventoryStock
from farmcore.apps.operations.corrections import decimal_text, fingerprint
from farmcore.core import logs, units
from farmcore.core.farm_location import farm_time_zone
from farmcore.core.logs import FACTORS, EnteredQuantity, SourceInput
from farmcore.core.models.canonical import CropSeason, Farm, Log, Parcel
from farmcore.core.models.catalog import (
    CatalogItem,
    CropProtectionItem,
    TypedQuantity,
    enter_quantity,
)
from farmcore.core.models.corrections import OperationCorrection
from farmcore.core.models.drafts import Draft
from farmcore.core.models.identity import LocalIdentity
from farmcore.core.timeline import post_event
from farmcore.settings import get_settings

SOURCE_APP = "fertilization"
LOG_TYPE = "fertilise"
KIND = "fertilization"
CORRECTION_KIND = "fertilization-correction"
PROPS_KEY = "fertilization"
RecordId = Annotated[int, Field(gt=0, lt=2**63)]
# Declared contents as EU fertilising-product labels give them, % by mass (Reg. (EU) 2019/1009,
# Annex I; oxide forms for P, K, Mg, S, Ca).
NUTRIENTS = ("N", "P2O5", "K2O", "MgO", "SO3", "CaO")
Nutrient = Literal["N", "P2O5", "K2O", "MgO", "SO3", "CaO"]
CompositionSource = Literal["label", "analysis", "supplier", "farmer"]
AMOUNT_MEASURES = frozenset({"mass", "volume", "mass_rate", "volume_rate"})
VOLUMES = frozenset({"volume", "volume_rate"})
RATES = frozenset({"mass_rate", "volume_rate"})
Missing = Literal["parcel", "date", "fertiliser", "amount", "composition", "area", "density"]
MISSING_WORDS = {
    "parcel": "the parcel",
    "date": "the date",
    "fertiliser": "the fertiliser",
    "amount": "the amount",
    "composition": "the nutrient composition and where it comes from",
    "area": "the area fertilised (a parcel's area is unknown)",
    "density": "the density in kg/l (litres on one side, kg on the other)",
}
# A farmer's typed record ranks with a file; an agent's proposal with voice (operations.drafts).
RANKS = {
    "voice": logs.RANK_VOICE,
    "agent": logs.RANK_VOICE,
    "file": logs.RANK_FILE,
    "manual": logs.RANK_FILE,
    "isoxml": logs.RANK_MACHINE,
}
Positive = Annotated[Decimal, Field(gt=0, allow_inf_nan=False, max_digits=24, decimal_places=6)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fertiliser(_Strict):
    name: str = Field(min_length=1, max_length=200)
    catalog_item_id: RecordId | None = None  # the stock item; None: no stock is counted

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Name the fertiliser.")
        return value.strip()


class Amount(_Strict):
    value: Positive
    unit: str = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def typed(self):
        try:
            measured = units.measure(self.value, self.unit)
        except units.UnitError as exc:
            raise ValueError(
                f"Give the amount in a known unit (kg, t, l, kg/ha, l/ha…), not {self.unit!r}."
            ) from exc
        if measured.measure not in AMOUNT_MEASURES:
            raise ValueError("Give the amount as a mass or a volume, in total or per hectare.")
        return self

    @property
    def measure(self) -> str:
        return units.measure(self.value, self.unit).measure


class Composition(_Strict):
    """Declared nutrient contents, % by mass, and where they come from; never inferred."""

    nutrients: dict[Nutrient, Annotated[Decimal, Field(ge=0, le=100, allow_inf_nan=False)]] = Field(
        min_length=1
    )
    source: CompositionSource | None = None
    reference: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def provenance(self):
        if self.source is None:
            raise ValueError(
                "Say where the composition comes from: the label, an analysis, the supplier's "
                "sheet or your own figure."
            )
        if sum(self.nutrients.values()) > 100:
            raise ValueError("The nutrient contents add up to more than 100 %.")
        return self


class EnteredFactor(_Strict):
    value: Positive
    source: str = Field(default="farmer", max_length=200)


class FertilizationDraft(_Strict):
    parcel_ids: list[RecordId] = Field(default_factory=list, max_length=100)
    ts_start: datetime | None = None
    fertiliser: Fertiliser | None = None
    amount: Amount | None = None
    composition: Composition | None = None
    density_kg_per_l: EnteredFactor | None = None
    area_ha: EnteredFactor | None = None  # the area fertilised, when it is not the parcels'
    notes: str = Field(default="", max_length=2000)
    missing: list[Missing] = Field(default_factory=list, max_length=8)  # set by `_checked`

    @field_validator("ts_start")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("Supply a timezone.")
        # One instant, one spelling: a correction compares instants, never offsets.
        return value.astimezone(UTC) if value is not None else None


def _parcels(session: Session, farm_id: int, ids: list[int]) -> list[Parcel]:
    found = list(
        session.scalars(select(Parcel).where(Parcel.id.in_(ids), Parcel.farm_id == farm_id))
    )
    if {p.id for p in found} != set(ids):
        raise ValueError("Some parcels are not on this farm.")
    return sorted(found, key=lambda p: ids.index(p.id))


def _checked(session: Session, farm_id: int, payload: dict) -> FertilizationDraft:
    """The payload validated against the farm, with `missing` decided here."""
    record = FertilizationDraft.model_validate({**payload, "missing": []})
    parcels = _parcels(session, farm_id, record.parcel_ids)
    item = record.fertiliser.catalog_item_id if record.fertiliser else None
    if (
        item is not None
        and session.scalar(offered_items(farm_id).where(CatalogItem.id == item)) is None
    ):
        raise ValueError("This fertiliser is not in the catalogue.")
    missing: list[str] = []
    for name, absent in (
        ("parcel", not parcels),
        ("date", record.ts_start is None),
        ("fertiliser", record.fertiliser is None),
        ("amount", record.amount is None),
        ("composition", record.composition is None),
    ):
        if absent:
            missing.append(name)
    if record.amount is not None:
        measure = record.amount.measure
        unknown_area = not parcels or any(p.area_ha is None or p.area_ha <= 0 for p in parcels)
        if measure in RATES and record.area_ha is None and parcels and unknown_area:
            missing.append("area")
        # Litres need kg/l for the composition (a share by mass); kg against a stock kept in
        # litres needs it too.
        in_litres = _stock_unit(session, farm_id, item, record.ts_start) == "l"
        if record.density_kg_per_l is None and (measure in VOLUMES or in_litres):
            missing.append("density")
    record.missing = missing
    return record


def offered_items(farm_id: int):
    """Catalog items a fertilization may name: not a crop-protection product, or one this farm
    keeps a stock row of (`select(CatalogItem.id, CatalogItem.name)`)."""
    return select(CatalogItem.id, CatalogItem.name).where(
        ~select(CropProtectionItem.catalog_item_id)
        .where(CropProtectionItem.catalog_item_id == CatalogItem.id)
        .exists()
        | select(InventoryStock.id)
        .where(InventoryStock.catalog_item_id == CatalogItem.id, InventoryStock.farm_id == farm_id)
        .exists()
    )


def farm_zone(session: Session, farm_id: int) -> ZoneInfo:
    """The farm's own time zone, else the installation's (`core.farm_location`)."""
    return ZoneInfo(farm_time_zone(session.get(Farm, farm_id), get_settings()))


def _stock_unit(session: Session, farm_id: int, item: int | None, at: datetime | None):
    """The unit (`kg` or `l`) this farm stocks `item` in for the application's season."""
    if item is None or at is None:
        return None
    season = at.astimezone(farm_zone(session, farm_id)).year
    return session.scalar(
        select(InventoryStock.unit).where(
            InventoryStock.farm_id == farm_id,
            InventoryStock.catalog_item_id == item,
            InventoryStock.season == season,
        )
    )


def validate(session: Session, farm_id: int, payload: dict) -> dict:
    return _checked(session, farm_id, payload).model_dump(mode="json", exclude_none=True)


def incomplete(payload: dict) -> str:
    """What a draft still needs, in words; "" when it is complete."""
    return ", ".join(MISSING_WORDS.get(m, m) for m in payload.get("missing") or [])


def seasons_on(
    session: Session, farm_id: int, parcel_ids: list[int], at: datetime
) -> dict[str, int | None]:
    """The crop season of each parcel on the farm-local day of `at` (None: no season covers it,
    which is unknown, shown as "not recorded")."""
    day = at.astimezone(farm_zone(session, farm_id)).date()
    out: dict[str, int | None] = {}
    for parcel_id in parcel_ids:
        out[str(parcel_id)] = session.scalar(
            select(CropSeason.id).where(
                CropSeason.farm_id == farm_id,
                CropSeason.parcel_id == parcel_id,
                CropSeason.starts_on <= day,
                (CropSeason.ends_on.is_(None)) | (CropSeason.ends_on >= day),
            )
        )
    return out


def _props(session: Session, farm_id: int, record: FertilizationDraft) -> dict[str, Any]:
    facts = record.model_dump(
        mode="json", include={"fertiliser", "composition", "notes"}, exclude_none=True
    )
    facts["seasons"] = seasons_on(session, farm_id, record.parcel_ids, record.ts_start)
    factors = {}
    if record.area_ha is not None:
        factors["area_ha"] = decimal_text(record.area_ha.value)
        facts["area_source"] = record.area_ha.source
    if record.density_kg_per_l is not None:
        factors["density_kg_per_l"] = decimal_text(record.density_kg_per_l.value)
        facts["density_source"] = record.density_kg_per_l.source
    return {PROPS_KEY: facts, **({FACTORS: factors} if factors else {})}


def _quantity(record: FertilizationDraft) -> EnteredQuantity:
    return EnteredQuantity(
        record.amount.value,
        record.amount.unit,
        label=record.fertiliser.name,
        catalog_item_id=record.fertiliser.catalog_item_id,
    )


def _complete(record: FertilizationDraft, payload: dict) -> None:
    if record.missing:
        raise ValueError(
            f"This draft is incomplete: it still needs {incomplete(payload)}. Nothing was recorded."
        )


def confirm(
    session: Session,
    draft: Draft,
    *,
    actor: str,
    now: datetime,
    services: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Write the draft as one done `fertilise` log; returns ("log", id)."""
    record = _checked(session, draft.farm_id, draft.payload)
    _complete(record, record.model_dump(mode="json"))
    if record.ts_start > now:
        raise ValueError(
            "This application is dated after the current time; planned work is not recorded."
        )
    provenance = dict(draft.provenance)
    log, held = logs.create_pending_once(
        session,
        farm_id=draft.farm_id,
        type=LOG_TYPE,
        parcel_ids=record.parcel_ids,
        ts_start=record.ts_start,
        ts_end=None,
        quantities=[_quantity(record)],
        source=SourceInput(
            source_type=provenance["source_type"],
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
            precision_rank=RANKS[provenance["source_type"]],
        ),
        actor=actor,
        occurred_at=now,
        name=record.fertiliser.name,
        source_app=SOURCE_APP,
    )
    if held:  # the draft's source is already on a record: never write it twice
        return "log", log.id
    log.props = _props(session, draft.farm_id, record)
    log, _merged = logs.confirm(session, log.id, actor, now, source_app=SOURCE_APP)
    return "log", log.id


# --- Corrections ---------------------------------------------------------------------------

CORRECTABLE = (
    "parcel_ids",
    "ts_start",
    "fertiliser",
    "amount",
    "composition",
    "density_kg_per_l",
    "area_ha",
    "notes",
)


class CorrectionDraft(_Strict):
    log_id: RecordId
    reason: str = Field(min_length=1, max_length=4000)
    changes: dict[str, Any] = Field(min_length=1)
    base: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")  # set at propose
    after: dict[str, Any] = Field(default_factory=dict)  # set by `validate_correction`
    missing: list[Missing] = Field(default_factory=list, max_length=8)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Say why the record is corrected.")
        return value

    @field_validator("changes")
    @classmethod
    def known(cls, value):
        unknown = set(value) - set(CORRECTABLE)
        if unknown:
            raise ValueError(f"These facts cannot be corrected: {sorted(unknown)}.")
        return value


def record_facts(log: Log) -> dict[str, Any]:
    """The record as a draft payload would give it: the facts a correction starts from."""
    facts = (log.props or {}).get(PROPS_KEY) or {}
    factors = (log.props or {}).get(FACTORS) or {}
    typed = log_quantity(log)
    out: dict[str, Any] = {
        "parcel_ids": sorted(p.id for p in log.parcels),
        "ts_start": log.ts_start.astimezone(UTC).isoformat(),
        "fertiliser": facts.get("fertiliser"),
        "amount": {"value": decimal_text(typed.entered_value), "unit": typed.entered_unit}
        if typed is not None
        else None,
        "composition": facts.get("composition"),
        "notes": facts.get("notes", ""),
    }
    for key, source in (("density_kg_per_l", "density_source"), ("area_ha", "area_source")):
        if key in factors:
            out[key] = {"value": factors[key], "source": facts.get(source, "farmer")}
    return {k: v for k, v in out.items() if v is not None}


def log_quantity(log: Log) -> TypedQuantity | None:
    from sqlalchemy.orm import object_session

    return object_session(log).scalar(
        select(TypedQuantity).where(TypedQuantity.log_id == log.id).order_by(TypedQuantity.id)
    )


def snapshot(log: Log) -> dict[str, Any]:
    """Everything a correction replaces, plus the record's sources; its fingerprint is the base."""
    return {
        "version": 1,
        "type": log.type,
        "status": log.status,
        **record_facts(log),
        "seasons": ((log.props or {}).get(PROPS_KEY) or {}).get("seasons"),
        "sources": sorted(s.id for s in log.sources),
    }


def _record(session: Session, farm_id: int, log_id: int, *, lock: bool = False) -> Log:
    stmt = select(Log).where(Log.id == log_id, Log.farm_id == farm_id)
    if lock:
        stmt = stmt.with_for_update()
    log = session.scalar(stmt.execution_options(populate_existing=True))
    if log is None or log.type != LOG_TYPE or log.status != "done":
        raise ValueError("Confirmed fertilization record not found.")
    return log


def _corrected(session: Session, farm_id: int, payload: dict) -> CorrectionDraft:
    correction = CorrectionDraft.model_validate({**payload, "after": {}, "missing": []})
    log = _record(session, farm_id, correction.log_id)
    before = record_facts(log)
    after = {**before, **correction.changes}
    after = {k: v for k, v in after.items() if v is not None}
    record = _checked(session, farm_id, after)
    clean = record.model_dump(mode="json", exclude_none=True, exclude={"missing"})
    if clean == FertilizationDraft.model_validate(before).model_dump(
        mode="json", exclude_none=True, exclude={"missing"}
    ):
        raise ValueError("Supply changed values.")
    correction.after = clean
    correction.missing = record.missing
    if correction.base is None:
        correction.base = fingerprint(snapshot(log))
    return correction


def validate_correction(session: Session, farm_id: int, payload: dict) -> dict:
    return _corrected(session, farm_id, payload).model_dump(mode="json", exclude_none=True)


def _actor_label(session: Session, actor: str) -> str:
    kind, _, raw = actor.partition(":")
    if kind == "identity" and raw.isdigit():
        identity = session.get(LocalIdentity, int(raw))
        if identity is not None:
            return identity.login
    return "Installation operator" if actor == "operator" else actor


def confirm_correction(
    session: Session,
    draft: Draft,
    *,
    actor: str,
    now: datetime,
    services: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Apply a reviewed correction once; refuse it if the record changed since it was proposed."""
    correction = CorrectionDraft.model_validate(draft.payload)
    log = _record(session, draft.farm_id, correction.log_id, lock=True)
    session.expire(log, ["parcels", "sources"])
    before = snapshot(log)
    if fingerprint(before) != correction.base:
        raise ValueError(
            "This record changed since the correction was proposed; nothing was corrected. "
            "Propose the correction again."
        )
    checked = _corrected(session, draft.farm_id, draft.payload)
    record = FertilizationDraft.model_validate(checked.after)
    _complete(record, checked.model_dump(mode="json"))
    if record.ts_start > now:
        raise ValueError("A corrected application cannot be dated after the current time.")
    log.parcels = _parcels(session, draft.farm_id, record.parcel_ids)
    log.ts_start = record.ts_start
    log.name = record.fertiliser.name
    log.props = _props(session, draft.farm_id, record)
    session.execute(delete(TypedQuantity).where(TypedQuantity.log_id == log.id))
    q = _quantity(record)
    enter_quantity(
        session,
        log_id=log.id,
        value=q.value,
        unit=q.unit,
        label=q.label,
        catalog_item_id=q.catalog_item_id,
    )
    session.flush()
    after = snapshot(log)
    receipt = OperationCorrection(
        farm_id=draft.farm_id,
        original_log_id=log.id,
        live_log_id=log.id,
        request_key=f"draft:{draft.id}",
        actor_key=draft.actor,
        actor_label=_actor_label(session, draft.actor),
        creator_identity_id=None,
        status="confirmed",
        reason=correction.reason,
        base_fingerprint=correction.base,
        before_snapshot=before,
        after_snapshot=after,
        finalized_at=now,
        finalized_actor_key=actor,
        finalized_actor_label=_actor_label(session, actor),
    )
    session.add(receipt)
    session.flush()
    for parcel_id in sorted(set(before["parcel_ids"]) | set(after["parcel_ids"])):
        post_event(
            session,
            farm_id=draft.farm_id,
            res_model="parcel",
            res_id=parcel_id,
            kind="log.corrected",
            body="Fertilization corrected",
            payload={"log_id": log.id, "correction_id": receipt.id, "type": LOG_TYPE},
            actor=actor,
            source_app=SOURCE_APP,
            occurred_at=now,
        )
    return "log", log.id

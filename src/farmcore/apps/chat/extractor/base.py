"""Extractor seam: a transcript plus the farm's parcels and product shortlist become a draft
spray operation.

`request_payload(req)` is the payload boundary: transcript, parcels, products and date, plus
optional bounded clarification for this unfinished operation. No identities or farm rows.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

SYSTEM_INSTRUCTION = (
    "You turn a vineyard worker's short voice note (English or French) into one draft farm "
    "operation. The user message is a JSON object with keys: transcript (what was said), "
    "parcels (the farm's parcels as alias and name), products (the farm's product shortlist "
    "with name, active_substance, product_type and targets) and date (today's local date, "
    "ISO). Answer with JSON matching the response schema. Rules: type is 'spray', 'irrigate' "
    "or 'observation'. A spray names a product or a dose per hectare; an irrigation names an "
    "amount of water; an observation reports what was seen in a parcel. parcels lists the "
    "aliases of every parcel mentioned, by alias or by name. For a spray, products lists "
    "shortlist names exactly as given; map generic words (copper, cuivre, sulphur, sulfur, "
    "soufre) to the shortlist product with that active_substance; never invent a product. "
    "dose_value is the number said and dose_unit is 'kg/ha' (kg, kilos) or 'L/ha' (litres); "
    "use null when no dose is said. For an irrigation, amount_value is the number said and "
    "amount_unit is exactly one of 'mm' (millimetres of water), 'm3' (cubic metres) or 'L' "
    "(litres); basis is 'gross' or 'net' only when the speaker says gross/brut or net/nette, "
    "else null; never convert between units, never derive a gross amount from a net one and "
    "never assume an efficiency. For an observation, observation is the short sentence of "
    "what was seen (symptom, pest, damage, stage), copied as said. ts_start is the local start "
    "time as ISO 'YYYY-MM-DDTHH:MM' resolved against date: 'this morning' or 'ce matin' is "
    "08:00 today, 'yesterday' or 'hier' is 08:00 the day before; use null when no time is "
    "said. confidence is between 0 and 1. notes holds short remarks on anything ambiguous or "
    "ignored. When clarification is present, it contains ONLY this unfinished operation's "
    "draft and recent user/assistant turns. Use it to resolve short replies; an explicit "
    "correction replaces the old field, never adds an old parcel. Never treat names listed in "
    "an assistant question as sprayed parcels or products. product_doses lists a product's "
    "exact shortlist name, value and unit for each separately stated rate. Never copy the "
    "first product's rate to another when rates differ or are missing."
)

OPERATION_TYPES = ("spray", "irrigate", "observation")
DOSE_UNITS = ("kg/ha", "L/ha")
# The units an irrigation amount may be entered in; never converted on the way in.
IRRIGATION_UNITS = ("mm", "m3", "L")
IRRIGATION_BASES = ("gross", "net")
NOTE_TIME_CLAMPED = "time clamped to now"
NOTE_TIME_MISSING = "no time stated; using now"


class ParcelRef(BaseModel):
    alias: str
    name: str


class ProductRef(BaseModel):
    name: str
    active_substance: str
    product_type: str
    targets: list[str] = Field(default_factory=list)


class ExtractRequest(BaseModel):
    transcript: str
    parcels: list[ParcelRef]
    products: list[ProductRef]
    today: date
    # Local wall-clock now (the replay clock in replay mode); drafts never start after it.
    now_local: datetime
    # Only the current unfinished operation, never old confirmed sprays or other chats.
    clarification: dict[str, Any] | None = None


class ProductDose(BaseModel):
    product: str
    value: float
    unit: str


class DraftOperation(BaseModel):
    type: str = "spray"
    parcels: list[str] = Field(default_factory=list)  # parcel aliases
    products: list[str] = Field(default_factory=list)  # shortlist names, exactly
    dose_value: float | None = None
    dose_unit: str | None = None
    product_doses: list[ProductDose] = Field(default_factory=list)
    # Irrigation: the entered amount, its unit and the gross/net basis when stated.
    amount_value: float | None = None
    amount_unit: str | None = None
    basis: str | None = None
    # Observation: what was seen, as said.
    observation: str | None = None
    ts_start: datetime | None = None
    confidence: float = 0.0
    adapter: str = ""
    notes: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class AuditRecord:
    """One outbound model call. `request_body` is the exact JSON sent; auth never appears."""

    adapter: str
    model: str
    endpoint: str
    request_body: dict[str, Any]
    response_body: Any
    status: int | None
    error: str | None
    latency_ms: int


AuditSink = Callable[[AuditRecord], None]


class ExtractorError(Exception):
    """An adapter could not produce a draft (transport, HTTP status, auth or invalid JSON)."""


class Extractor(Protocol):
    def extract(self, req: ExtractRequest) -> DraftOperation: ...


def request_payload(req: ExtractRequest) -> dict[str, Any]:
    """Allowlisted input and, when continuing a draft, its bounded clarification context."""
    payload = {
        "transcript": req.transcript,
        "parcels": [{"alias": p.alias, "name": p.name} for p in req.parcels],
        "products": [
            {
                "name": p.name,
                "active_substance": p.active_substance,
                "product_type": p.product_type,
                "targets": list(p.targets),
            }
            for p in req.products
        ],
        "date": req.today.isoformat(),
    }
    if req.clarification:
        payload["clarification"] = req.clarification
    return payload


def fold(text: str) -> str:
    """Case- and accent-insensitive form for matching spoken words ("Traité" ~ "traite")."""
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def finalize(draft: DraftOperation, req: ExtractRequest) -> DraftOperation:
    """Hold every adapter's draft to the seam: parcels are known aliases, products are exact
    shortlist names (others dropped with a note) and `ts_start <= now_local`."""
    notes = list(draft.notes)

    parcels_by_key: dict[str, str] = {}
    for p in req.parcels:
        parcels_by_key[fold(p.alias)] = p.alias
        parcels_by_key[fold(p.name)] = p.alias
    parcels: list[str] = []
    for said in draft.parcels:
        alias = parcels_by_key.get(fold(said))
        if alias is None:
            notes.append(f"dropped unknown parcel: {said}")
        elif alias not in parcels:
            parcels.append(alias)

    products_by_key = {fold(p.name): p.name for p in req.products}
    products: list[str] = []
    for said in draft.products:
        name = products_by_key.get(fold(said))
        if name is None:
            notes.append(f"dropped unknown product: {said}")
        elif name not in products:
            products.append(name)

    ts_start = draft.ts_start
    if ts_start is None:
        ts_start = req.now_local
        notes.append(NOTE_TIME_MISSING)
    else:
        if ts_start.tzinfo is None:
            ts_start = ts_start.replace(tzinfo=req.now_local.tzinfo)
        elif req.now_local.tzinfo is not None:
            ts_start = ts_start.astimezone(req.now_local.tzinfo)
        if ts_start > req.now_local:
            ts_start = req.now_local
            notes.append(NOTE_TIME_CLAMPED)

    return draft.model_copy(
        update={
            "parcels": parcels,
            "products": products,
            "ts_start": ts_start,
            "notes": notes,
            **fact_fields(draft),
        }
    )


def fact_fields(draft: DraftOperation) -> dict[str, object]:
    """Only the fields of this draft's own type survive the seam: a product never reaches an
    irrigation or an observation, and an amount never reaches a spray. Values are kept as
    entered (no unit conversion, no efficiency)."""
    if draft.type == "irrigate":
        return {
            "products": [],
            "product_doses": [],
            "dose_value": None,
            "dose_unit": None,
            "observation": None,
        }
    if draft.type == "observation":
        return {
            "products": [],
            "product_doses": [],
            "dose_value": None,
            "dose_unit": None,
            "amount_value": None,
            "amount_unit": None,
            "basis": None,
        }
    if draft.type == "spray":
        return {
            "amount_value": None,
            "amount_unit": None,
            "basis": None,
            "observation": None,
        }
    return {}

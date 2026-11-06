"""From a farm and a message to an `ExtractRequest`, and from a draft to a checked spray.

Reads go through a `ScopedRepository` bound to the chat's farm. The request carries the
farm's parcels (alias + name), the product shortlist and the clock's local date and time, so a
replayed "this morning" lands on the replay day.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from farmcore.apps.chat.extractor import (
    DraftOperation,
    ExtractRequest,
    ParcelRef,
    ProductRef,
)
from farmcore.apps.chat.extractor.base import (
    DOSE_UNITS,
    IRRIGATION_BASES,
    IRRIGATION_UNITS,
    NOTE_TIME_CLAMPED,
    fold,
)
from farmcore.apps.operations import facts
from farmcore.apps.operations.catalogue import products_for_farm
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Log, Parcel

# Plausible spray dose per hectare, either unit; outside it the bot asks again rather than
# logging a mis-heard number.
DOSE_MIN = 0.1
DOSE_MAX = 20.0
# `conversation.MAX_CHARS` bounds the transcript; an observation keeps that bound in props.
MAX_OBSERVATION_CHARS = 2000
# The echo truncates a long observation; the record keeps it whole.
OBSERVATION_SUMMARY_CHARS = 160
# Note prefixes the extractor's `finalize` writes for names it could not match (06's seam).
NOTE_UNKNOWN_PARCEL = "dropped unknown parcel:"
NOTE_UNKNOWN_PRODUCT = "dropped unknown product:"


@dataclass(frozen=True)
class ParcelInfo:
    id: int
    alias: str
    name: str


@dataclass(frozen=True)
class ProductInfo:
    id: int
    name: str
    active_substance: str
    product_type: str
    targets: tuple[str, ...]


@dataclass(frozen=True)
class FarmContext:
    """What one message is checked against, read once per message."""

    parcels: list[ParcelInfo]
    products: list[ProductInfo]
    now_utc: datetime
    tz: ZoneInfo

    @property
    def now_local(self) -> datetime:
        return self.now_utc.astimezone(self.tz)

    def request(self, transcript: str) -> ExtractRequest:
        return ExtractRequest(
            transcript=transcript,
            parcels=[ParcelRef(alias=p.alias, name=p.name) for p in self.parcels],
            products=[
                ProductRef(
                    name=p.name,
                    active_substance=p.active_substance,
                    product_type=p.product_type,
                    targets=list(p.targets),
                )
                for p in self.products
            ],
            today=self.now_local.date(),
            now_local=self.now_local,
        )


def _alias_key(alias: str) -> tuple[int, str]:
    m = re.fullmatch(r"[Pp](\d+)", alias)
    return (int(m.group(1)), alias) if m else (10**9, alias)


def load_context(repo: ScopedRepository, now_utc: datetime, tz: str) -> FarmContext:
    parcels = sorted(repo.list(Parcel, scope="fields:read"), key=lambda p: _alias_key(p.alias))
    products = products_for_farm(repo, scope="logs:write")
    return FarmContext(
        parcels=[ParcelInfo(p.id, p.alias, p.name) for p in parcels],
        products=[
            ProductInfo(p.id, p.name, p.active_substance, p.product_type, tuple(p.targets))
            for p in products
        ],
        now_utc=now_utc,
        tz=ZoneInfo(tz),
    )


@dataclass(frozen=True)
class CheckedSpray:
    parcels: list[ParcelInfo]
    products: list[ProductInfo]
    dose_value: float
    dose_unit: str
    ts_start: datetime  # UTC
    notes: list[str]
    product_doses: tuple[tuple[ProductInfo, float, str], ...] = ()


@dataclass(frozen=True)
class CheckedIrrigation:
    parcels: list[ParcelInfo]
    amount_value: float
    amount_unit: str
    basis: str | None
    ts_start: datetime
    notes: list[str]


@dataclass(frozen=True)
class CheckedObservation:
    parcels: list[ParcelInfo]
    text: str
    ts_start: datetime
    notes: list[str]


Checked = CheckedSpray | CheckedIrrigation | CheckedObservation


class Unclear(ValueError):
    """The draft cannot become a log; `reply` asks the farmer for what is missing."""

    def __init__(self, reply: str) -> None:
        super().__init__(reply)
        self.reply = reply


def _dropped(notes: list[str], prefix: str) -> list[str]:
    return [n.removeprefix(prefix).strip() for n in notes if n.startswith(prefix)]


def _parcel_list(ctx: FarmContext) -> str:
    return "Your parcels: " + ", ".join(f"{p.alias} {p.name}" for p in ctx.parcels) + "."


def _product_list(ctx: FarmContext) -> str:
    names = ", ".join(f"{p.name} ({p.active_substance})" for p in ctx.products)
    return f"Products on your list: {names}."


def check(draft: DraftOperation, ctx: FarmContext) -> Checked:
    """Map the draft onto the farm, clamp its start to now, and check what its type needs;
    raise `Unclear` with a clarifying reply when it cannot be logged."""
    if draft.type == "irrigate":
        return _check_irrigation(draft, ctx)
    if draft.type == "observation":
        return _check_observation(draft, ctx)
    if draft.type != "spray":
        raise Unclear("I can only record a spray, an irrigation or a field observation.")
    return _check_spray(draft, ctx)


def _parcel_question(kind: str) -> str:
    if kind == "irrigate":
        return "Which parcel did you irrigate?"
    if kind == "observation":
        return "Which parcel is this about?"
    return "Which parcel did you spray?"


def _checked_parcels(draft: DraftOperation, ctx: FarmContext, kind: str) -> list[ParcelInfo]:
    by_alias = {fold(p.alias): p for p in ctx.parcels}
    by_alias.update({fold(p.name): p for p in ctx.parcels})
    notes = list(draft.notes)
    unknown = _dropped(notes, NOTE_UNKNOWN_PARCEL)
    unknown += [a for a in draft.parcels if fold(a) not in by_alias]
    if unknown:
        raise Unclear(
            f"I don't know parcel {', '.join(dict.fromkeys(unknown))}. "
            f"{_parcel_list(ctx)} {_parcel_question(kind)}"
        )
    parcels = list(dict.fromkeys(by_alias[fold(a)] for a in draft.parcels))
    if not parcels:
        raise Unclear(f"{_parcel_question(kind)} {_parcel_list(ctx)}")
    return parcels


def _checked_ts(draft: DraftOperation, ctx: FarmContext, notes: list[str]) -> datetime:
    ts = draft.ts_start or ctx.now_local
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ctx.tz)
    if ts > ctx.now_utc:  # a draft never starts after the clock's now
        ts = ctx.now_utc
        if NOTE_TIME_CLAMPED not in notes:
            notes.append(NOTE_TIME_CLAMPED)
    return ts.astimezone(ctx.now_utc.tzinfo)


def _check_spray(draft: DraftOperation, ctx: FarmContext) -> CheckedSpray:
    """The existing spray path: parcels, shortlist products, dose and start time."""
    by_name = {fold(p.name): p for p in ctx.products}
    notes = list(draft.notes)
    parcels = _checked_parcels(draft, ctx, "spray")

    unknown_products = _dropped(notes, NOTE_UNKNOWN_PRODUCT)
    unknown_products += [n for n in draft.products if fold(n) not in by_name]
    if unknown_products:
        raise Unclear(
            f"I don't know the product {', '.join(dict.fromkeys(unknown_products))}. "
            f"{_product_list(ctx)}"
        )
    products = list(dict.fromkeys(by_name[fold(n)] for n in draft.products))
    if not products:
        raise Unclear(f"Which product did you spray? {_product_list(ctx)}")

    rates = []
    if draft.product_doses:
        named = {dose.product: dose for dose in draft.product_doses}
        for product in products:
            dose = named.get(product.name)
            if dose is None:
                raise Unclear(f"How much {product.name} did you use? Give its dose per hectare.")
            if dose.unit not in DOSE_UNITS or not DOSE_MIN <= dose.value <= DOSE_MAX:
                raise Unclear(f"Please repeat the dose per hectare for {product.name}.")
            rates.append((product, dose.value, dose.unit))
    if not rates and (draft.dose_value is None or draft.dose_unit not in DOSE_UNITS):
        raise Unclear('How much did you use? Say the dose per hectare, for example "2 kg/ha".')
    if not rates and not DOSE_MIN <= draft.dose_value <= DOSE_MAX:
        raise Unclear(
            f"A dose of {draft.dose_value:g} {draft.dose_unit} looks wrong. "
            f"I expect {DOSE_MIN:g} to {DOSE_MAX:g} kg/ha or L/ha. Please say it again."
        )

    if not rates:
        rates = [(product, float(draft.dose_value), draft.dose_unit) for product in products]
    return CheckedSpray(
        parcels=parcels,
        products=products,
        dose_value=rates[0][1],
        dose_unit=rates[0][2],
        ts_start=_checked_ts(draft, ctx, notes),
        notes=notes,
        product_doses=tuple(rates),
    )


def _check_irrigation(draft: DraftOperation, ctx: FarmContext) -> CheckedIrrigation:
    """Parcels, an explicit amount and unit, and the gross/net basis only when stated. No unit
    conversion and no efficiency: a net amount is never turned into a gross one."""
    notes = list(draft.notes)
    parcels = _checked_parcels(draft, ctx, "irrigate")
    unit = (draft.amount_unit or "").strip()
    if unit.casefold() in ("l", "litre", "liter", "litres", "liters"):
        unit = "L"
    if (
        draft.amount_value is None
        or not math.isfinite(draft.amount_value)
        or draft.amount_value <= 0
        or not unit
    ):
        raise Unclear(
            "How much water, and in which unit? Say the amount with mm, m3 or L, "
            'for example "20 mm".'
        )
    if unit not in IRRIGATION_UNITS:
        raise Unclear(
            "I only record irrigation in mm, m3 or L. Say the amount with one of those units, "
            "as it was entered."
        )
    basis = (draft.basis or "").strip().casefold() or None
    if basis is not None and basis not in IRRIGATION_BASES:
        raise Unclear('Was that gross or net? Say "gross" or "net"; I never assume an efficiency.')
    return CheckedIrrigation(
        parcels=parcels,
        amount_value=float(draft.amount_value),
        amount_unit=unit,
        basis=basis,
        ts_start=_checked_ts(draft, ctx, notes),
        notes=notes,
    )


def _check_observation(draft: DraftOperation, ctx: FarmContext) -> CheckedObservation:
    """Parcels and the sentence of what was seen."""
    notes = list(draft.notes)
    parcels = _checked_parcels(draft, ctx, "observation")
    text = (draft.observation or "").strip()[:MAX_OBSERVATION_CHARS]
    if not text:
        raise Unclear("What did you observe? Describe what you saw, and on which parcel.")
    return CheckedObservation(
        parcels=parcels,
        text=text,
        ts_start=_checked_ts(draft, ctx, notes),
        notes=notes,
    )


def _summary(
    parcels: list[tuple[str, str]],
    substances: list[str],
    dose: str,
    ts_start: datetime,
    tz: ZoneInfo,
) -> str:
    when = ts_start.astimezone(tz).strftime("%d %b %H:%M")
    names = ", ".join(f"{alias} {name}" for alias, name in parcels)
    return f"{names} · {' + '.join(dict.fromkeys(substances))} · {dose} · {when}"


def summary(spray: CheckedSpray, tz: ZoneInfo) -> str:
    """`P1 Le Plateau · copper + sulphur · 2 kg/ha · 10 Apr 06:00` (local time)."""
    return _summary(
        [(p.alias, p.name) for p in spray.parcels],
        [p.active_substance for p in spray.products],
        " / ".join(dict.fromkeys(f"{value:g} {unit}" for _, value, unit in spray.product_doses))
        if spray.product_doses
        else f"{spray.dose_value:g} {spray.dose_unit}",
        spray.ts_start,
        tz,
    )


def _names(parcels: list[ParcelInfo]) -> str:
    return ", ".join(
        f"{p.alias} {p.name}" for p in sorted(parcels, key=lambda p: _alias_key(p.alias))
    )


def _fact_line(parcels: list[ParcelInfo], detail: str, ts_start: datetime, tz: ZoneInfo) -> str:
    when = ts_start.astimezone(tz).strftime("%d %b %H:%M")
    return f"{_names(parcels)} · {detail} · {when}"


def irrigation_detail(amount_value: float, unit: str, basis: str | None) -> str:
    """`irrigation 20 mm gross`, or `irrigation 12 m3, basis not stated` (never an assumed one)."""
    detail = f"irrigation {facts.amount_text(amount_value)} {unit}"
    return f"{detail} {basis}" if basis else f"{detail}, basis not stated"


def observation_detail(text: str) -> str:
    short = (
        text
        if len(text) <= OBSERVATION_SUMMARY_CHARS
        else text[: OBSERVATION_SUMMARY_CHARS - 1] + "…"
    )
    return f"observation: {short}"


def checked_summary(checked: Checked, tz: ZoneInfo) -> str:
    """The preview line for any checked operation type."""
    if isinstance(checked, CheckedSpray):
        return summary(checked, tz)
    if isinstance(checked, CheckedIrrigation):
        detail = irrigation_detail(checked.amount_value, checked.amount_unit, checked.basis)
    else:
        detail = observation_detail(checked.text)
    return _fact_line(checked.parcels, detail, checked.ts_start, tz)


def log_summary(log: Log, tz: ZoneInfo) -> str:
    """The same line for a stored log: a spray's rate quantities, or a fact's validated props."""
    if log.type in ("irrigate", "observation"):
        if log.type == "irrigate":
            fact = facts.irrigation(log.props)
            detail = (
                irrigation_detail(float(fact.amount), fact.unit, fact.basis)
                if fact is not None
                else "irrigation"
            )
        else:
            fact = facts.observation(log.props)
            detail = observation_detail(fact.text) if fact is not None else "observation"
        parcels = [ParcelInfo(p.id, p.alias, p.name) for p in log.parcels]
        return _fact_line(parcels, detail, log.ts_start, tz)
    rates = [q for q in log.quantities if q.measure == "rate"]
    doses = list(dict.fromkeys(f"{q.value.normalize():f} {q.unit}" for q in rates))
    return _summary(
        sorted(((p.alias, p.name) for p in log.parcels), key=lambda a: _alias_key(a[0])),
        [q.product.active_substance if q.product else (q.label or "?") for q in rates],
        " / ".join(doses) or "no dose",
        log.ts_start,
        tz,
    )

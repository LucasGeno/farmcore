"""What the farmer reads: this season's confirmed applications, through the scoped repository.

Nutrients per hectare are shown only when the entered facts convert exactly (`core.units`): the
amount per hectare (a total divided by the area fertilised; litres times the entered density)
times each declared share. Otherwise that figure is "not computable", never estimated.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select

from farmcore.apps.fertilization.records import LOG_TYPE, PROPS_KEY
from farmcore.apps.inventory.service import season_start
from farmcore.apps.operations.corrections import decimal_text
from farmcore.core import units
from farmcore.core.access import ScopedRepository
from farmcore.core.logs import FACTORS
from farmcore.core.models.canonical import Log, LogParcel, Parcel
from farmcore.core.models.catalog import TypedQuantity

LOGS_SCOPE = "logs:read"
FIELDS_SCOPE = "fields:read"


@dataclass(frozen=True)
class Row:
    log_id: int
    local_start: datetime
    parcel_ids: list[int]
    parcel_names: list[str]
    fertiliser: str
    amount: str
    composition: dict
    per_ha: dict[str, str] | None  # nutrient -> kg/ha; None: not computable from entered facts


def _factor(props: dict, key: str) -> Decimal | None:
    raw = (props.get(FACTORS) or {}).get(key)
    return None if raw is None else Decimal(str(raw))


def per_hectare(typed: TypedQuantity, props: dict, parcel_area: Decimal) -> dict[str, str] | None:
    composition = (props.get(PROPS_KEY) or {}).get("composition")
    if typed is None or typed.measure is None or not composition:
        return None
    area = _factor(props, "area_ha") or (parcel_area if parcel_area > 0 else None)
    try:
        measured = units.Measured(
            typed.measure, typed.value, typed.unit, typed.entered_value, typed.entered_unit
        )
        rate = units.convert(
            measured,
            "kg/ha",
            area_ha=area,
            density_kg_per_l=_factor(props, "density_kg_per_l"),
        )
    except units.UnitError:
        return None
    return {
        name: decimal_text(units.plain(rate * Decimal(share) / 100))
        for name, share in composition["nutrients"].items()
    }


def season_rows(
    repo: ScopedRepository, *, now: datetime, tz: str, parcel_id: int | None = None
) -> list[Row]:
    """Done fertilization records of this season (local calendar year) up to now, latest first."""
    _, start = season_start(now, tz)
    criteria = [
        Log.type == LOG_TYPE,
        Log.status == "done",
        Log.ts_start >= start,
        Log.ts_start <= now,
    ]
    if parcel_id is not None:
        criteria.append(
            Log.id.in_(select(LogParcel.log_id).where(LogParcel.parcel_id == parcel_id))
        )
    found = repo.list(Log, *criteria, scope=LOGS_SCOPE, order_by=(Log.ts_start.desc(), Log.id))
    if not found:
        return []
    ids = [log.id for log in found]
    links = repo.list(LogParcel, LogParcel.log_id.in_(ids), scope=LOGS_SCOPE)
    parcels = {
        p.id: p
        for p in repo.list(
            Parcel, Parcel.id.in_({link.parcel_id for link in links}), scope=FIELDS_SCOPE
        )
    }
    # The record's entered amount; a legacy quantity core_0018 moved (it has a measure) is not.
    typed = {
        t.log_id: t
        for t in repo.list(
            TypedQuantity,
            TypedQuantity.log_id.in_(ids),
            TypedQuantity.entered_measure.is_(None),
            scope=LOGS_SCOPE,
            order_by=TypedQuantity.id,
        )
    }
    rows = []
    for log in found:
        on = sorted(link.parcel_id for link in links if link.log_id == log.id)
        props = log.props or {}
        facts = props.get(PROPS_KEY) or {}
        t = typed.get(log.id)
        rows.append(
            Row(
                log_id=log.id,
                local_start=log.ts_start.astimezone(ZoneInfo(tz)),
                parcel_ids=on,
                parcel_names=[parcels[i].name for i in on if i in parcels],
                fertiliser=(facts.get("fertiliser") or {}).get("name", log.name),
                amount=f"{decimal_text(t.entered_value)} {t.entered_unit}" if t else "",
                composition=facts.get("composition") or {},
                per_ha=per_hectare(
                    t, props, sum((parcels[i].area_ha for i in on if i in parcels), Decimal(0))
                ),
            )
        )
    return rows

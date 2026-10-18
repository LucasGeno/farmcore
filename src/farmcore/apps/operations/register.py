"""Spray register (Reg. (EU) 2023/564): one row per done spray log × parcel × product.

Read through the `ScopedRepository` under `register:read`; bounded by the clock (`ts_start ≤
now`), so a stepped-back replay hides later sprays. Field order (`FIELDS`) is the CSV header and
the JSON key order; it is stable. A log with no product quantity still gets one row per parcel
with blank product fields, so a spray is never silently dropped.

- date / start_time: the parcel interval (`parcel_intervals`) in the display timezone;
  `start_time` is blank when `log.props["time_known"]` is false (records that carry a date only,
  e.g. third-party files).
- product: a quantity's product, else its `label` with `active_substance` (and the rest) from
  `log.props["products"][label]`; a `props.products` entry with no quantity (null dose) still
  gets its row, dose blank. Logs without `props.products` read as before.
- area_treated_ha: the parcel area, unless `log.props["area_treated_ha"]` overrides it (a number
  for every parcel, or a mapping of parcel id → ha).
- crop_name / crop_eppo: parcel props `crop_label` / `eppo_crop`; a parcel with neither is the
  demo vine (`VITVI`, grapevine); a named crop without a code stays blank.
- bbch: the log's, else the configured grapevine phase (`bbch.py`, an assumption) only on a
  `VITVI` parcel, else blank; `bbch_source` says which (`log`, `config`, `none`).
- authorisation_placeholder: the product's `props.placeholder` (E-Phy numbers pending, P2).
- checks (only with `checks=True`): the spray's label and copper checks (`checks.py`), the
  same list on every row of the spray, counted over the confirmed sprays this read sees (so a
  stepped-back replay counts only the sprays up to the clock). `to_csv` writes `CSV_FIELDS`: the
  EU columns, then `checks` as one line of words (blank on rows read without checks).
- saved_weather (only with `weather=True`, owner exports): the whitelisted snapshots
  (`spray_weather.project`) of every source on the row's log that applies to the row's parcel
  (a source's own original parcels, `spray_weather.for_parcel`), in source id order; an old
  record has none. Read from the stored payloads only: no station read, no recompute, no write.
  `to_csv(..., weather=True)` appends one `saved_weather` column of source-labelled words after
  `checks`, its header even on an empty register. Grant and DG AGRI reads keep the defaults.
- parcel_aliases (only with `aliases=True`, the JSON exports): the parcel's portable ids,
  portable `ext_id` rows naming it as `source:key` (e.g. `ign-rpg-2024:<id_parcel>`), sorted, so a
  row can be matched in another tool next to `iacs_parcel_id`. In the demo, that register
  column carries the open RPG polygon id as a stand-in for the IACS reference, not a true
  IACS/LPIS id. A Varda Global FieldID would be one more alias, source `varda` (`VARDA_SOURCE`),
  when one is recorded: nothing calls Varda or makes such an id up. Synthetic DG AGRI aliases
  are omitted when `props.id_label` is set, as for the NGSI-LD IACS entry. The CSV never carries
  aliases (`to_csv` writes the EU columns only).
- season export (D4, `since` / `before` / `parcel_id`): the rows of sprays with
  `since ≤ ts_start < before` (a season's 1 January, local, to the next; `season_window`, half
  open so a 1 January 00:00 spray is in one season only) and of one parcel only. The checks
  are still counted over every spray the read sees up to `now`, so a narrowed row keeps its
  full checks.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from farmcore.apps.operations import bbch as bbch_config
from farmcore.apps.operations import checks as label_checks
from farmcore.apps.operations import spray_weather
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import (
    ExtId,
    Log,
    LogParcel,
    LogSource,
    Parcel,
    Product,
    Quantity,
)

SCOPE = "register:read"
DEFAULT_EPPO = "VITVI"  # grapevine, Vitis vinifera (EPPO code)
DEFAULT_TZ = "Europe/Paris"
_NUMBERED_ALIAS = re.compile(r"^P(\d+)$")

FIELDS: tuple[str, ...] = (
    "product_name",
    "authorisation_no",
    "authorisation_placeholder",
    "active_substance",
    "date",
    "start_time",
    "dose",
    "dose_unit",
    "iacs_parcel_id",
    "parcel_alias",
    "area_treated_ha",
    "crop_name",
    "crop_eppo",
    "bbch",
    "bbch_source",
    "log_id",
)
CHECKS = "checks"
CSV_FIELDS: tuple[str, ...] = (*FIELDS, CHECKS)
SAVED_WEATHER = "saved_weather"  # owner opt-in only, after `checks`
PARCEL_ALIASES = "parcel_aliases"  # JSON exports only (`aliases=True`), last
VARDA_SOURCE = "varda"  # the slot for a Varda Global FieldID alias, when one is recorded


def alias_key(alias: str) -> tuple[int, int, str]:
    """Sort key for parcel aliases: `P1, P2, …, P10` by number, other aliases after, by name."""
    m = _NUMBERED_ALIAS.match(alias)
    return (0, int(m.group(1)), "") if m else (1, 0, alias)


def parcel_key(parcel: Parcel) -> tuple[tuple[int, int, str], int]:
    return alias_key(parcel.alias), parcel.id


def _area(log: Log, parcel: Parcel) -> Decimal:
    override = (log.props or {}).get("area_treated_ha")
    if isinstance(override, dict):
        override = override.get(str(parcel.id))
    if override is None:
        return parcel.area_ha
    return Decimal(str(override))


def _crop_eppo(props: dict[str, Any]) -> str:
    if props.get("eppo_crop"):
        return props["eppo_crop"]
    return "" if props.get("crop_label") else DEFAULT_EPPO


def _bbch(
    log: Log, local_day: Any, crop_eppo: str, *, configured: bool = True
) -> tuple[int | None, str]:
    if log.bbch is not None:
        return log.bbch, "log"
    if crop_eppo != DEFAULT_EPPO or not configured:
        return None, "none"  # the configured phases are grapevine phases
    phase = bbch_config.phase_on(local_day)
    return (phase, "config") if phase is not None else (None, "none")


def _product_cells(
    q: Quantity | None, product: Product | None, entry: dict[str, Any] | None
) -> dict[str, Any]:
    entry = entry or {}
    if product:
        name, substance = product.name, product.active_substance
    else:
        name = entry.get("name", q.label if q else None)
        substance = entry.get("active_substance")
    return {
        "product_name": name or "",
        "authorisation_no": product.authorisation_no if product else "",
        "authorisation_placeholder": bool(product and (product.props or {}).get("placeholder")),
        "active_substance": substance or "",
        "dose": str(q.value) if q else "",
        "dose_unit": q.unit if q else "",
    }


ProductItem = tuple[Quantity | None, dict[str, Any] | None]


def _product_items(log: Log, quantities: list[Quantity]) -> list[ProductItem]:
    """Product rates, including unmatched labelled rates, then props-only products."""
    entries: dict[str, Any] = (log.props or {}).get("products") or {}
    items: list[ProductItem] = [
        (q, entries.get(q.label) if q.label is not None else None)
        for q in quantities
        if q.measure == "rate" and (q.product_id is not None or (q.label and q.label.strip()))
    ]
    covered = {q.label for q, _ in items if q is not None}
    items += [(None, entry) for key, entry in entries.items() if key not in covered]
    return items or [(None, None)]


def product_summaries(
    repo: ScopedRepository, logs: list[Log], *, scope: str = SCOPE
) -> dict[int, list[dict[str, Any]]]:
    """Each log's products as the register cards read them (`product_name`, `dose`,
    `dose_unit`, the register's own cells), one per product name; a log with no named product
    gets an empty list. For the recent records and timelines, read under the caller's scope."""
    ids = [log.id for log in logs]
    quantities_of: dict[int, list[Quantity]] = defaultdict(list)
    if ids:
        for q in repo.list(Quantity, Quantity.log_id.in_(ids), scope=scope, order_by=Quantity.id):
            quantities_of[q.log_id].append(q)
    product_ids = {q.product_id for qs in quantities_of.values() for q in qs if q.product_id}
    products = (
        {p.id: p for p in repo.list(Product, Product.id.in_(product_ids), scope=scope)}
        if product_ids
        else {}
    )
    out: dict[int, list[dict[str, Any]]] = {}
    for log in logs:
        named: dict[str, dict[str, Any]] = {}
        for q, entry in _product_items(log, quantities_of[log.id]):
            product = products.get(q.product_id) if q and q.product_id else None
            cells = _product_cells(q, product, entry)
            if cells["product_name"]:
                named.setdefault(cells["product_name"], cells)
        out[log.id] = [
            {k: c[k] for k in ("product_name", "dose", "dose_unit")} for c in named.values()
        ]
    return out


def _applications(
    spray_logs: list[Log],
    parcels_of: dict[int, list[Parcel]],
    quantities_of: dict[int, list[Quantity]],
    products: dict[int, Product],
    zone: ZoneInfo,
) -> list[label_checks.Application]:
    """One application per log × parcel × known product, for the checks."""
    out = []
    for log in spray_logs:
        day = log.ts_start.astimezone(zone).date()
        for q in quantities_of[log.id]:
            if q.measure != "rate":
                continue
            product = products.get(q.product_id) if q.product_id is not None else None
            if product is None:
                continue
            label = label_checks.Label.from_props(product.props)
            dose = label_checks.dose_kg_ha(q.value, q.unit)
            for parcel in sorted(parcels_of[log.id], key=parcel_key):
                out.append(
                    label_checks.Application(
                        log_id=log.id,
                        ts=log.ts_start,
                        day=day,
                        parcel=parcel.alias,
                        product_id=product.id,
                        product_name=product.name,
                        substance=product.active_substance,
                        dose_kg_ha=dose,
                        label=label,
                    )
                )
    return out


def preview_checks(repo: ScopedRepository, pending: Log, now: datetime, *, tz=DEFAULT_TZ):
    """Read-only estimate including this pending spray; confirmation rechecks saved facts.

    Other drafts and future sprays never affect the preview. This does not confirm, run jobs,
    write weather, or change a quantity. A nearby merge is explicitly flagged by the caller.
    """
    history = repo.list(
        Log, Log.type == "spray", Log.status == "done", Log.ts_start <= now, scope=SCOPE
    )
    spray_logs = [*history, pending]
    ids = [log.id for log in spray_logs]
    links = repo.list(LogParcel, LogParcel.log_id.in_(ids), scope=SCOPE)
    parcels = {p.id: p for p in repo.list(Parcel, scope=SCOPE)}
    quantities = repo.list(Quantity, Quantity.log_id.in_(ids), scope=SCOPE)
    product_ids = {q.product_id for q in quantities if q.product_id is not None}
    products = {p.id: p for p in repo.list(Product, Product.id.in_(product_ids), scope=SCOPE)}
    parcels_of = defaultdict(list)
    quantities_of = defaultdict(list)
    for link in links:
        parcels_of[link.log_id].append(parcels[link.parcel_id])
    for q in quantities:
        quantities_of[q.log_id].append(q)
    apps = _applications(spray_logs, parcels_of, quantities_of, products, ZoneInfo(tz))
    return [check.as_dict() for check in label_checks.spray_checks(pending.id, apps)]


def parcel_aliases(
    repo: ScopedRepository, parcels: Iterable[Parcel], *, scope: str = SCOPE
) -> dict[int, list[tuple[str, str]]]:
    """parcel id -> its `ext_id` (source, key) pairs, sorted. `ext_id` has no farm column: the
    read is bounded by parcels the caller already read through the farm-scoped repository.
    DG AGRI aliases are omitted for parcels labelled synthetic (`props.id_label`)."""
    rows = {p.id: p for p in parcels}
    ids = sorted(rows)
    out: dict[int, list[tuple[str, str]]] = {i: [] for i in ids}
    if not ids:
        return out
    stmt = (
        select(ExtId)
        .where(ExtId.model == Parcel.__tablename__, ExtId.res_id.in_(ids))
        .order_by(ExtId.source, ExtId.key)
    )
    for ext in repo.execute(stmt, scope=scope, resource="ext_id").scalars():
        if ext.source == "dg-agri" and (rows[ext.res_id].props or {}).get("id_label"):
            continue
        out[ext.res_id].append((ext.source, ext.key))
    return out


def parcel_intervals(
    repo: ScopedRepository, logs: list[Log], parcels_of: dict[int, list[Parcel]]
) -> dict[tuple[int, int], tuple[datetime, datetime | None]]:
    """Interval per (log, parcel): highest-ranked explicitly covering source, else the log.

    Equal ranks keep the earliest receipt. Legacy payloads without top-level parcel_ids
    cannot bind an interval; saved-weather associations and partfield names are not inferred.
    Reads are farm-scoped/audited, and source payloads are never included in the export.
    """
    out = {(log.id, p.id): (log.ts_start, log.ts_end) for log in logs for p in parcels_of[log.id]}
    if not out:
        return out
    sources = repo.list(
        LogSource,
        LogSource.log_id.in_([log.id for log in logs]),
        scope=SCOPE,
        order_by=(LogSource.precision_rank.desc(), LogSource.id),
    )
    resolved: set[tuple[int, int]] = set()
    for source in sources:
        payload = source.raw_payload or {}
        if not isinstance(payload, dict) or not isinstance(payload.get("parcel_ids"), list):
            continue
        try:
            start = datetime.fromisoformat(payload.get("ts_start"))
            raw_end = payload.get("ts_end")
            end = datetime.fromisoformat(raw_end) if raw_end is not None else None
        except (TypeError, ValueError):
            continue
        if start.utcoffset() is None or (
            end is not None and (end.utcoffset() is None or end < start)
        ):
            continue
        for parcel_id in payload["parcel_ids"]:
            if type(parcel_id) is not int:
                continue
            key = (source.log_id, parcel_id)
            if key in out and key not in resolved:
                out[key] = (start, end)
                resolved.add(key)
    return out


def _saved_weather(repo: ScopedRepository, log_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    """Each log's projected snapshots, one per source that has one, in source id order. Each
    source is projected once, so a spray's rows share the same entry objects."""
    out: dict[int, list[dict[str, Any]]] = defaultdict(list)
    sources = repo.list(
        LogSource, LogSource.log_id.in_(log_ids), scope=SCOPE, order_by=LogSource.id
    )
    for source in sources:
        entry = spray_weather.project(source.source_type, source.raw_payload)
        if entry is not None:
            out[source.log_id].append(entry)
    return out


Window = tuple[datetime | None, datetime, datetime | None]


def season_window(season: int | None, now: datetime, tz: str) -> Window:
    """(since, until, before) for a season export: sprays with `since ≤ ts_start < before`
    (1 January of `season`, local, and of the next season), read up to `until` (the earlier of
    `before` and `now`); no season is (None, now, None)."""
    if season is None:
        return None, now, None
    zone = ZoneInfo(tz)
    since, before = datetime(season, 1, 1, tzinfo=zone), datetime(season + 1, 1, 1, tzinfo=zone)
    return since, min(before, now), before


SEASON_MIN, SEASON_MAX = 2000, 2100  # accepted `season` values for an export
PARCEL_ID_MAX = 2**63 - 1  # PostgreSQL bigint, the `parcel.id` type


def export_filter(
    repo: ScopedRepository, now: datetime, tz: str, *, season: int | None, parcel_id: int | None
) -> tuple[Window, Parcel | None]:
    """(`season_window`, parcel) for an export narrowed to `season` and `parcel_id`; the parcel is
    read through the farm-scoped repository (`register:read`), so another farm's parcel, or one
    that does not exist, raises `LookupError`."""
    window = season_window(season, now, tz)
    if parcel_id is None:
        return window, None
    parcel = repo.get(Parcel, parcel_id, scope=SCOPE)
    if parcel is None:
        raise LookupError(parcel_id)
    return window, parcel


def export_filename(stem: str, ext: str, season: int | None, parcel: Parcel | None) -> str:
    """`spray-register.csv`, `spray-register-2024.csv`, `spray-register-2024-P5.csv`, …"""
    parts = [stem, *([str(season)] if season is not None else [])]
    if parcel is not None:
        parts.append(re.sub(r"[^A-Za-z0-9_-]+", "_", parcel.alias))
    return f"{'-'.join(parts)}.{ext}"


def seasons(repo: ScopedRepository, now: datetime, tz: str) -> tuple[int, ...]:
    """Seasons with a done spray up to `now`, newest first; always the current one."""
    first = repo.list(
        Log,
        Log.type == "spray",
        Log.status == "done",
        Log.ts_start <= now,
        scope=SCOPE,
        order_by=Log.ts_start,
        limit=1,
    )
    zone = ZoneInfo(tz)
    current = now.astimezone(zone).year
    oldest = min(first[0].ts_start.astimezone(zone).year, current) if first else current
    return tuple(range(current, oldest - 1, -1))


def register_rows(
    repo: ScopedRepository,
    now: datetime,
    *,
    tz: str = DEFAULT_TZ,
    checks: bool = False,
    weather: bool = False,
    aliases: bool = False,
    since: datetime | None = None,
    before: datetime | None = None,
    parcel_id: int | None = None,
) -> list[dict[str, Any]]:
    """Rows for done sprays with `ts_start ≤ now`, ordered by time, log, parcel, product; with
    `checks`, each row also carries its spray's checks (`CSV_FIELDS`); with `weather`, its
    parcel's saved snapshots (`SAVED_WEATHER`); with `aliases`, its portable ids
    (`PARCEL_ALIASES`, last)."""
    spray_logs = repo.list(
        Log,
        Log.type == "spray",
        Log.status == "done",
        Log.ts_start <= now,
        scope=SCOPE,
        order_by=(Log.ts_start, Log.id),
    )
    if not spray_logs:
        return []
    log_ids = [log.id for log in spray_logs]
    links = repo.list(LogParcel, LogParcel.log_id.in_(log_ids), scope=SCOPE)
    parcels = {
        p.id: p
        for p in repo.list(Parcel, Parcel.id.in_({link.parcel_id for link in links}), scope=SCOPE)
    }
    quantities = repo.list(
        Quantity, Quantity.log_id.in_(log_ids), scope=SCOPE, order_by=Quantity.id
    )
    product_ids = {q.product_id for q in quantities if q.product_id is not None}
    products = (
        {p.id: p for p in repo.list(Product, Product.id.in_(product_ids), scope=SCOPE)}
        if product_ids
        else {}
    )

    parcels_of: dict[int, list[Parcel]] = defaultdict(list)
    for link in links:
        parcels_of[link.log_id].append(parcels[link.parcel_id])
    quantities_of: dict[int, list[Quantity]] = defaultdict(list)
    for q in quantities:
        quantities_of[q.log_id].append(q)

    zone = ZoneInfo(tz)
    checks_of: dict[int, list[label_checks.Check]] = (
        label_checks.checks_by_log(
            _applications(spray_logs, parcels_of, quantities_of, products, zone)
        )
        if checks
        else {}
    )
    weather_of = _saved_weather(repo, log_ids) if weather else {}
    aliases_of = parcel_aliases(repo, parcels.values()) if aliases else {}
    intervals = parcel_intervals(repo, spray_logs, parcels_of)
    rows = []
    for log in spray_logs:
        if (since is not None and log.ts_start < since) or (
            before is not None and log.ts_start >= before
        ):
            continue
        time_known = (log.props or {}).get("time_known") is not False
        items = _product_items(log, quantities_of[log.id])
        for parcel in sorted(parcels_of[log.id], key=parcel_key):
            if parcel_id is not None and parcel.id != parcel_id:
                continue
            local = intervals[log.id, parcel.id][0].astimezone(zone)
            props = parcel.props or {}
            crop_eppo = _crop_eppo(props)
            bbch, bbch_source = _bbch(
                log, local.date(), crop_eppo, configured=props.get("bbch_config", True)
            )
            for q, entry in items:
                product = products.get(q.product_id) if q and q.product_id else None
                cells = {
                    **_product_cells(q, product, entry),
                    "date": local.strftime("%Y-%m-%d"),
                    "start_time": local.strftime("%H:%M") if time_known else "",
                    "iacs_parcel_id": parcel.iacs_id or "",
                    "parcel_alias": parcel.alias,
                    "area_treated_ha": str(_area(log, parcel)),
                    "crop_name": props.get("crop_label", ""),
                    "crop_eppo": crop_eppo,
                    "bbch": bbch,
                    "bbch_source": bbch_source,
                    "log_id": log.id,
                }
                row = {name: cells[name] for name in FIELDS}
                if checks:
                    row[CHECKS] = [c.as_dict() for c in checks_of.get(log.id, [])]
                if weather:
                    row[SAVED_WEATHER] = spray_weather.for_parcel(
                        weather_of.get(log.id, []), parcel.id
                    )
                if aliases:
                    row[PARCEL_ALIASES] = [f"{s}:{k}" for s, k in aliases_of[parcel.id]]
                rows.append(row)
    return rows


def _csv_cell(value: Any) -> Any:
    if value is True:
        return "yes"
    if value is False or value is None:
        return ""
    return value


def to_csv(rows: list[dict[str, Any]], *, weather: bool = False, tz: str = DEFAULT_TZ) -> str:
    """CSV text with the `CSV_FIELDS` header; booleans as `yes` / blank, None as blank, the
    checks as one line of words (`checks.summary`); with `weather`, one more `saved_weather`
    column, every snapshot in words (`spray_weather.summary`), blank when none."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow((*CSV_FIELDS, SAVED_WEATHER) if weather else CSV_FIELDS)
    for row in rows:
        cells = [_csv_cell(row[name]) for name in FIELDS]
        cells.append(label_checks.summary(row.get(CHECKS) or []))
        if weather:
            cells.append(spray_weather.summary(row.get(SAVED_WEATHER) or [], tz))
        writer.writerow(cells)
    return buf.getvalue()


def weather_lines(rows: list[dict[str, Any]], tz: str = DEFAULT_TZ) -> list[str]:
    """One spray's saved snapshots in words for its card: each source's snapshot once across
    the spray's rows (they share each source's entry object), in row order (by parcel, then
    source id), labelled with the parcels whose rows carry it (the source's own parcels, never
    the whole merged spray's) and its own treatment time; an old record gives none."""
    seen: dict[int, tuple[dict[str, Any], list[str]]] = {}
    for row in rows:
        for entry in row.get(SAVED_WEATHER) or []:
            aliases = seen.setdefault(id(entry), (entry, []))[1]
            if row["parcel_alias"] not in aliases:
                aliases.append(row["parcel_alias"])
    return [
        spray_weather.describe(entry, tz, ", ".join(sorted(aliases, key=alias_key)))
        for entry, aliases in seen.values()
    ]

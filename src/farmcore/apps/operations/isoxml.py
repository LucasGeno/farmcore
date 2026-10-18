"""ISO 11783-10 TaskData (ISOXML) reader and import into operation logs.

Parsed with stdlib `xml.etree.ElementTree` (isoxml-py would pull numpy, shapely and xsdata for
the ~80 lines we need). Read: completed tasks `TSK` (`G="4"`), their partfield `PFD` designator,
`TIM` start/stop and `PDV` setpoints in the treatment zones. Doses: DDI `0006` mass per area
(mg/m² ÷ 100 = kg/ha), DDI `0001` volume per area (mm³/m² ÷ 100 = l/ha). Other DDIs and binary
`TLG` logs are ignored and listed in the task's `notes`.
"""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from farmcore.apps.operations import spray_weather
from farmcore.apps.operations.catalogue import products_for_farm
from farmcore.core import logs
from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Log, LogSource, Parcel, Product

SCOPE = "logs:write"
PARCEL_SCOPE = "fields:read"  # parcel names and aliases belong to the fields app
SOURCE_TYPE = "isoxml"
SOURCE_APP = "operations"
# Product-name words that name an active substance (French label words → shortlist substance).
# "Bouillie bordelaise" (Bordeaux mixture) is a copper product whatever its brand suffix.
SUBSTANCE_WORDS = {
    "cuivre": "copper",
    "bordelaise": "copper",
    "soufre": "sulphur",
    "sulfur": "sulphur",
}
ROOT_TAG = "ISO11783_TaskData"
TASK_COMPLETED = "4"
XML_NAME = "TASKDATA.XML"
ZIP_MAGIC = b"PK\x03\x04"
# Cap on the TASKDATA.XML expanded from a zip (the route caps only the upload): no zip bomb.
MAX_XML_BYTES = 50_000_000
# DDI (hex, ISO 11783-11) -> (unit after conversion, divisor from the ISO unit).
DOSE_DDIS: dict[str, tuple[str, Decimal]] = {
    "0001": ("l/ha", Decimal(100)),  # setpoint volume per area, mm³/m²
    "0006": ("kg/ha", Decimal(100)),  # setpoint mass per area, mg/m²
}


class IsoxmlError(ValueError):
    """The file is not a TaskData file we can import (plain message for the farmer)."""


@dataclass(frozen=True)
class ProductDose:
    product_ref: str | None
    name: str | None
    value: Decimal
    unit: str
    ddi: str
    raw_value: int


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    partfield: str
    ts_start: datetime
    ts_end: datetime | None
    products: tuple[ProductDose, ...]
    attributes: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    # The file's device designators (`DVC B`), in file order. A task names no device (no DAN
    # here), so this is what the file lists, not the device that ran the task.
    file_devices: tuple[str, ...] = ()

    def payload(self) -> dict[str, Any]:
        """The raw task as stored in `log_source.raw_payload`: attributes, doses and the file's
        device designators."""
        return {
            "task": dict(self.attributes),
            "partfield": self.partfield,
            "ts_start": self.ts_start.isoformat(),
            "ts_end": self.ts_end.isoformat() if self.ts_end else None,
            "doses": [
                {
                    "ddi": p.ddi,
                    "raw_value": p.raw_value,
                    "product_ref": p.product_ref,
                    "product": p.name,
                    "value": str(p.value),
                    "unit": p.unit,
                }
                for p in self.products
            ],
            "notes": list(self.notes),
            "file_devices": list(self.file_devices),
        }


def _time(task_id: str, value: str, tz: str) -> datetime:
    try:
        t = datetime.fromisoformat(value)
    except ValueError:
        raise IsoxmlError(f"task {task_id} has an unreadable time {value!r}") from None
    if t.tzinfo is None:  # ISOXML times without an offset are local to the farm
        t = t.replace(tzinfo=ZoneInfo(tz))
    return t.astimezone(UTC)


def _doses(
    task: ET.Element, task_id: str, products: dict[str, str]
) -> tuple[tuple[ProductDose, ...], tuple[str, ...]]:
    """Setpoint doses per product (the first treatment zone that names it), plus notes."""
    doses: dict[str | None, ProductDose] = {}
    notes: list[str] = []
    for pdv in task.iter("PDV"):
        ddi = pdv.get("A", "").upper()
        if ddi not in DOSE_DDIS:
            notes.append(f"DDI {ddi or '?'} ignored (only 0001 and 0006 doses are read)")
            continue
        unit, divisor = DOSE_DDIS[ddi]
        try:
            raw = int(pdv.get("B", ""))
        except ValueError:
            raise IsoxmlError(f"task {task_id} has an unreadable dose {pdv.get('B')!r}") from None
        ref = pdv.get("C")
        if ref in doses:
            notes.append(f"product {ref}: later treatment zones ignored (first zone's dose kept)")
            continue
        doses[ref] = ProductDose(
            ref, products.get(ref or ""), Decimal(raw) / divisor, unit, ddi, raw
        )
    for tlg in task.iter("TLG"):
        notes.append(f"binary log {tlg.get('A', '?')} ignored (TLG logs are not read)")
    return tuple(doses.values()), tuple(dict.fromkeys(notes))


def _task(
    task: ET.Element, partfields: dict[str, str], products: dict[str, str], tz: str
) -> TaskRecord:
    task_id = task.get("A", "?")
    starts = [_time(task_id, t.get("A", ""), tz) for t in task.iter("TIM") if t.get("A")]
    stops = [_time(task_id, t.get("B", ""), tz) for t in task.iter("TIM") if t.get("B")]
    if not starts:
        raise IsoxmlError(f"task {task_id} has no start time (no TIM with a start)")
    partfield = partfields.get(task.get("E", ""), "").strip()
    if not partfield:
        raise IsoxmlError(f"task {task_id} names no partfield (TSK E → a PFD with a designator)")
    doses, notes = _doses(task, task_id, products)
    return TaskRecord(
        task_id=task_id,
        partfield=partfield,
        ts_start=min(starts),
        ts_end=max(stops) if stops else None,
        products=doses,
        attributes=dict(task.attrib),
        notes=notes,
    )


def _xml_bytes(data: bytes) -> bytes:
    """The TASKDATA.XML bytes: the file itself, or read from a zip (`TASKDATA/TASKDATA.XML`)."""
    if not data.startswith(ZIP_MAGIC):
        return data
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = [n for n in zf.namelist() if n.upper().rsplit("/", 1)[-1] == XML_NAME]
            if not names:
                raise IsoxmlError(f"the zip holds no {XML_NAME} (expected TASKDATA/{XML_NAME})")
            preferred = [n for n in names if n.upper() == f"TASKDATA/{XML_NAME}"]
            name = (preferred or sorted(names, key=len))[0]
            too_big = f"the {XML_NAME} in the zip is larger than {MAX_XML_BYTES // 1_000_000} MB"
            if zf.getinfo(name).file_size > MAX_XML_BYTES:
                raise IsoxmlError(too_big)
            with zf.open(name) as member:  # the header size can lie: read at most the cap
                xml = member.read(MAX_XML_BYTES + 1)
            if len(xml) > MAX_XML_BYTES:
                raise IsoxmlError(too_big)
            return xml
    except zipfile.BadZipFile:
        raise IsoxmlError("the file is not a readable zip") from None


def parse(data: bytes, *, tz: str = "Europe/Paris") -> list[TaskRecord]:
    """The completed tasks of a TaskData file (bare XML or zip), in file order. Times without an
    offset are local to `tz`. Raises `IsoxmlError` with a plain message."""
    try:
        root = ET.fromstring(_xml_bytes(data))
    except ET.ParseError as exc:
        raise IsoxmlError(f"the file is not valid XML ({exc})") from None
    if root.tag != ROOT_TAG:
        raise IsoxmlError(f"the file is not an ISO 11783 TaskData file (root {root.tag!r})")
    partfields = {p.get("A", ""): p.get("C", "") for p in root.iter("PFD")}
    products = {p.get("A", ""): p.get("B", "") for p in root.iter("PDT")}
    every = list(root.iter("TSK"))
    if not every:
        raise IsoxmlError("the file has no task (no TSK element)")
    tasks = [t for t in every if t.get("G") == TASK_COMPLETED]
    if not tasks:
        raise IsoxmlError('the file has no completed task (TSK with G="4")')
    devices = tuple(
        dict.fromkeys(d.get("B", "").strip() for d in root.iter("DVC") if d.get("B", "").strip())
    )
    return [replace(_task(t, partfields, products, tz), file_devices=devices) for t in tasks]


# --- import ---


@dataclass(frozen=True)
class TaskImport:
    task_id: str
    partfield: str
    parcel_id: int
    log_id: int
    log_name: str
    merged: bool  # into an existing log (else a new log)
    replaced: bool  # the log's time and dose now come from this machine record
    unchanged: bool  # this task was imported before: nothing written
    badges: tuple[str, ...]  # the log's source types, in order


@dataclass(frozen=True)
class ImportResult:
    tasks: tuple[TaskImport, ...]
    notes: tuple[str, ...]


def _fold(text: str) -> str:
    """Case- and accent-insensitive key: `Château` == `chateau`."""
    decomposed = unicodedata.normalize("NFKD", text.strip().casefold())
    return " ".join("".join(c for c in decomposed if not unicodedata.combining(c)).split())


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", _fold(text)))


def _parcel_map(repo: ScopedRepository, tasks: list[TaskRecord]) -> dict[str, Parcel]:
    """Partfield designator -> the farm's parcel (by name, then alias); all must map."""
    parcels = repo.list(Parcel, scope=PARCEL_SCOPE, order_by=Parcel.id)
    by_name = {_fold(p.name): p for p in parcels}
    by_alias = {_fold(p.alias): p for p in parcels}
    out, unknown = {}, []
    for t in tasks:
        key = _fold(t.partfield)
        parcel = by_name.get(key) or by_alias.get(key)
        if parcel is None:
            unknown.append(t.partfield)
        else:
            out[t.partfield] = parcel
    if unknown:
        names = ", ".join(f"{p.alias} {p.name}" for p in parcels) or "none"
        raise IsoxmlError(
            f"unknown partfield(s) {', '.join(dict.fromkeys(unknown))}: "
            f"no parcel of this farm has that name or alias (parcels: {names})"
        )
    return out


def _product(products: list[Product], name: str | None) -> Product | None:
    """The shortlist product for a TaskData product name: exact name, then active substance."""
    if not name:
        return None
    for p in products:
        if _fold(p.name) == _fold(name):
            return p
    words = _words(name)
    substances = {SUBSTANCE_WORDS.get(w, w) for w in words}
    matches = [p for p in products if _fold(p.active_substance) in substances]
    return matches[0] if len(matches) == 1 else None


def _quantities(products: list[Product], task: TaskRecord) -> list[logs.QuantityInput]:
    out = []
    for dose in task.products:
        product = _product(products, dose.name)
        label = product.name if product else (dose.name or f"product {dose.product_ref or '?'}")
        out.append(
            logs.QuantityInput(
                measure="rate",
                value=dose.value,
                unit=dose.unit,
                product_id=product.id if product else None,
                label=label,
            )
        )
    return out


def _check_clock(tasks: list[TaskRecord], now: datetime, tz: str) -> None:
    late = [t for t in tasks if t.ts_start > now]
    if late:
        last = max(t.ts_start for t in late).astimezone(ZoneInfo(tz))
        raise IsoxmlError(
            "machine record later than the replay clock — step the clock past "
            f"{last:%H:%M} on {last:%-d %b %Y} first"
        )


def _sources_of(repo: ScopedRepository, log_id: int) -> list[LogSource]:
    return repo.list(LogSource, LogSource.log_id == log_id, scope=SCOPE, order_by=LogSource.id)


def import_taskdata(
    repo: ScopedRepository,
    data: bytes,
    *,
    now: datetime,
    actor: str,
    tz: str = "Europe/Paris",
    weather_reader: spray_weather.Reader | None = None,
) -> ImportResult:
    """Import every completed task of a TaskData file as a done spray through
    `logs.record_operation` (merging with a matching voice log; machine rank wins time and dose).
    Each new source carries its resolved parcel_ids for per-parcel export intervals; held
    receipts are not backfilled. All-or-nothing: every partfield must map to a parcel and no
    task may start after `now` before anything is written. Re-importing the same bytes changes
    nothing. A new receipt gets
    its saved station weather (`spray_weather`) in this transaction; `weather_reader` is the
    composed optional reader (None: saved as unavailable)."""
    repo.check(SCOPE)
    tasks = parse(data, tz=tz)
    logs.lock_farm(repo.session, repo.farm_id)
    parcels = _parcel_map(repo, tasks)
    _check_clock(tasks, now, tz)
    products = products_for_farm(repo, scope=SCOPE, order_by=Product.id)
    digest = hashlib.sha256(data).hexdigest()[:12]
    out = []
    for task in tasks:
        key = f"{digest}:{task.task_id}"
        parcel = parcels[task.partfield]
        held = repo.list(
            LogSource,
            LogSource.source_type == SOURCE_TYPE,
            LogSource.external_key == key,
            scope=SCOPE,
            limit=1,
        )
        unchanged = bool(held)
        if held:  # imported before: write nothing (record_operation would no-op too)
            # A confirmation may have moved this receipt since the caller cached it.
            repo.session.refresh(held[0])
            log = repo.session.get(Log, held[0].log_id, populate_existing=True)
            assert log is not None
        else:
            local_start = task.ts_start.astimezone(ZoneInfo(tz))
            log, merged = logs.record_operation(
                repo.session,
                farm_id=repo.farm_id,
                type="spray",
                status="done",
                parcel_ids=[parcel.id],
                ts_start=task.ts_start,
                ts_end=task.ts_end,
                quantities=_quantities(products, task),
                source=logs.SourceInput(
                    SOURCE_TYPE,
                    external_key=key,
                    raw_payload={**task.payload(), "parcel_ids": [parcel.id]},
                    confidence=1.0,
                    precision_rank=logs.RANK_MACHINE,
                ),
                actor=actor,
                occurred_at=now,
                not_after=now,
                name=f"Machine spray {local_start:%-d %b %Y} ({parcel.name})",
                source_app=SOURCE_APP,
            )
        sources = _sources_of(repo, log.id)
        others = [s for s in sources if (s.source_type, s.external_key) != (SOURCE_TYPE, key)]
        if not unchanged:  # a new receipt only: its own time and parcel (saved weather)
            mine = [s for s in sources if (s.source_type, s.external_key) == (SOURCE_TYPE, key)]
            spray_weather.capture(
                repo,
                [spray_weather.Receipt(s.id, task.ts_start, (parcel.id,)) for s in mine],
                reader=weather_reader,
                data_now=now,
                captured_at=datetime.now(UTC),
            )
        if unchanged:
            merged, replaced = bool(others), False
        else:
            replaced = (
                merged and max((s.precision_rank for s in others), default=0) < logs.RANK_MACHINE
            )
        badges = tuple(dict.fromkeys(s.source_type for s in sources))
        out.append(
            TaskImport(
                task_id=task.task_id,
                partfield=task.partfield,
                parcel_id=parcel.id,
                log_id=log.id,
                log_name=log.name,
                merged=merged,
                replaced=replaced,
                unchanged=unchanged,
                badges=badges,
            )
        )
    notes = tuple(dict.fromkeys(n for t in tasks for n in t.notes))
    return ImportResult(tuple(out), notes)

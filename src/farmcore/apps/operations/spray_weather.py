"""Saved spray weather: station facts recorded on a confirmed spray's own source receipt.

- Stored per original `LogSource` at `raw_payload["farmcore.spray_weather"]` (`v` 1), by
  whole-dictionary reassignment; every original raw field is kept. Presence of the key, even an
  unavailable record, means captured: never recomputed, overwritten or backfilled.
- Captured only for a genuinely new confirmed source (voice ✅, ISOXML new receipt), in the
  caller's transaction, with the receipt's own treatment time and parcel ids (never a merged
  log's later canonical time or parcel union).
- Selection `exact_previous_hour_slot_v1`: the UTC hour slot at or before the source time, no
  older carry-forward. The station's hourly cadence, not an agronomic rule. Rain is the hour
  ending at the slot, (slot − 1 h, slot].
- Weather absent (no reader) or switched off (live `app_module` row read here, in the caller's
  transaction), no station, no slot, unknown time: an explicit unavailable record, no zeros.
  Real database errors propagate, so the caller's whole write rolls back.
- `project` / `summary` give the owner register a whitelisted view; nothing else of the raw
  payload, no transcript, chat id or source key.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import LogSource
from farmcore.core.models.kernel import AppModule

NAMESPACE = "farmcore.spray_weather"
VERSION = 1
POLICY = "exact_previous_hour_slot_v1"
STATIONS = "stations"
UNINSTALLED = "uninstalled"  # registry.UNINSTALLED; no row = installed (registry.read_states)
SCOPE = "logs:write"

FACTS = ("t_c", "ff_kmh", "rr1_mm")
UNITS = {"t_c": "°C", "ff_kmh": "km/h", "rr1_mm": "mm"}
LABELS = {"t_c": "temperature", "ff_kmh": "wind", "rr1_mm": "rain (hour ending)"}

CAPTURED, PARTIAL, UNAVAILABLE = "captured", "partial", "unavailable"
NO_CAPABILITY = "weather_unavailable"  # Weather app absent or switched off
NO_STATION = "no_station"
NO_SLOT = "no_reading_at_slot"
NO_TIME = "source_time_unknown"
REASON_WORDS = {
    NO_CAPABILITY: "Weather app unavailable at capture",
    NO_STATION: "no weather station",
    NO_SLOT: "no station reading for that hour",
    NO_TIME: "treatment time unknown",
}
SOURCE_WORDS = {"voice": "Voice", "isoxml": "Machine file", "file": "File", "seed": "Seed"}

Reader = Callable[[ScopedRepository, datetime], dict[str, Any]]


@dataclass(frozen=True)
class Receipt:
    """One source as it was received: its own treatment time and parcels."""

    source_id: int
    treated_at: datetime | None
    parcel_ids: tuple[int, ...]


def slot_for(treated_at: datetime) -> datetime:
    """The UTC hour slot at or before `treated_at` (`exact_previous_hour_slot_v1`)."""
    utc = treated_at.astimezone(ZoneInfo("UTC"))
    return utc.replace(minute=0, second=0, microsecond=0)


def weather_on(repo: ScopedRepository) -> bool:
    """The stations app's current state, a fresh scalar read in the caller's transaction."""
    state = repo.session.scalar(select(AppModule.state).where(AppModule.name == STATIONS))
    return state != UNINSTALLED


def capture(
    repo: ScopedRepository,
    receipts: Sequence[Receipt],
    *,
    reader: Reader | None,
    data_now: datetime,
    captured_at: datetime,
) -> None:
    """Save the evidence on each receipt's source that has none yet; commits nothing."""
    if not receipts:
        return
    ids = [r.source_id for r in receipts]
    sources = {s.id: s for s in repo.list(LogSource, LogSource.id.in_(ids), scope=SCOPE)}
    pending = [r for r in receipts if r.source_id in sources]
    pending = [r for r in pending if NAMESPACE not in (sources[r.source_id].raw_payload or {})]
    if not pending:
        return
    usable = reader if reader is not None and weather_on(repo) else None
    for receipt in pending:
        evidence = _evidence(repo, receipt, usable, data_now, captured_at)
        source = sources[receipt.source_id]
        source.raw_payload = {**(source.raw_payload or {}), NAMESPACE: evidence}
    repo.session.flush()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _evidence(
    repo: ScopedRepository,
    receipt: Receipt,
    reader: Reader | None,
    data_now: datetime,
    captured_at: datetime,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "v": VERSION,
        "policy": POLICY,
        "status": UNAVAILABLE,
        "reason": None,
        "source_time": _iso(receipt.treated_at),
        "parcel_ids": list(receipt.parcel_ids),
        "slot": None,
        "station": None,
        "observed_at": None,
        "rain_interval": None,
        "values": dict.fromkeys(FACTS),
        "units": dict(UNITS),
        "captured_at": _iso(captured_at),
        "data_clock": _iso(data_now),
    }
    if receipt.treated_at is None:
        return {**out, "reason": NO_TIME}
    slot = slot_for(receipt.treated_at)
    out["slot"] = _iso(slot)
    if reader is None:
        return {**out, "reason": NO_CAPABILITY}
    read = reader(repo, slot)
    station = read.get("station")
    if not station:
        return {**out, "reason": NO_STATION}
    out["station"] = _station(station)
    reading = read.get("reading")
    if slot > data_now or not reading or reading.get("ts") != slot:
        return {**out, "reason": NO_SLOT}
    values = {name: _number(reading.get(name)) for name in FACTS}
    known = [v for v in values.values() if v is not None]
    if not known:
        return {**out, "reason": NO_SLOT}
    out.update(
        status=CAPTURED if len(known) == len(FACTS) else PARTIAL,
        observed_at=_iso(slot),
        values=values,
        rain_interval=[_iso(slot - timedelta(hours=1)), _iso(slot)]
        if values["rr1_mm"] is not None
        else None,
    )
    return out


def _number(value: Any) -> float | None:
    return None if value is None else float(value)


# --- owner projection (register) -----------------------------------------------------------

_STATION_KEYS = ("id", "name")
_PROVIDER_KEYS = ("source", "key", "name", "licence")  # public reference; never a file path


def _station(station: Any) -> dict[str, Any] | None:
    """The whitelisted station identity: local id and name, and the public provider reference."""
    if not isinstance(station, dict):
        return None
    out = {k: station.get(k) for k in _STATION_KEYS}
    ref = station.get("provider")
    out["provider"] = {k: ref.get(k) for k in _PROVIDER_KEYS} if isinstance(ref, dict) else None
    return out


UNSUPPORTED = "unsupported"
STATUSES = (CAPTURED, PARTIAL, UNAVAILABLE)


def _when(value: Any) -> datetime | None:
    """A stored aware ISO time, else None (malformed or naive)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _fact_value(value: Any) -> tuple[bool, float | None]:
    """`(valid, value)`: None, or a finite real number (never a bool or a string)."""
    if value is None:
        return True, None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False, None
    try:  # a huge stored JSON integer does not fit a float: not a reading
        number = float(value)
    except OverflowError:
        return False, None
    return math.isfinite(number), number


def _identity(station: Any) -> bool:
    """None, or a station identity of plain values: int id, str name, a provider of strings."""
    if station is None:
        return True
    if not isinstance(station, dict):
        return False
    if type(station.get("id")) is not int or not isinstance(station.get("name"), str):
        return False
    ref = station.get("provider")
    if ref is None:
        return True
    return isinstance(ref, dict) and all(
        ref.get(k) is None or isinstance(ref.get(k), str) for k in _PROVIDER_KEYS
    )


def _valid(saved: dict[str, Any]) -> bool:
    """A v1 record exactly as `capture` writes it; anything else is not trusted."""
    v = saved.get("v")
    if type(v) is not int or v != VERSION or saved.get("status") not in STATUSES:
        return False
    ids = saved.get("parcel_ids")
    if not isinstance(ids, list) or not all(type(i) is int for i in ids):
        return False
    for key in ("source_time", "slot", "observed_at", "captured_at", "data_clock"):
        if saved.get(key) is not None and _when(saved[key]) is None:
            return False
    values = saved.get("values")
    if not isinstance(values, dict) or not _identity(saved.get("station")):
        return False
    checked = [_fact_value(values.get(name)) for name in FACTS]
    if not all(ok for ok, _ in checked):
        return False
    known = [value for _, value in checked if value is not None]
    status = saved["status"]
    if status == UNAVAILABLE:
        reason = saved.get("reason")
        return isinstance(reason, str) and reason in REASON_WORDS
    if saved.get("reason") is not None or _when(saved.get("observed_at")) is None:
        return False
    if (status == CAPTURED) != (len(known) == len(FACTS)) or not known:
        return False
    interval = saved.get("rain_interval")
    if values.get("rr1_mm") is None:
        return interval is None
    observed = _when(saved["observed_at"])
    return (
        isinstance(interval, list)
        and len(interval) == 2
        and [_when(t) for t in interval] == [observed - timedelta(hours=1), observed]
    )


def project(source_type: str, payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """The whitelisted view of one source's saved evidence, or None when it has none. A record
    that is not an exact v1 (unknown version, malformed field) is shown as unsupported, with no
    values: never trusted, re-read or recomputed."""
    payload = payload or {}
    if NAMESPACE not in payload:
        return None
    saved = payload[NAMESPACE]
    base = {"source": source_type, "source_label": SOURCE_WORDS.get(source_type, source_type)}
    if not isinstance(saved, dict) or not _valid(saved):
        v = saved.get("v") if isinstance(saved, dict) else None
        return {**base, "v": v if type(v) is int else None, "status": UNSUPPORTED}
    station = saved.get("station")
    values = saved["values"]
    unavailable = saved["status"] == UNAVAILABLE
    return {
        **base,
        "v": VERSION,
        "policy": saved.get("policy") if saved.get("policy") == POLICY else None,
        "status": saved["status"],
        "reason": saved.get("reason") if unavailable else None,
        "source_time": saved.get("source_time"),
        "parcel_ids": list(saved["parcel_ids"]),
        "slot": saved.get("slot"),
        "station": _station(station),
        "observed_at": None if unavailable else saved["observed_at"],
        "rain_interval": None if unavailable else saved.get("rain_interval"),
        "values": {
            name: None if unavailable else _fact_value(values.get(name))[1] for name in FACTS
        },
        "units": {name: UNITS[name] for name in FACTS},
        "captured_at": saved.get("captured_at"),
        "data_clock": saved.get("data_clock"),
    }


def for_parcel(entries: Iterable[dict[str, Any]], parcel_id: int) -> list[dict[str, Any]]:
    """The snapshots that apply to one parcel: only a source's own original parcels."""
    # An unsupported record's parcels are not trusted: it shows on every row, never hidden.
    return [e for e in entries if e.get("status") == UNSUPPORTED or parcel_id in e["parcel_ids"]]


def _local(iso: str | None, zone: ZoneInfo) -> str:
    if not iso:
        return "time unknown"
    return datetime.fromisoformat(iso).astimezone(zone).strftime("%d %b %H:%M")


def _hour(iso: str, zone: ZoneInfo) -> str:
    return datetime.fromisoformat(iso).astimezone(zone).strftime("%H:%M")


def _fact(name: str, value: float | None, entry: dict[str, Any], zone: ZoneInfo) -> str:
    if value is None:
        return f"{LABELS[name]} missing"
    if name == "rr1_mm":  # the hour ending at the reading, with its start and end
        start, end = entry["rain_interval"]
        return f"rain {value:g} mm {_hour(start, zone)}–{_hour(end, zone)}"
    return f"{value:g} {UNITS[name]}" if name == "t_c" else f"wind {value:g} km/h"


def describe(entry: dict[str, Any], tz: str, parcels: str | None = None) -> str:
    """One snapshot in words: its source, the source's own treatment time (which can differ
    from the merged record's time) and, on a card, the source's own parcels ("Machine file ·
    P1 · treated …"), so a merged spray never reads one parcel's weather as another's. The
    station reading is the one saved with the record, not today's weather."""
    zone = ZoneInfo(tz)
    label = entry["source_label"]
    if entry.get("status") == UNSUPPORTED:
        version = f" (version {entry['v']})" if entry.get("v") is not None else ""
        return f"{label}: saved weather in an unsupported format{version}"
    when = _local(entry.get("source_time"), zone)
    head = f"{label} · {parcels} · treated {when}" if parcels else f"{label} · treated {when}"
    if entry.get("status") == UNAVAILABLE:
        return f"{head}: no saved weather ({REASON_WORDS.get(entry.get('reason'), 'unavailable')})"
    station = (entry.get("station") or {}).get("name") or "station"
    facts = ", ".join(_fact(n, entry["values"].get(n), entry, zone) for n in FACTS)
    reading = _local(entry.get("observed_at"), zone)
    return f"{head}: {station} reading {reading}, saved with the record: {facts}"


def summary(entries: Sequence[dict[str, Any]], tz: str) -> str:
    """Every snapshot, in the given order, joined for one CSV cell; blank when none."""
    return " | ".join(describe(e, tz) for e in entries)

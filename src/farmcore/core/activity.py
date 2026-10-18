"""Recent activity: the farm's latest things that happened, read from `event`, for Today
(`recent_activity`) and the Activity app's page (`activity_rows`: the same rows plus raw fields).

Reads go through the request's `ScopedRepository` (`logs:read`, farm-scoped), newest first by
`occurred_at`, bounded by clock now (replay-aware). The core imports no app: it knows the event
kinds the apps post (`core/logs.py`: `log.*`; advisor: `advice.*`; share: `share.*`) and
links by path, and only to apps that are loaded.

Grouping (one row per thing the farmer did, not per parcel event):

- `log.*`: `core/logs.py` posts one event per parcel of the log, all with the same kind, actor,
  `occurred_at` and payload. Batch writers (the scene B seed, an ISOXML import, a DG AGRI import)
  write one log per parcel at the same instant. Key: (kind, actor, occurred_at, log type,
  status, source types), so a 10-parcel spray is one row either way. Two logs the same actor
  wrote at the same clock instant read as one row ("Spray confirmed on P5, P6"), which is true.
  A pending `log.created` whose log is confirmed, merged or cancelled in the fetched events is
  dropped: the later row says what happened.
- `advice.changed` only (an hourly `advice.evaluated` that changed nothing is noise). Key:
  (kind, occurred_at, new decision): one row per decision an evaluation moved parcels to.
- `share.*` and anything else: one row per event (`body` is already a sentence).

Kinds are kept only when the event's `source_app` is loaded and switched on (`core` always);
`log.*` (spray records) always shows, whichever app took the record down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_

from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import Event, LogSource, Parcel, Product, Quantity

SCOPE = "logs:read"
PARCEL_SCOPE = "fields:read"
LIMIT = 5
PAGE = 50  # rows per Activity page
FETCH = 200  # events read to fill LIMIT rows after grouping (advice.evaluated is skipped in SQL)
NAMED_PARCELS = 3  # more than this: "on 10 parcels"
HIDDEN_KINDS = ("advice.evaluated",)
SOURCES = {
    "voice": ("mic", "Voice note", "a voice note"),
    "isoxml": ("tractor", "Machine file", "the machine file"),
    "file": ("register", "File", "a file"),
    "seed": ("leaf", "Demo data", "demo data"),
}
DECISION_WORDS = {"spray": "Spray", "wait": "Wait", "protected": "Protected", "none": "No advice"}
REGISTER = "/operations/register"
DIARY = "/operations/"
ACTIVITY = "/activity/"
ADVISOR = "/advisor/"
SHARING = "/share/"
ActivityType = Literal["all", "spray", "advice", "sharing", "demo"]
Cursor = tuple[datetime, int]  # a group's newest event: (occurred_at, id), the query's order


@dataclass
class _Group:
    kind: str
    first: Event  # the newest event of the group
    parcel_ids: list[int] = field(default_factory=list)
    log_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True)
class ActivityFilters:
    """Select whole actions after resolving drafts; period is [since, until)."""

    parcel_id: int | None = None
    kind: ActivityType = "all"
    since: datetime | None = None
    until: datetime | None = None

    def matches(self, group: _Group) -> bool:
        if self.parcel_id is not None and self.parcel_id not in group.parcel_ids:
            return False
        event = group.first
        if self.since is not None and event.occurred_at < self.since:
            return False
        if self.until is not None and event.occurred_at >= self.until:
            return False
        if self.kind == "spray":
            return event.kind.startswith("log.") and (event.payload or {}).get("type") == "spray"
        prefix = {"advice": "advice.", "sharing": "share.", "demo": "demo."}.get(self.kind)
        return prefix is None or event.kind.startswith(prefix)


def _key(e: Event) -> tuple[Any, ...]:
    p = e.payload or {}
    if e.kind.startswith("log."):
        sources = tuple(p.get("source_types", []))
        return (e.kind, e.actor, e.occurred_at, p.get("type"), p.get("status"), sources)
    if e.kind == "advice.changed":
        return (e.kind, e.occurred_at, p.get("new"))
    return (e.kind, e.id)


def _alias_key(alias: str) -> tuple[str, int, str]:
    m = re.match(r"^(\D*)(\d+)(.*)$", alias)
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (alias, 0, "")


def _where(aliases: list[str]) -> str:
    if not aliases:
        return "a parcel"
    if len(aliases) > NAMED_PARCELS:
        return f"{len(aliases)} parcels"
    return ", ".join(aliases)


def when(at: datetime, now: datetime, tz: str) -> str:
    """`Today · 07:40`, `Yesterday · 07:40`, else `Wed 10 Apr · 07:40` (display tz)."""
    zone = ZoneInfo(tz)
    local, today = at.astimezone(zone), now.astimezone(zone).date()
    time = local.strftime("%H:%M")
    if local.date() == today:
        return f"Today · {time}"
    if local.date() == today - timedelta(days=1):
        return f"Yesterday · {time}"
    return f"{local.strftime('%a %-d %b')} · {time}"


def _dose(value: Any) -> str:
    text = format(value, "f") if hasattr(value, "as_tuple") else str(value)
    return text.rstrip("0").rstrip(".") if "." in text else text


def _products(repo: ScopedRepository, log_ids: list[int]) -> dict[int, list[str]]:
    """log id -> `copper 2 kg/ha` per quantity that names a product (by its active substance)."""
    out: dict[int, list[str]] = {}
    if not log_ids:
        return out
    quantities = repo.list(
        Quantity, Quantity.log_id.in_(log_ids), scope=SCOPE, order_by=Quantity.id
    )
    ids = {q.product_id for q in quantities if q.product_id is not None}
    names = {}
    if ids:
        for prod in repo.list(Product, Product.id.in_(ids), scope=SCOPE):
            names[prod.id] = prod.active_substance or prod.name
    for q in quantities:
        name = names.get(q.product_id) if q.product_id is not None else q.label
        if not name or q.measure != "rate":
            continue
        out.setdefault(q.log_id, []).append(f"{name} {_dose(q.value)} {q.unit}")
    return out


def _devices(repo: ScopedRepository, log_ids: list[int]) -> dict[int, list[str]]:
    """log id -> the device designators its machine files list (`raw_payload["file_devices"]`,
    written by ISOXML imports; older receipts have none)."""
    out: dict[int, list[str]] = {}
    if not log_ids:
        return out
    sources = repo.list(
        LogSource, LogSource.log_id.in_(log_ids), scope=SCOPE, order_by=LogSource.id
    )
    for s in sources:
        names = (s.raw_payload or {}).get("file_devices") or []
        out.setdefault(s.log_id, []).extend(str(n) for n in names if n)
    return out


def _log_row(g: _Group, where: str, products: list[str]) -> dict[str, Any]:
    p = g.first.payload or {}
    what = str(p.get("type") or "record").capitalize()
    sources = [s for s in p.get("source_types", []) if s in SOURCES]
    link = REGISTER
    if g.kind == "log.created" and p.get("status") == "pending":
        text, link = f"{what} noted on {where}, waiting for confirmation", DIARY
    elif g.kind == "log.created":
        text = f"{what} logged on {where}"
    elif g.kind == "log.confirmed" or (g.kind == "log.merged" and p.get("confirmed_log_id")):
        text = f"{what} confirmed on {where}"
    elif g.kind == "log.merged":
        source = SOURCES[sources[0]][2] if sources else "another record"
        text = f"{what} on {where} updated from {source}"
    elif g.kind == "log.cancelled":
        text, link = f"{what} draft on {where} cancelled", DIARY
    else:
        text = g.first.body
    icon = SOURCES[sources[0]][0] if sources else "spray"
    return {
        "kind": g.kind,
        "icon": icon,
        "text": text,
        "detail": " + ".join(dict.fromkeys(products)),
        "sources": [{"icon": SOURCES[s][0], "label": SOURCES[s][1]} for s in sources],
        "link": link,
    }


def _resolved(events: list[Event]) -> set[int]:
    """Pending log ids that a fetched event confirms, merges or cancels."""
    done = set()
    for e in events:
        p = e.payload or {}
        if e.kind in ("log.confirmed", "log.cancelled") and p.get("log_id") is not None:
            done.add(p["log_id"])
        if e.kind == "log.merged" and p.get("confirmed_log_id") is not None:
            done.add(p["confirmed_log_id"])
    return done


def recent_activity(
    repo: ScopedRepository, now: datetime, tz: str, apps: set[str], limit: int = LIMIT
) -> list[dict[str, Any]]:
    """The latest `limit` rows, newest first. `apps`: names of the loaded, switched-on apps.
    A row: `kind, icon, text, detail, sources, decision, when, link` (link `None` when its app
    is not loaded)."""
    return [row for row, _raw in _grouped(repo, now, tz, apps, limit, None, with_raw=False)]


def activity_rows(
    repo: ScopedRepository,
    now: datetime,
    tz: str,
    apps: set[str],
    limit: int = PAGE,
    before: Cursor | None = None,
    *,
    selection: ActivityFilters | None = None,
) -> list[dict[str, Any]]:
    """The Activity page's rows: `recent_activity`'s rows plus the raw fields its columns show,
    `at` (occurred_at), `actor` (unmasked; the page masks it), `parcels` (aliases, sorted),
    `products` (`copper 2 kg/ha` per product), `source_types`, `log_ids` and `devices` (machine
    files' device designators) and `cursor_id` (the group's newest event id). `before`: the last
    shown row's `(at, cursor_id)`; only rows after it in the feed's order (the "Show older"
    page). Current products and devices appear only on each log's newest row, including
    when that row is on a newer page."""
    rows = _grouped(repo, now, tz, apps, limit, before, with_raw=True, selection=selection)
    return [{**row, **raw} for row, raw in rows]


RESOLVING = ("log.confirmed", "log.cancelled", "log.merged")


def _group(events: list[Event], cap: int | None = None) -> dict[tuple[Any, ...], _Group]:
    """Events (newest first) -> groups in order of their newest event; `cap`: stop opening new
    groups after that many (Today's bound)."""
    groups: dict[tuple[Any, ...], _Group] = {}
    for e in events:
        g = groups.get(_key(e))
        if g is None:
            if cap is not None and len(groups) >= cap:
                continue
            g = groups[_key(e)] = _Group(e.kind, e)
        if e.res_model == "parcel" and e.res_id not in g.parcel_ids:
            g.parcel_ids.append(e.res_id)
        log_id = (e.payload or {}).get("log_id")
        if e.kind.startswith("log.") and log_id is not None and log_id not in g.log_ids:
            g.log_ids.append(log_id)
    return groups


def _visible(groups: list[_Group], resolved: set[int]) -> list[_Group]:
    """Drop a pending draft whose logs a later event confirmed, merged or cancelled."""
    return [
        g
        for g in groups
        if not (
            g.kind == "log.created"
            and (g.first.payload or {}).get("status") == "pending"
            and g.log_ids
            and set(g.log_ids) <= resolved
        )
    ]


def _scan(
    repo: ScopedRepository,
    now: datetime,
    filters: list[Any],
    limit: int,
    before: Cursor | None,
    selection: ActivityFilters,
) -> list[_Group]:
    """Activity: read raw events in batches of `FETCH` (newest first, keyset on the query's own
    order) until `limit` complete groups are in hand or the history ends. A group is complete
    once an older timestamp has been read (every group key holds its time, so a group never
    spans two times). Selection keeps whole groups after resolving drafts and before the limit.
    `before`: only groups whose newest event comes after it in that order;
    the events of `before`'s own time are all read, so its groups stay whole."""
    upper = now if before is None else min(now, before[0])
    events: list[Event] = []
    resolved: set[int] = set()
    if before is not None:  # drafts resolved after the cursor, on a newer page
        resolved = _resolved(
            repo.list(
                Event,
                Event.kind.in_(RESOLVING),
                Event.occurred_at <= now,
                Event.occurred_at >= before[0],
                scope=SCOPE,
            )
        )
    last: Cursor | None = None
    while True:
        keyset = []
        if last is not None:
            keyset.append(
                or_(
                    Event.occurred_at < last[0],
                    and_(Event.occurred_at == last[0], Event.id < last[1]),
                )
            )
        batch = repo.list(
            Event,
            Event.occurred_at <= upper,
            *keyset,
            *filters,
            scope=SCOPE,
            order_by=(Event.occurred_at.desc(), Event.id.desc()),
            limit=FETCH,
        )
        events.extend(batch)
        exhausted = len(batch) < FETCH
        if batch:
            last = (batch[-1].occurred_at, batch[-1].id)
        groups = list(_group(events).values())
        if before is not None:
            groups = [g for g in groups if (g.first.occurred_at, g.first.id) < before]
        shown = [g for g in _visible(groups, resolved | _resolved(events)) if selection.matches(g)]
        complete = [g for g in shown if exhausted or last is None or g.first.occurred_at > last[0]]
        if exhausted or len(complete) >= limit:
            return complete[:limit]


def _grouped(
    repo: ScopedRepository,
    now: datetime,
    tz: str,
    apps: set[str],
    limit: int,
    before: Cursor | None,
    *,
    with_raw: bool,
    selection: ActivityFilters | None = None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Grouped rows with their raw fields. Today (`with_raw=False`) keeps its single bounded read
    (`FETCH` events, `limit * 3` groups) and skips the extra reads, so its rows and access_log
    rows stay as they were; Activity (`with_raw=True`) scans in batches (`_scan`)."""
    filters = [
        Event.kind.not_in(HIDDEN_KINDS),
        # Spray records belong to the farm, not to the app that took them down: a voice log stays
        # in the feed when the Telegram assistant is switched off (Records is always on).
        or_(Event.source_app.in_(apps | {"core"}), Event.kind.like("log.%")),
    ]
    if with_raw:
        shown = _scan(repo, now, filters, limit, before, selection or ActivityFilters())
    else:
        events = repo.list(
            Event,
            Event.occurred_at <= now,
            *filters,
            scope=SCOPE,
            order_by=(Event.occurred_at.desc(), Event.id.desc()),
            limit=FETCH,
        )
        groups = _group(events, cap=limit * 3)  # enough to fill `limit` after dropping drafts
        shown = _visible(list(groups.values()), _resolved(events))[:limit]

    parcel_ids = {pid for g in shown for pid in g.parcel_ids}
    aliases = {}
    if parcel_ids:
        for parcel in repo.list(Parcel, Parcel.id.in_(parcel_ids), scope=PARCEL_SCOPE):
            aliases[parcel.id] = parcel.alias
    log_ids = list({i for g in shown for i in g.log_ids})
    products = _products(repo, log_ids)
    devices = _devices(repo, log_ids) if with_raw else {}
    newest = {}
    if with_raw and log_ids:
        # Look beyond this page's cursor: current record values belong only to the newest
        # event group of each log, not to older actors/sources in its history.
        for event in repo.list(
            Event,
            Event.kind.like("log.%"),
            Event.payload["log_id"].as_integer().in_(log_ids),
            Event.occurred_at <= now,
            scope=SCOPE,
            order_by=(Event.occurred_at.desc(), Event.id.desc()),
        ):
            newest.setdefault(event.payload["log_id"], _key(event))

    rows = []
    for g in shown:
        names = sorted({aliases[i] for i in g.parcel_ids if i in aliases}, key=_alias_key)
        p = g.first.payload or {}
        summary_ids = [i for i in g.log_ids if not with_raw or newest.get(i) == _key(g.first)]
        items = [x for i in summary_ids for x in products.get(i, [])]
        raw = {
            "at": g.first.occurred_at,
            "actor": g.first.actor,
            "parcels": names,
            "products": list(dict.fromkeys(items)),
            "source_types": list(p.get("source_types", [])) if g.kind.startswith("log.") else [],
            "log_ids": list(g.log_ids),
            "cursor_id": g.first.id,
            "devices": list(dict.fromkeys(d for i in summary_ids for d in devices.get(i, []))),
        }
        if g.kind.startswith("log."):
            row = _log_row(g, _where(names), items)
        elif g.kind == "advice.changed":
            single = len(g.parcel_ids) == 1
            row = {
                "kind": g.kind,
                "icon": "advisor",
                "text": f"Advice for {_where(names)} changed to",
                "decision": p.get("new"),
                "link": (p.get("card_path") or ADVISOR) if single else ADVISOR,
            }
        elif g.kind.startswith("share."):
            row = {"kind": g.kind, "icon": "share", "text": g.first.body, "link": SHARING}
        else:
            row = {"kind": g.kind, "icon": "history", "text": g.first.body, "link": None}
        row["when"] = when(g.first.occurred_at, now, tz)
        if row["link"] and row["link"].strip("/").split("/")[0] not in apps:
            row["link"] = None
        rows.append(({"detail": "", "sources": [], "decision": None, **row}, raw))
    return rows

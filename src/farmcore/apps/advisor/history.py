"""A parcel's season history (N7): what was sprayed, which infection-risk episodes the advice
detected, when it was protected, and how the advice changed.

Everything is read from stored rows through the request's `ScopedRepository`, bounded by the
season (local calendar year, as the register's season checks count it) and by the replay clock's
`now`, so nothing after now ever shows:

- sprays: done spray logs on the parcel (`ts_start`), with each quantity's product and dose;
- episodes: the downy 3-10 events named in the stored `downy_3_10` reasons (`events`), with the
  rain and temperature the rule stored for its latest event, `uncovered` when an evaluation ever
  listed the event as active risk (`active_events`), else covered;
- protection: one period per spray the stored `protection_downy` reason counted as cover
  (verdict `protected`), from the spray to the latest stored cover end;
- changes: the advisor's `advice.changed` events for the parcel.

Only two small JSON members of each stored advice row are read (`jsonb_path_query_first`), not
the whole reasons list. No rule logic here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, literal_column, select
from sqlalchemy.dialects.postgresql import JSONB

from farmcore.core.access import ScopedRepository
from farmcore.core.models.canonical import (
    Advice,
    Event,
    Log,
    LogParcel,
    LogSource,
    Product,
    Quantity,
)

LOGS_SCOPE = "logs:read"
ADVICE_SCOPE = "advice:read"
DECISION_WORDS = {"spray": "Spray", "wait": "Wait", "protected": "Protected", "none": "No advice"}


def _rule(name: str):
    path = literal_column(f"""'$[*] ? (@.rule == "{name}")'::jsonpath""")
    return func.jsonb_path_query_first(Advice.reasons, path, type_=JSONB)


@dataclass(frozen=True)
class SprayItem:
    at: datetime
    doses: tuple[str, ...]  # "BOUILLIE BORDELAISE RSR DISPERSS NC 2 kg/ha"
    note: str = ""  # e.g. the demo preset's "copper omitted, label maximum ... reached"


@dataclass
class Episode:
    at: datetime
    rain_mm: float | None = None
    temp_c: float | None = None
    uncovered: bool = False


@dataclass
class Cover:
    spray_at: datetime
    until: datetime | None
    products: tuple[str, ...] = ()


@dataclass(frozen=True)
class Change:
    at: datetime
    old: str
    new: str
    headline: str


@dataclass(frozen=True)
class History:
    season: int
    seasons: tuple[int, ...]
    sprays: list[SprayItem] = field(default_factory=list)
    episodes: list[Episode] = field(default_factory=list)
    covers: list[Cover] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    # The season's sprays are the demo preset's simulated passes: no advice was stored along the
    # way, so "none" would be wrong for protection periods and advice changes.
    demo_season: bool = False


def _ts(value: Any) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


def _num(value: Decimal) -> str:
    s = f"{value:f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def bounds(season: int, now: datetime, tz: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(tz)
    start = datetime(season, 1, 1, tzinfo=zone)
    return start, min(datetime(season + 1, 1, 1, tzinfo=zone), now)


def seasons(repo: ScopedRepository, parcel_id: int, now: datetime, tz: str) -> tuple[int, ...]:
    """The seasons with stored advice or a done spray on the parcel up to now, newest first;
    always the current one."""
    first = repo.execute(
        select(func.min(Advice.evaluated_at)).where(
            Advice.farm_id == repo.farm_id, Advice.parcel_id == parcel_id
        ),
        scope=ADVICE_SCOPE,
        resource="advice",
    ).scalar()
    logs = repo.list(
        Log,
        Log.type == "spray",
        Log.status == "done",
        Log.ts_start <= now,
        Log.id.in_(select(LogParcel.log_id).where(LogParcel.parcel_id == parcel_id)),
        scope=LOGS_SCOPE,
        order_by=Log.ts_start,
        limit=1,
    )
    zone = ZoneInfo(tz)
    current = now.astimezone(zone).year
    starts = [t for t in (first, logs[0].ts_start if logs else None) if t and t <= now]
    oldest = min((t.astimezone(zone).year for t in starts), default=current)
    return tuple(range(current, oldest - 1, -1))


def season_history(
    repo: ScopedRepository, parcel_id: int, now: datetime, tz: str, season: int
) -> History:
    start, end = bounds(season, now, tz)
    return History(
        season=season,
        seasons=seasons(repo, parcel_id, now, tz),
        sprays=_sprays(repo, parcel_id, start, end),
        **_advice(repo, parcel_id, start, end),
        changes=_changes(repo, parcel_id, start, end),
        demo_season=_demo_season(repo, parcel_id, start, end),
    )


def _demo_season(repo, parcel_id, start, end) -> bool:
    """True when a spray of the season was logged by the 'Season 2024 complete' preset."""
    stmt = (
        select(LogSource.id)
        .join(Log, Log.id == LogSource.log_id)
        .where(
            Log.farm_id == repo.farm_id,
            Log.type == "spray",
            Log.ts_start >= start,
            Log.ts_start <= end,
            Log.id.in_(select(LogParcel.log_id).where(LogParcel.parcel_id == parcel_id)),
            LogSource.raw_payload["scene"].astext == "season-2024",
        )
        .limit(1)
    )
    if repo.private_member:
        stmt = stmt.where(Log.status == "done")
    return repo.execute(stmt, scope=LOGS_SCOPE, resource="log_source").first() is not None


def _sprays(repo, parcel_id, start, end) -> list[SprayItem]:
    logs = repo.list(
        Log,
        Log.type == "spray",
        Log.status == "done",
        Log.ts_start >= start,
        Log.ts_start <= end,
        Log.id.in_(select(LogParcel.log_id).where(LogParcel.parcel_id == parcel_id)),
        scope=LOGS_SCOPE,
        order_by=(Log.ts_start, Log.id),
    )
    if not logs:
        return []
    stmt = (
        select(Quantity, Product.name)
        .outerjoin(Product, Product.id == Quantity.product_id)
        .where(Quantity.log_id.in_([log.id for log in logs]))
        .order_by(Quantity.id)
    )
    doses: dict[int, list[str]] = {}
    for q, product in repo.execute(stmt, scope=LOGS_SCOPE, resource="quantity"):
        name = product or q.label or "Unnamed product"
        doses.setdefault(q.log_id, []).append(f"{name} {_num(q.value)} {q.unit}")
    return [SprayItem(log.ts_start, tuple(doses.get(log.id, ())), log.notes or "") for log in logs]


def _advice(repo, parcel_id, start, end) -> dict[str, Any]:
    rows = repo.execute(
        select(Advice.evaluated_at, _rule("downy_3_10"), _rule("protection_downy"))
        .where(
            Advice.farm_id == repo.farm_id,
            Advice.parcel_id == parcel_id,
            Advice.evaluated_at >= start,
            Advice.evaluated_at <= end,
        )
        .order_by(Advice.evaluated_at),
        scope=ADVICE_SCOPE,
        resource="advice",
    ).all()
    episodes: dict[datetime, Episode] = {}
    covers: dict[datetime, Cover] = {}
    for _, risk, cover in rows:
        inputs = (risk or {}).get("inputs") or {}
        active = {t for t in map(_ts, inputs.get("active_events") or []) if t}
        latest = _ts(inputs.get("event_at"))
        for at in filter(None, map(_ts, inputs.get("events") or [])):
            if at > end:
                continue
            ep = episodes.setdefault(at, Episode(at))
            if at == latest:
                ep.rain_mm = inputs.get("event_r48_mm", ep.rain_mm)
                ep.temp_c = inputs.get("event_t48_c", ep.temp_c)
            ep.uncovered = ep.uncovered or (risk.get("verdict") == "risk" and at in active)
        c_inputs = (cover or {}).get("inputs") or {}
        sprayed = _ts(c_inputs.get("spray_ts"))
        if (cover or {}).get("verdict") == "protected" and sprayed and sprayed <= end:
            covers[sprayed] = Cover(
                sprayed, _ts(c_inputs.get("until")), tuple(c_inputs.get("products") or ())
            )
    return {
        "episodes": [episodes[k] for k in sorted(episodes)],
        "covers": [covers[k] for k in sorted(covers)],
    }


def _changes(repo, parcel_id, start, end) -> list[Change]:
    events = repo.list(
        Event,
        Event.kind == "advice.changed",
        Event.res_model == "parcel",
        Event.res_id == parcel_id,
        Event.occurred_at >= start,
        Event.occurred_at <= end,
        scope=ADVICE_SCOPE,
        order_by=(Event.occurred_at, Event.id),
    )
    out = []
    for e in events:
        p = e.payload or {}
        out.append(
            Change(
                e.occurred_at,
                DECISION_WORDS.get(str(p.get("old")), str(p.get("old"))),
                DECISION_WORDS.get(str(p.get("new")), str(p.get("new"))),
                str(p.get("headline") or ""),
            )
        )
    return out

"""Presentation only: no jobs, weather fetches or new disease/forecast rules."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from farmcore.apps.advisor import service as advisor
from farmcore.apps.advisor.engine.types import floor_hour
from farmcore.apps.advisor.presenter import parcel_row
from farmcore.apps.advisor.priority import of_advice
from farmcore.apps.fields.seed import alias_number
from farmcore.apps.forecast import ranking, store
from farmcore.apps.stations import data_sources as station_sources
from farmcore.apps.stations.service import station_for_farm
from farmcore.apps.stations.today import STALE_AFTER
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.core.urls import with_farm


@dataclass(frozen=True)
class Item:
    key: str
    severity: str
    title: str
    detail: str
    href: str
    observed_at: datetime | None = None
    priority: int | None = None  # N2, shown as a badge next to the status; never in the key
    priority_label: str | None = None
    priority_why: str | None = None
    priority_assumption: bool = False


def item(
    kind, entity, severity, title, detail, href, observed_at=None, *, state=None, priority=None
):
    # Evaluation ids/timestamps deliberately do not determine read state.
    material = json.dumps(
        [kind, entity, title, detail if state is None else state], sort_keys=True, default=str
    )
    key = sha256(material.encode()).hexdigest()[:24]
    p = priority
    return Item(
        key,
        severity,
        title,
        detail,
        href,
        observed_at,
        *((p.level, p.label, p.why, p.assumption) if p else ()),
    )


def items(repo, now, tz, visible, *, busy_days=()):
    out = []
    farm = repo.get(Farm, repo.farm_id, scope="fields:read")
    unavailable = (farm.props or {}).get("advisor_unavailable_reason")
    if "advisor" in visible and unavailable:
        out.append(
            item(
                "unavailable",
                farm.id,
                "info",
                "Advice unavailable",
                unavailable,
                with_farm("/advisor/", farm.id),
            )
        )
    if "advisor" in visible and not unavailable:
        latest = advisor.latest_advice(repo, now)
        parcels = sorted(
            repo.list(Parcel, scope="fields:read"), key=lambda p: alias_number(p.alias)
        )
        unknown = [p for p in parcels if p.id not in latest]
        if unknown:
            out.append(
                item(
                    "unknown",
                    farm.id,
                    "info",
                    "No advice yet",
                    "Awaiting evaluation: " + ", ".join(p.alias for p in unknown) + ".",
                    with_farm("/advisor/", farm.id),
                )
            )
        advice_items = []  # (priority, item): listed highest priority first, then by alias
        for parcel in parcels:
            advice = latest.get(parcel.id)
            if advice and advice.decision in ("spray", "wait"):
                action = "Spray today" if advice.decision == "spray" else "At risk — wait"
                detail = (advice.props or {}).get("headline", "")
                view = parcel_row(parcel, advice, tz)
                if view.window_horizon_h is not None and view.window_start is not None:
                    edge = floor_hour(advice.evaluated_at) + timedelta(hours=view.window_horizon_h)
                    if now < view.window_start < edge:
                        start = view.window_start.astimezone(ZoneInfo(tz))
                        detail += f" Upcoming spray opportunity: {start:%d %b %H:%M}."
                        if view.window_end and not view.window_open_ended:
                            detail += (
                                f" Until {view.window_end.astimezone(ZoneInfo(tz)):%d %b %H:%M}."
                            )
                if view.replay:
                    detail += " Replay: observed weather used for the spray window."
                advice_items.append(
                    (
                        view.priority or 0,
                        item(
                            "advice",
                            parcel.id,
                            "red",
                            f"{parcel.alias} {parcel.name}: {action}",
                            detail,
                            with_farm(f"/advisor/parcels/{parcel.id}", repo.farm_id),
                            advice.evaluated_at,
                            priority=of_advice(advice),
                        ),
                    )
                )
        out.extend(i for _, i in sorted(advice_items, key=lambda pair: -pair[0]))
    if "stations" in visible:
        out.extend(station_items(repo, now, unavailable))
    if "forecast" in visible and not unavailable:
        station = station_for_farm(repo)
        if not station or not station.props.get("archive_only"):
            out.extend(forecast_items(repo, now, tz, farm, busy_days))
    return sorted(
        out, key=lambda i: ({"red": 0, "amber": 1, "info": 2}[i.severity], -(i.priority or 0))
    )


def forecast_items(repo, now, tz, farm, busy_days):
    href = with_farm(f"/#spray-window-{farm.id}-today", farm.id)
    run = store.latest_run(repo, now)
    if run is None:
        return [
            item(
                "forecast",
                farm.id,
                "amber",
                "No forecast available",
                "No imported forecast available at this clock time.",
                href,
            )
        ]
    config = advisor.config_for(farm, now, tz)
    days = ranking.rank_days(run.series, now, tz, config, busy_days=busy_days)
    provenance = "As-issued forecast" if run.as_issued else "Forecast not as issued"
    provenance += f" · {run.label} · issued {run.issued_at.astimezone(ZoneInfo(tz)):%d %b %H:%M}."
    out = []
    by_ts = {row.ts: row for row in run.series}
    missing = []
    for day in days:
        # Completeness is not a weather hazard. Reuse the existing window's required inputs
        # and lookahead; missing rain must never become “Rain 0 mm, avoid”.
        rain_hours = {
            h + timedelta(hours=n)
            for h in day.hours
            for n in range(int(config.window_rain_lookahead_h.value) + 1)
        }
        complete = all(h in by_ts and by_ts[h].rr1_mm is not None for h in rain_hours)
        complete = complete and all(h in by_ts and by_ts[h].ff_kmh is not None for h in day.hours)
        if day.grade == ranking.NO_FORECAST or not complete:
            missing.append(day.day.strftime("%d %b"))
            continue
        if day.grade == "avoid":
            out.append(
                item(
                    "avoid",
                    day.day.isoformat(),
                    "amber",
                    f"Spray window: avoid · {day.day:%d %b}",
                    f"{day.reason}. {provenance}",
                    href,
                    run.available_at,
                    state=day.reason,
                )
            )
    if missing:
        out.append(
            item(
                "forecast-gap",
                farm.id,
                "amber",
                "Forecast coverage incomplete",
                "No forecast yet: " + ", ".join(missing) + ".",
                href,
                run.available_at,
            )
        )
    return out


def station_items(repo, now, unavailable):
    station = station_for_farm(repo)
    href = with_farm(f"/stations/{station.id}" if station else "/stations/", repo.farm_id)
    if station and station.props.get("archive_only"):
        return (
            []
            if unavailable
            else [
                item(
                    "archive",
                    station.id,
                    "info",
                    "Station archive only",
                    "Archive data is not live weather.",
                    href,
                )
            ]
        )
    out = []
    if station and station.props.get("error"):
        out.append(
            item(
                "ingest",
                station.id,
                "amber",
                "Station import failed",
                f"Import failed: {station.props['error']}",
                href,
            )
        )
    summary = station_sources.summary(repo, now)
    if summary.last_at is None:
        out.append(
            item(
                "station",
                repo.farm_id,
                "amber",
                "No observed station data",
                "No station reading available at this clock time.",
                href,
            )
        )
    elif summary.last_at < now - STALE_AFTER:
        out.append(
            item(
                "station",
                repo.farm_id,
                "amber",
                "Station reading is stale",
                f"Last observed reading: {summary.last_at.isoformat()}.",
                href,
                summary.last_at,
            )
        )
    return out

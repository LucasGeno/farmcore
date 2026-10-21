"""One fourteen-date weather plan; Home previews it without selecting another Best day.

Busy dates replan treatment windows locally; stored engine evaluations remain unchanged.
"""

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from farmcore.apps.advisor import service as advice
from farmcore.apps.advisor.engine.types import ceil_hour, floor_hour
from farmcore.apps.forecast import ranking, sources, store
from farmcore.apps.forecast.weather import day_weather
from farmcore.core.access import ScopeDenied, ScopedRepository
from farmcore.core.models.canonical import Farm, Parcel

DAYS = 14
WORDS = {
    "best": ("Best weather", "ok"),
    "good": ("Good", "ok"),
    "possible": ("Possible", "warn"),
    "avoid": ("Avoid", "bad"),
    "no_forecast": ("No forecast yet", "neutral"),
    "busy": ("Unavailable (busy)", "neutral"),
    "passed": ("Hours passed", "neutral"),
}


# Calendar-cell words: short enough for a 7-column phone grid; the full word stays in the details.
SHORT_WORDS = {
    "best": "Best",
    "good": "Good",
    "possible": "Maybe",
    "avoid": "Avoid",
    "no_forecast": "No forecast",
    "busy": "Busy",
    "passed": "Over",
}
# Order of suggestion, best first; only graded, available days compete (never busy, no forecast
# or Avoid: Home would rather show fewer than three days than suggest one to avoid).
GRADE_ORDER = ("best", "good", "possible")


def top_days(days: list[dict], count: int = 3) -> list[dict]:
    """The `count` best days of the shared plan (ties by date), shown in date order."""
    ranked = [d for d in days if d["eligible"] and d["grade"] in GRADE_ORDER]
    ranked.sort(key=lambda d: GRADE_ORDER.index(d["grade"]))  # stable: earlier date wins ties
    return sorted(ranked[:count], key=lambda d: d["day"])


def utc_text(ts: datetime) -> str:
    utc = ts.astimezone(UTC)
    return f"{utc.day} {utc:%b %H:%M} UTC"


def day_label(day: date, today: date) -> str:
    text = f"{day:%a} {day.day} {day:%b}"
    if day == today:
        return f"Today · {text}"
    return f"Tomorrow · {text}" if (day - today).days == 1 else text


def run_info(run):
    if run is None:
        return None
    return {
        "name": "the ECMWF forecast"
        if run.props.get("model") == sources.SINGLE_RUNS_MODEL
        else f"the forecast ({run.label})",
        "issued": None if run.props.get("issued_at_kind") == "receipt" else utc_text(run.issued_at),
        "fetched": utc_text(run.available_at)
        if run.props.get("issued_at_kind") == "receipt"
        else None,
        "receipt": run.props.get("issued_at_kind") == "receipt",
        "available": utc_text(run.available_at),
        "label": run.label,
        "as_issued": run.as_issued,
        "attribution": run.props.get("attribution"),
        "end": utc_text(max(o.ts for o in run.series)) if run.series else None,
    }


def _replacement_window(run, now, tz, config, busy_days, original, *, sulphur):
    """Earliest suitable open run after the original start, within the advisor's search horizon."""
    if run is None:
        return None
    horizon = floor_hour(now) + timedelta(hours=config.window_horizon_h.value)
    earliest = ceil_hour(max(now, original))
    candidates = []
    for d in ranking.rank_days(
        run.series, now, tz, config, days=DAYS, sulphur=sulphur, busy_days=busy_days
    ):
        if d.grade not in ("best", "good") or not d.has_forecast:
            continue
        start, end = max(d.open_from, earliest), min(d.open_until, horizon)
        if end - start >= timedelta(hours=config.window_min_open_hours.value):
            candidates.append((start, end))
    if not candidates:
        return None
    return min(candidates)


def plan_days(
    repo: ScopedRepository, now: datetime, tz: str, busy_days=(), *, sulphur: bool = False
) -> dict:
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()
    run = store.latest_run(repo, now)
    farm = repo.get(Farm, repo.farm_id, scope=store.SCOPE)
    from farmcore.apps.review.service import resolve

    unavailable = advice.current_availability(repo, now, tz)[1]
    try:
        repo.check("parameters:read", "parameter_version")
    except ScopeDenied:
        config = advice.config_for(farm, now, tz)
    else:
        config = resolve(repo, now, tz).config
    band_start = config.calendar.window_day_start_hour_local.value
    band_end = config.calendar.window_day_end_hour_local.value
    # Observation coverage limits agronomy, not an independently known calendar fact.
    withheld = (farm.props or {}).get("advisor_unavailable_reason")
    passed = not withheld and now.astimezone(zone).hour >= band_end
    graded = (
        {
            d.day: d
            for d in ranking.rank_days(
                run.series,
                now,
                tz,
                config,
                days=DAYS - int(passed),
                sulphur=sulphur,
                busy_days=busy_days,
            )
        }
        if run
        else {}
    )
    days = []
    for offset in range(DAYS):
        day = today + timedelta(days=offset)
        d = graded.get(day)
        grade = (
            (d.grade if d.has_forecast else "no_forecast")
            if d
            else "passed"
            if offset == 0 and passed
            else "no_forecast"
        )
        reason = (
            d.reason
            if d
            else "Today’s spray hours have passed"
            if grade == "passed"
            else "No forecast yet"
        )
        word, tone = WORDS[grade]
        days.append(
            {
                "day": day.isoformat(),
                "label": day_label(day, today),
                "grade": grade,
                "word": word,
                "short": SHORT_WORDS[grade],
                "weekday": f"{day:%a}",
                "date_num": day.day,
                "is_today": offset == 0,
                "tone": tone,
                "reason": reason,
                "busy": day in busy_days,
                "eligible": not (offset == 0 and passed),
                "gusts": max((run.gusts[h] for h in d.hours if h in run.gusts), default=None)
                if run and d
                else None,
                "parcels": [],
                **day_weather(run, day, tz),
            }
        )
    undated = []
    rescheduled = []
    if not unavailable:
        current = advice.latest_advice(repo, now, tz=tz)
        by_day = {d["day"]: d for d in days}
        for parcel in repo.list(Parcel, scope="fields:read", order_by=Parcel.id):
            a = current.get(parcel.id)
            if (
                a is None
                or a.decision not in ("spray", "wait")
                or (a.props or {}).get("assessment_status") in ("limited", "unavailable")
            ):
                continue
            p = {"id": parcel.id, "label": f"{parcel.alias} {parcel.name}", "decision": a.decision}
            start, end = a.next_window_start, a.next_window_end
            if start is None:
                undated.append(p)
                continue
            original_day = start.astimezone(zone).date()
            last_day = (
                (end - timedelta(microseconds=1)).astimezone(zone).date() if end else original_day
            )
            conflicts = any(original_day <= day <= last_day for day in busy_days)
            if conflicts:
                replacement = _replacement_window(
                    run,
                    now,
                    tz,
                    config,
                    busy_days,
                    start,
                    sulphur=any("powdery" in (p.get("targets") or []) for p in (a.products or [])),
                )
                p["from_label"] = f"{original_day:%a} {original_day.day} {original_day:%b}"
                if replacement is None:
                    p["schedule_reason"] = "No suitable available window within the advice horizon."
                    undated.append(p)
                    continue
                start, end = replacement
                p["rescheduled_from"] = original_day.isoformat()
                p["to_day"] = start.astimezone(zone).date().isoformat()
                p["to_label"] = day_label(start.astimezone(zone).date(), today)
                rescheduled.append(p)
            local = start.astimezone(zone)
            if local.date().isoformat() in by_day:
                p["window"] = local.strftime("%H:%M") + (
                    f"–{end.astimezone(zone):%d %b %H:%M}" if end else ""
                )
                by_day[local.date().isoformat()]["parcels"].append(p)
    return {
        "display_tz": tz,
        "days": days,
        "run": run_info(run),
        "freshness": store.freshness(repo, now),
        "band": f"{band_start:02d}:00–{band_end:02d}:00",
        "unavailable": unavailable,
        "undated": undated,
        "rescheduled": rescheduled,
        "sulphur": sulphur,
    }

"""`GET /stations/today`: the Today weather card, from the farm's own station only
(`Asset.kind == "station"`, via `service.station_for_farm`), never imported `regional` rows.

The station measures rain, temperature, humidity and wind (`rr1_mm, t_c, rh_pct, ff_kmh`), no
cloud or sun, so the card never says "sunny". `rr1_mm` at hour T is the rain of the hour ending
at T.

- **Now** = the latest hour at or before `clock.now()`: °C, wind, humidity, and a condition word:
  "Raining" when that hour had rain (> 0 mm), else "Dry" (+ "humid" at ≥ 90 % humidity).
  Missing rain stays unknown.
- **Next rain** = the first hour after now, up to now + 72 h, with ≥ 0.2 mm (one gauge tip)
  whose 24 h from that hour on (that hour included, the horizon caps it) bring ≥ 1 mm, and that
  24 h sum. A lone gauge tip is drizzle, not "rain" a farmer plans around. A fixed 24 h sum, not
  "the spell": drizzle spells have gaps, and 24 h is what a spray decision weighs. The day reads
  "today" / "tomorrow" / the weekday.
  In replay the stored hours after now are what the station really measured, used as the
  forecast (the advisor reads the same hours). No hour after now at all = "No forecast", never
  a dry claim.
- **Last 48 h** = rain of the observed hours in (now - 48 h, now].
- **Strip** = the 24 hours after now: one bar per hour with rain, the temperature line, ticks
  every 6 h; inline SVG with `role="img"` and a one-sentence summary as its label.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from farmcore.apps.stations import service
from farmcore.apps.stations.conditions import spray_conditions
from farmcore.apps.stations.connector import REPLAY_HORIZON
from farmcore.core.access import Repo
from farmcore.core.clock import get_clock
from farmcore.web import render

router = APIRouter()
HOUR = timedelta(hours=1)
RAIN_WINDOW = timedelta(hours=48)
RAIN_MIN_MM = 0.2  # one tip of the station's gauge
RAIN_SUM = timedelta(hours=24)
RAIN_COUNTS_MM = 1.0  # the 24 h from the first hour must bring this much to count as rain
HUMID_PCT = 90.0
STALE_AFTER = timedelta(hours=2)  # an older latest reading is labelled "Last reading …"
STRIP_HOURS = 24
TICK_EVERY = 6
# SVG user units: 10 per hour; rain bars from the bottom, the temperature line above them.
STRIP_W, STRIP_H = STRIP_HOURS * 10, 60
RAIN_TOP, TEMP_TOP, TEMP_BOTTOM = 14, 6, 34
RAIN_SCALE_MIN_MM = 2.0  # a 0.2 mm hour stays a small bar


def _fmt(value: float) -> str:
    text = f"{value:.0f}"
    return "0" if text == "-0" else text


def _mm(value: float) -> str:
    return f"{value:.1f}"


def _rain_coverage(
    by_ts: dict[datetime, dict[str, Any]], start: datetime, end: datetime
) -> tuple[int, int]:
    """Known rain hours in (start, end]; temperature does not establish rain coverage."""
    hour = start.replace(minute=0, second=0, microsecond=0) + HOUR
    known = expected = 0
    while hour <= end:
        expected += 1
        known += by_ts.get(hour, {}).get("rr1_mm") is not None
        hour += HOUR
    return known, expected


def _strip(by_ts: dict[datetime, dict[str, Any]], start: datetime, zone: ZoneInfo) -> dict:
    hours = [start + HOUR * i for i in range(STRIP_HOURS + 1)]
    rain = [(i, by_ts.get(h, {}).get("rr1_mm")) for i, h in enumerate(hours) if i > 0]
    peak = max([RAIN_SCALE_MIN_MM, *(r for _, r in rain if r)])
    bars = []
    for i, r in rain:
        if r and r > 0:
            height = max(1.5, (r / peak) * (STRIP_H - RAIN_TOP))
            bars.append({"x": (i - 1) * 10 + 1.5, "y": STRIP_H - height, "h": height, "mm": r})
    temps = [(i, by_ts.get(h, {}).get("t_c")) for i, h in enumerate(hours)]
    known = [t for _, t in temps if t is not None]
    path, t_min, t_max = "", None, None
    if known:
        t_min, t_max = min(known), max(known)
        span = max(t_max - t_min, 4.0)
        mid = (t_max + t_min) / 2
        lo = mid - span / 2
        pen = "M"
        for i, t in temps:
            if t is None:
                pen = "M"  # a missing hour is a gap, never 0
                continue
            y = TEMP_BOTTOM - (t - lo) / span * (TEMP_BOTTOM - TEMP_TOP)
            path += f"{pen}{i * 10:.0f} {y:.1f} "
            pen = "L"
    total = sum(r for _, r in rain if r)
    rainy = sum(1 for _, r in rain if r and r > 0)
    rain_known = sum(r is not None for _, r in rain)
    if rainy:
        said_rain = f"{_mm(total)} mm of rain over {rainy} hour{'s' if rainy > 1 else ''}"
    elif rain_known == STRIP_HOURS:
        said_rain = "no rain"
    else:
        said_rain = "rain readings unavailable" if not rain_known else "known rain total 0.0 mm"
    if rain_known < STRIP_HOURS:
        said_rain += f"; rain coverage {rain_known}/{STRIP_HOURS} h"
    said_temp = f", {_fmt(t_min)} to {_fmt(t_max)} °C" if known else ""
    ticks = [
        {
            "pct": i / STRIP_HOURS * 100,
            "label": "Now" if i == 0 else hours[i].astimezone(zone).strftime("%H:%M"),
        }
        for i in range(0, STRIP_HOURS + 1, TICK_EVERY)
    ]
    return {
        "w": STRIP_W,
        "h": STRIP_H,
        "bars": bars,
        "temp_path": path.strip(),
        "grid": [i * 10 for i in range(TICK_EVERY, STRIP_HOURS, TICK_EVERY)],
        "ticks": ticks,
        "label": f"Next 24 h: {said_rain}{said_temp}.",
        "rain_known": rain_known,
        "rain_complete": rain_known == STRIP_HOURS,
    }


_when = service.reading_when  # kept for this module's callers


def weather_card(rows: list[dict[str, Any]], now: datetime, tz: str) -> dict[str, Any] | None:
    """The card from hourly rows (`service.load_hourly`, any span around now); None when no
    hour at or before now has a reading."""
    zone = ZoneInfo(tz)
    past = [r for r in rows if r["ts"] <= now]
    if not past:
        return None
    latest = past[-1]
    future = [r for r in rows if now < r["ts"] <= now + REPLAY_HORIZON]
    rain_now = latest["rr1_mm"]
    raining = rain_now is not None and rain_now > 0
    humid = latest["rh_pct"] is not None and latest["rh_pct"] >= HUMID_PCT
    by_ts = {r["ts"]: r for r in rows}
    recent_known, recent_expected = _rain_coverage(by_ts, now - RAIN_WINDOW, now)
    recent = [r["rr1_mm"] for r in past if r["ts"] > now - RAIN_WINDOW and r["rr1_mm"] is not None]
    forecast_by_ts = {r["ts"]: r for r in future}
    next_rain = None
    for first in (r for r in future if (r["rr1_mm"] or 0) >= RAIN_MIN_MM):
        end = first["ts"] + RAIN_SUM
        amount = sum(r["rr1_mm"] or 0 for r in future if first["ts"] <= r["ts"] < end)
        if amount >= RAIN_COUNTS_MM:
            # `rr1` at T is the rain of the hour ending at T: the rain starts at T - 1 h.
            known, expected = _rain_coverage(forecast_by_ts, first["ts"] - HOUR, end - HOUR)
            leading_known, leading_expected = _rain_coverage(forecast_by_ts, now, first["ts"])
            next_rain = {
                "when": _when(first["ts"] - HOUR, now, zone),
                "mm": _mm(amount),
                "complete": known == expected,
                "start_complete": leading_known == leading_expected,
            }
            break
    start = now.replace(minute=0, second=0, microsecond=0)
    # The forecast may stop short of 72 h (the end of the replay data): never claim more.
    covered_h = int((future[-1]["ts"] - now) / HOUR) if future else 0
    forecast_known, forecast_expected = _rain_coverage(
        by_ts, now, future[-1]["ts"] if future else now
    )
    stale = latest["ts"] < now - STALE_AFTER
    return {
        "at": latest["ts"].astimezone(zone).strftime("%H:%M"),
        "stale": _when(latest["ts"], now, zone) if stale else None,
        "horizon": "the next 3 days" if covered_h >= 72 else f"the next {covered_h} h",
        "t_c": None if latest["t_c"] is None else _fmt(latest["t_c"]),
        "wind_kmh": None if latest["ff_kmh"] is None else _fmt(latest["ff_kmh"]),
        "rh_pct": None if latest["rh_pct"] is None else _fmt(latest["rh_pct"]),
        "raining": raining,
        "condition": (
            "Rain unknown"
            if rain_now is None
            else "Raining"
            if raining
            else "Dry · humid"
            if humid
            else "Dry"
        ),
        "condition_icon": "info" if rain_now is None else "rain" if raining else "dry",
        "rain_48h": _mm(sum(recent)) if recent else None,
        "rain_48h_known": recent_known,
        "rain_48h_complete": recent_known == recent_expected,
        "forecast_rain_complete": forecast_expected > 0 and forecast_known == forecast_expected,
        "has_forecast": bool(future),
        "next_rain": next_rain,
        "strip": _strip({r["ts"]: r for r in rows}, start, zone) if future else None,
        "spray": spray_conditions(rows, now, tz) if future else None,
    }


@router.get("/today", response_class=HTMLResponse)
def today_fragment(request: Request, repo: Repo, compact: bool = False) -> HTMLResponse:
    settings = request.app.state.settings
    now = get_clock(repo.session, settings).now()
    station = service.station_for_farm(repo)
    if station is not None and station.props.get("archive_only"):
        return render(
            request,
            "stations/_archive_summary.html",
            archive=station.props["archive"],
            archive_url=station.props.get("archive_page"),
        )
    card = None
    if station is not None:
        rows = service.load_hourly(repo, station.id, now - RAIN_WINDOW, now + REPLAY_HORIZON)
        card = weather_card(rows, now, settings.display_tz)
    return render(
        request,
        "stations/_today_now.html" if compact else "stations/_today.html",
        station=None if station is None else {"id": station.id, "name": station.name},
        card=card,
    )

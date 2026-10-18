"""Stations assistant tool (read-only): `stations_weather(day)`.

Station-only, a hard rule: it reads the farm's `Asset.kind == "station"` asset (Météo-France
33394002, Licence Ouverte) and only observations of a station asset, never a `regional` asset
(regional weather imports use that kind). Enforced by
tests/apps/stations/test_weather_station_only.py.

`day` is "today", "tomorrow" (also "aujourd'hui", "demain", "yesterday", "hier", the day after
tomorrow / before yesterday) or a local date `YYYY-MM-DD`; hours are capped at now + 72 h, the
replay "forecast" (observed values replayed ahead of the clock, so `replayed` is always true). A
day the cap cuts short sums only the hours up to the cap and says so (`until`, local HH:MM).
`rr1_mm` is an hour-ending total, so a day's rain is the slots in (00:00, stop]; the readings
(temperature, wind, humidity) are the slots in [00:00, stop), stop = next 00:00 or the cap.
A day with a slot missing for any variable is never shown as complete: the facts then carry
`expected_hours` (the rain slots) and one count per variable (`rain_hours`, `t_hours`,
`wind_hours`, `rh_hours`), the rain reads "at least", a variable without any reading reads "not
recorded" (never 0) and a second line lists the gaps. A cap off the hour (now 06:30) keeps the
readings up to it, but the hourly rain totals end at the hour before: `rain_until` and "3 mm
rain to 06:00" say so (no interpolation of the last minutes).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from farmcore.apps.stations import service
from farmcore.apps.stations.connector import REPLAY_HORIZON
from farmcore.core.models.canonical import Asset, Observation
from farmcore.core.tools import ToolContext, ToolResult, ToolSpec

SCOPE = service.SCOPE
STATION = "station"
STATION_PATH = "/stations/"
WEATHER_FACTS = frozenset(
    {"day", "rain_mm", "t_min", "t_max", "wind_max_kmh", "rh_mean", "replayed", "until", "error"}
    | {"expected_hours", "rain_hours", "t_hours", "wind_hours", "rh_hours", "rain_until"}
)
# Coverage fact per variable, and its words in the "Incomplete" line.
COVERAGE = (
    ("rr1_mm", "rain_hours", "rain", "pluie"),
    ("t_c", "t_hours", "temperature", "température"),
    ("ff_kmh", "wind_hours", "wind", "vent"),
    ("rh_pct", "rh_hours", "humidity", "humidité"),
)
DAY_WORDS = {
    "today": 0,
    "aujourd'hui": 0,
    "aujourd hui": 0,
    "tomorrow": 1,
    "demain": 1,
    "yesterday": -1,
    "hier": -1,
    "day after tomorrow": 2,
    "après-demain": 2,
    "apres-demain": 2,
    "day before yesterday": -2,
    "avant-hier": -2,
}
DAY_TEXT = {
    "en": {0: "today", 1: "tomorrow", -1: "yesterday", 2: "the day after tomorrow"},
    "fr": {0: "aujourd'hui", 1: "demain", -1: "hier", 2: "après-demain"},
}

TOOLS = (
    ToolSpec(
        name="stations_weather",
        description=(
            "One local day's weather at the farm's Météo-France station (replayed observations, "
            "up to now + 72 h): rain mm, min and max temperature, max wind, mean humidity."
        ),
        params={"day": "STRING"},
        func="farmcore.apps.stations.tools:weather",
        scope=SCOPE,
        keywords=(
            "weather",
            "forecast",
            "rain",
            "temperature",
            "wind",
            "meteo",
            "prevision",
            "pluie",
            "vent",
            "temps demain",
        ),
    ),
)


def _day(ctx: ToolContext, day: str) -> date | None:
    today = ctx.now.astimezone(ctx.tz).date()
    key = day.strip().casefold().replace("’", "'")
    if key in DAY_WORDS:
        return today + timedelta(days=DAY_WORDS[key])
    try:
        return date.fromisoformat(key)
    except ValueError:
        return None


def _round(values: list[float], agg: Any) -> float | None:
    return round(agg(values), 1) if values else None


def _num(x: float, unit: str, fr: bool) -> str:
    s = f"{x:g}"
    return (s.replace(".", ",") if fr else s) + unit


def weather(ctx: ToolContext, day: str = "today") -> ToolResult:
    d = _day(ctx, day)
    if d is None:
        return ToolResult(facts={"error": "unknown day"}, text=_no_day(ctx))
    start = datetime(d.year, d.month, d.day, tzinfo=ctx.tz)
    end = start + timedelta(days=1)
    cap = ctx.now + REPLAY_HORIZON
    if start > cap:
        text = (
            "Je n'ai la météo que pour les 72 prochaines heures."
            if ctx.lang == "fr"
            else "I only have the weather for the next 72 hours."
        )
        return ToolResult(facts={"error": "beyond forecast", "day": d.isoformat()}, text=text)
    station = service.station_for_farm(ctx.repo)  # Asset.kind == "station"
    if station is None or station.kind != STATION:
        return ToolResult(facts={"error": "no station"}, text="No weather station is set up.")
    stations = select(Asset.id).where(Asset.kind == STATION)
    stop = min(end, cap)
    obs = ctx.repo.list(
        Observation,
        Observation.asset_id == station.id,
        Observation.asset_id.in_(stations),
        Observation.ts >= start,
        Observation.ts <= stop,
        scope=SCOPE,
        order_by=(Observation.ts, Observation.variable),
    )

    def values(variable: str) -> list[float]:
        # rr1_mm is the rain of the hour ending at ts: (start, stop]. Readings: [start, stop).
        if variable == "rr1_mm":
            return [o.value for o in obs if o.variable == variable and o.ts > start]
        return [o.value for o in obs if o.variable == variable and o.ts < stop]

    rh = values("rh_pct")
    facts: dict[str, Any] = {
        "day": d.isoformat(),
        "rain_mm": _round(values("rr1_mm"), sum),
        "t_min": _round(values("t_c"), min),
        "t_max": _round(values("t_c"), max),
        "wind_max_kmh": _round(values("ff_kmh"), max),
        "rh_mean": round(sum(rh) / len(rh), 1) if rh else None,
        "replayed": True,
    }
    if end > cap:  # the cap cuts the day short: only the hours up to it are summed
        facts["until"] = f"{cap.astimezone(ctx.tz):%H:%M}"
    rain_slots, reading_slots = _slots(start, stop)
    rain_end = start.astimezone(UTC) + timedelta(hours=rain_slots)
    if rain_end < stop:  # a cap off the hour: the hourly rain totals end at the hour before it
        facts["rain_until"] = f"{rain_end.astimezone(ctx.tz):%H:%M}"
    counts = {key: len(values(var)) for var, key, *_ in COVERAGE}
    expected = {key: rain_slots if var == "rr1_mm" else reading_slots for var, key, *_ in COVERAGE}
    gaps = any(counts[key] < expected[key] for key in counts)
    if gaps:
        facts["expected_hours"] = rain_slots
        facts.update(counts)
    text = _text(ctx, d, facts, station.name, expected if gaps else None)
    return ToolResult(
        facts=facts,
        text=text,
        links=(("Open station", STATION_PATH),),
    )


def _slots(start: datetime, stop: datetime) -> tuple[int, int]:
    """(rain slots in (start, stop], reading slots in [start, stop)) on the UTC hour grid, so a
    DST day has 23 or 25 and a cap off the hour still counts the reading before it."""
    first, last = start.astimezone(UTC), stop.astimezone(UTC)
    whole = int((last - first) // timedelta(hours=1))
    return whole, whole + (1 if first + timedelta(hours=whole) < last else 0)


def _no_day(ctx: ToolContext) -> str:
    if ctx.lang == "fr":
        return "Quel jour ? Dites aujourd'hui, demain ou une date (AAAA-MM-JJ)."
    return "Which day? Say today, tomorrow or a date (YYYY-MM-DD)."


def _text(
    ctx: ToolContext,
    d: date,
    f: dict[str, Any],
    station: str,
    expected: dict[str, int] | None = None,
) -> str:
    fr = ctx.lang == "fr"
    offset = (d - ctx.now.astimezone(ctx.tz).date()).days
    words = DAY_TEXT["fr" if fr else "en"]
    until = f.get("until")
    gaps = [
        (en, fr_word, f[key], expected[key])
        for _, key, en, fr_word in COVERAGE
        if expected and f[key] < expected[key]
    ]
    rain_short = bool(expected) and f.get("rain_hours", 0) < (expected or {}).get("rain_hours", 0)
    temp = (
        f"{_num(f['t_min'], '', fr)}–{_num(f['t_max'], ' °C', fr)}"
        if f["t_min"] is not None
        else ("température non relevée" if fr else "temperature not recorded")
    )
    if fr:
        when = f"{words[offset]} ({d:%d/%m})" if offset in words else f"le {d:%d/%m}"
        when += f" jusqu'à {until}" if until else ""
        if f["rain_mm"] is None:
            rain = "pluie non relevée"
        else:
            rain = ("au moins " if rain_short else "") + f"{_num(f['rain_mm'], ' mm', fr)} de pluie"
            rain += f" jusqu'à {f['rain_until']}" if f.get("rain_until") else ""
        wind = (
            f"vent jusqu'à {_num(f['wind_max_kmh'], ' km/h', fr)}"
            if f["wind_max_kmh"] is not None
            else "vent non relevé"
        )
        rh = (
            f"humidité {_num(f['rh_mean'], ' %', fr)}"
            if f["rh_mean"] is not None
            else "humidité non relevée"
        )
        lines = [f"Météo {when}, station {station} : {rain}, {temp}, {wind}, {rh}."]
        if gaps:
            parts = [
                f"{w} {n} sur {total}" + (" h" if w == "pluie" else "") for _, w, n, total in gaps
            ]
            lines.append("Incomplet : " + ", ".join(parts) + ".")
        lines.append("Observations Météo-France rejouées, pas une prévision.")
        return "\n".join(lines)
    when = f"{words[offset]} ({d:%d %b})" if offset in words else f"on {d:%d %b}"
    when += f" until {until}" if until else ""
    if f["rain_mm"] is None:
        rain = "rain not recorded"
    else:
        rain = ("at least " if rain_short else "") + f"{_num(f['rain_mm'], ' mm', fr)} rain"
        rain += f" to {f['rain_until']}" if f.get("rain_until") else ""
    wind = (
        f"wind up to {_num(f['wind_max_kmh'], ' km/h', fr)}"
        if f["wind_max_kmh"] is not None
        else "wind not recorded"
    )
    rh = (
        f"humidity {_num(f['rh_mean'], ' %', fr)}"
        if f["rh_mean"] is not None
        else "humidity not recorded"
    )
    lines = [f"Weather {when}, station {station}: {rain}, {temp}, {wind}, {rh}."]
    if gaps:
        parts = [f"{w} {n} of {total}" + (" h" if w == "rain" else "") for w, _, n, total in gaps]
        lines.append("Incomplete: " + ", ".join(parts) + ".")
    lines.append("Replayed Météo-France observations, not a forecast.")
    return "\n".join(lines)

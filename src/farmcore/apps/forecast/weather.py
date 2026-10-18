"""Local-day forecast facts; missing hours are never zero rainfall."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from farmcore.apps.forecast.store import StoredRun

HOUR = timedelta(hours=1)


def day_weather(run: StoredRun | None, day: date, tz: str) -> dict:
    zone = ZoneInfo(tz)
    start = datetime.combine(day, time(), zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time(), zone).astimezone(UTC)
    expected = int((end - start) / HOUR)
    series = run.series if run else []
    rain = [o.rr1_mm for o in series if start < o.ts <= end and o.rr1_mm is not None]
    wind = [o.ff_kmh for o in series if start <= o.ts < end and o.ff_kmh is not None]
    temp = [o.t_c for o in series if start <= o.ts < end and o.t_c is not None]
    gust = [v for ts, v in run.gusts.items() if start <= ts < end] if run else []
    return {
        "has_data": bool(rain or wind or temp or gust),
        "expected": expected,
        "rain": {"value": sum(rain) if rain else None, "known": len(rain)},
        "wind": {"value": max(wind) if wind else None, "known": len(wind)},
        "gust": {"value": max(gust) if gust else None, "known": len(gust)},
        "temp": {
            "low": min(temp) if temp else None,
            "high": max(temp) if temp else None,
            "known": len(temp),
        },
    }

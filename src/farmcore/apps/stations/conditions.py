"""Conservative weather flags for the next hours, independent of product-specific advice.

Crop protection decides whether and when to apply. These flags retain gauge-tip rain,
wind and possible wash-off facts; they never declare a spray window open or prohibited.
The 0.2 mm gauge tip, 19 km/h wind flag and two-hour rain buffer are app thresholds,
not label-specific application limits or exact statutory boundaries.

An hour at h reads the station row at h+1 (rain of the hour ending there), then rain
at h+2 and h+3. Known rain/strong wind wins over missing readings. Otherwise missing own
wind/rain is unknown; breeze or known rain soon is caution; missing future rain remains
unknown, never dry. Covered counts the leading hours with the full lookahead horizon.
Known partial flags remain visible even with zero covered hours; the summary explicitly
labels that forecast incomplete. Cell reasons retain the measured values.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

HOUR = timedelta(hours=1)

# One tip of the station's tipping-bucket gauge: less is not measured rain.
RAIN_MM = 0.2
# France, arrêté du 4 mai 2017 art. 2: no spraying or dusting when the wind exceeds 3 on the
# Beaufort scale (Beaufort 3 = 12-19 km/h). Read conservatively: 19 km/h or more is "no".
WIND_NO_KMH = 19.0
# Beaufort 3 starts at 12 km/h ("gentle breeze"): drift rises, spray with care (anti-drift
# nozzles, buffer strips). Below it, a light breeze, is the usual advice for spraying.
WIND_MARGINAL_KMH = 12.0
# Rain right after a spray washes it off before it dries on the leaf. Labels give their own
# rain-fastness (often 1-6 h); 2 h after the spray hour is a simplification for every product.
WASH_OFF_HOURS = 2

STATES = ("good", "marginal", "no", "unknown")
GLYPHS = {"good": "✓", "marginal": "~", "no": "✕", "unknown": "?"}
WORDS = {
    "good": "Dry, light wind",
    "marginal": "Caution",
    "no": "Weather flag",
    "unknown": "No data",
}
_PHRASES = {
    ("no", "rain"): "Rain",
    ("no", "wind"): "Strong wind",
    ("marginal", "wind"): "Breezy",
    ("marginal", "rain_after"): "Rain soon",
    ("unknown", "missing"): "No weather data",
}


def _mm(value: float) -> str:
    return f"{value:.1f}"


def _judge(
    rain: float | None, wind: float | None, after: list[float | None]
) -> tuple[str, str, str]:
    """(state, cause, reason) for one hour from its rain, wind and the rain of the hours after."""
    if rain is not None and rain >= RAIN_MM:
        return "no", "rain", f"Rain {_mm(rain)} mm"
    if wind is not None and wind >= WIND_NO_KMH:
        return "no", "wind", f"Wind {wind:.0f} km/h"
    if wind is None:
        return "unknown", "missing", "No wind reading"
    if rain is None:
        return "unknown", "missing", "No rain reading"
    if wind >= WIND_MARGINAL_KMH:
        return "marginal", "wind", f"Wind {wind:.0f} km/h"
    soon = sum(r for r in after if r is not None and r >= RAIN_MM)
    if soon:
        return "marginal", "rain_after", f"Rain {_mm(soon)} mm within {WASH_OFF_HOURS} h"
    if any(r is None for r in after):
        return "unknown", "missing", f"No rain reading for the {WASH_OFF_HOURS} h after"
    return "good", "", f"Wind {wind:.0f} km/h, dry"


def _at(ts: datetime, now: datetime, zone: ZoneInfo) -> str:
    local = ts.astimezone(zone)
    if (local.date() - now.astimezone(zone).date()).days == 1:
        return "midnight" if (local.hour, local.minute) == (0, 0) else f"tomorrow {local:%H:%M}"
    return f"{local:%H:%M}"


def _summary(cells: list[dict[str, Any]], now: datetime, zone: ZoneInfo, hours: int) -> str:
    if not cells:
        return "No forecast"
    span = f"all the next {hours} h" if len(cells) == hours else f"for the next {len(cells)} h"
    first = cells[0]
    if first["state"] == "good":
        stop = next((c for c in cells if c["state"] != "good"), None)
        if stop is None:
            return f"Dry, light wind {span}"
        return f"Dry, light wind until {_at(stop['ts'], now, zone)}"
    phrase = _PHRASES[(first["state"], first["cause"])]
    key = (first["state"], first["cause"])
    change = next((c for c in cells if (c["state"], c["cause"]) != key), None)
    if change is None:
        return f"{phrase} {span}"
    head = f"{phrase} until {_at(change['ts'], now, zone)}"
    if change["state"] == "good":
        return f"{head}, then dry, light wind"
    good = next((c for c in cells if c["ts"] > change["ts"] and c["state"] == "good"), None)
    if good is None:
        return f"{head}, no dry hour with light wind in the next {len(cells)} h"
    return f"{head}, dry, light wind from {_at(good['ts'], now, zone)}"


def spray_conditions(
    rows: list[dict[str, Any]], now: datetime, tz: str, hours: int = 24
) -> dict[str, Any]:
    """`{hours: [{ts, label, state, reason, glyph, word, now}], summary, state, covered}` from
    hourly rows (`service.load_hourly`, any span around now). `state` is the current hour's
    state, or `none` when no hour can be judged at all. See the module docstring for the rules."""
    zone = ZoneInfo(tz)
    by_ts = {r["ts"]: r for r in rows}
    last = max((r["ts"] for r in rows), default=None)
    start = now.replace(minute=0, second=0, microsecond=0)
    cells: list[dict[str, Any]] = []
    covered = 0
    for i in range(hours):
        ts = start + HOUR * i
        own = by_ts.get(ts + HOUR, {})
        after = [by_ts.get(ts + HOUR * (k + 2), {}).get("rr1_mm") for k in range(WASH_OFF_HOURS)]
        state, cause, reason = _judge(own.get("rr1_mm"), own.get("ff_kmh"), after)
        full = last is not None and ts + HOUR * (WASH_OFF_HOURS + 1) <= last
        if full and covered == i:
            covered += 1
        if state == "unknown" and not full:
            reason = "Beyond the forecast"
        cells.append(
            {
                "ts": ts,
                "label": f"{ts.astimezone(zone):%H:%M}",
                "state": state,
                "cause": cause,
                "reason": reason,
                "glyph": GLYPHS[state],
                "word": WORDS[state],
                "now": i == 0,
            }
        )
    known = [i for i, cell in enumerate(cells) if cell["state"] != "unknown"]
    judged = cells[:covered] if covered else cells[: known[-1] + 1] if known else []
    summary = _summary(judged, now, zone, hours)
    if not covered and known:
        summary += " (forecast incomplete)"
    return {
        "hours": cells,
        "summary": summary,
        "has_known": bool(known),
        "state": judged[0]["state"] if judged else "none",
        "covered": covered,
    }


def rules() -> dict[str, Any]:
    """The thresholds, for the "How we decide" text."""
    return {
        "rain_mm": _mm(RAIN_MM),
        "wind_no_kmh": f"{WIND_NO_KMH:.0f}",
        "wind_marginal_kmh": f"{WIND_MARGINAL_KMH:.0f}",
        "wash_off_hours": WASH_OFF_HOURS,
    }

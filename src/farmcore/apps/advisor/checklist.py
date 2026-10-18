"""The advice card's ✅/❌ checklist (3-10 rule style), built from the stored reasons only.

One row per condition of the downy rule, each with a met / not met mark, the value and the
threshold with units:

- shoots ≥ 10 cm: the `season_gate` verdict and its date (assumed when the gate date is a
  default, i.e. named in the reason note's `(assumed: …)`);
- mean temperature ≥ `downy_temp_min_c` over `downy_window_h`, from `event_t48_c`;
- rain ≥ `downy_rain_min_mm` in `downy_window_h`, from `event_r48_mm`.

Thresholds come from each reason's `threshold` dict, never from literals here. The values are the
infection-event window ones, so the rows match the headline; with no event they are the
current 48 h (`t48_c`, `r48_mm`) and the row says "last 48 h". No engine call, no decision change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

_ASSUMED_RE = re.compile(r"\(assumed: ([^)]*)\)")


@dataclass(frozen=True)
class CheckRow:
    key: str  # "shoots" | "temperature" | "rain": the `data-check` hook
    label: str  # "Rain ≥ 10 mm in 48 h"
    met: bool | None  # None: the value is missing (data gap)
    value: str  # "10.3 mm"
    raw: str  # the value as a bare number/date, for `data-value` ("10.3")
    window: str  # "48 h before the infection event of 9 Apr 00:00" | "last 48 h"
    assumed: bool


def assumed_keys(reason: dict[str, Any]) -> set[str]:
    """Threshold keys the reason's note marks `(assumed: a, b)`."""
    match = _ASSUMED_RE.search(reason.get("note", ""))
    return {n.strip() for n in match.group(1).split(",")} if match else set()


def _num(value: Any) -> str:
    """10.0 -> "10", 10.3 -> "10.3", 15.41 -> "15.4"."""
    return f"{float(value):.1f}".removesuffix(".0")


def _local(ts: str, tz: str) -> datetime:
    return datetime.fromisoformat(ts).astimezone(ZoneInfo(tz))


def day_text(d: date) -> str:
    """`9 Apr 2024` (no leading zero)."""
    return f"{d.day} {d:%b %Y}"


def _gate_row(gate: dict[str, Any]) -> CheckRow:
    day = gate.get("threshold", {}).get("season_gate")
    started = gate.get("verdict") == "season started"
    when = day_text(date.fromisoformat(day)) if day else "date unknown"
    return CheckRow(
        key="shoots",
        label="Shoots ≥ 10 cm",
        met=started,
        value=f"since {when}" if started else f"expected {when}",
        raw=day or "",
        window="",
        assumed="season_gate" in assumed_keys(gate),
    )


def _downy_rows(downy: dict[str, Any], tz: str) -> list[CheckRow]:
    inputs, threshold = downy.get("inputs", {}), downy.get("threshold", {})
    assumed = assumed_keys(downy)
    hours = threshold.get("downy_window_h")
    event = inputs.get("event_at")
    if event:
        temp, rain = inputs.get("event_t48_c"), inputs.get("event_r48_mm")
        at = _local(event, tz)
        window = f"{hours} h up to the infection event of {at.day} {at:%b %H:%M}"
    else:
        temp, rain = inputs.get("t48_c"), inputs.get("r48_mm")
        window = f"last {hours} h"
    t_min, r_min = threshold.get("downy_temp_min_c"), threshold.get("downy_rain_min_mm")

    def row(key: str, label: str, value: Any, minimum: Any, unit: str, name: str) -> CheckRow:
        met = None if value is None or minimum is None else float(value) >= float(minimum)
        shown = "no data" if value is None else f"{_num(value)} {unit}"
        return CheckRow(
            key=key,
            label=label,
            met=met,
            value=shown,
            raw="" if value is None else _num(value),
            window=window,
            assumed=name in assumed,
        )

    return [
        row(
            "temperature",
            f"Mean temperature ≥ {_num(t_min)} °C",
            temp,
            t_min,
            "°C",
            "downy_temp_min_c",
        ),
        row(
            "rain",
            f"Rain ≥ {_num(r_min)} mm in {hours} h",
            rain,
            r_min,
            "mm",
            "downy_rain_min_mm",
        ),
    ]


def checklist(reasons: list[dict[str, Any]], tz: str) -> list[CheckRow]:
    """The 3-10 checklist rows, in reading order; empty when the reasons lack both rules."""
    by_rule = {r["rule"]: r for r in reasons}
    rows: list[CheckRow] = []
    if "season_gate" in by_rule:
        rows.append(_gate_row(by_rule["season_gate"]))
    if "downy_3_10" in by_rule:
        rows += _downy_rows(by_rule["downy_3_10"], tz)
    return rows

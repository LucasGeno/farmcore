"""Priority (Due · High · Urgent) of a parcel that needs a pass, from its stored advice row only.

A ranking of which parcel to treat first, never a probability: the rules are not a calibrated
model, so it carries no percentage. Inputs, all already stored on the advice row:

- the decision (only `spray` and `wait` need action; anything else has no priority);
- uncovered active downy risk: `downy_3_10` verdict `risk` with active events, and
  `protection_downy` verdict `no cover`;
- hours uncovered under that risk: from the earliest active event to `evaluated_at`;
- downy incubation progress (`downy_incubation`: `pct` against its stored `incub_alert_pct`,
  the Montenegro DSS alert) and its expected end (`expected_symptoms`);
- whether the advice's next spray window (`next_window_start`) opens before incubation ends.

Levels (first match wins):

- 3 `Urgent`: uncovered active risk, and incubation at or past the alert stage, or no spray window
  opening before incubation is expected to end;
- 2 `High`: uncovered active risk for at least `UNCOVERED_HOURS` (24 h);
- 1 `Due`: any other `spray`/`wait` (a recent uncovered risk, a powdery pass, a cover running out).

`UNCOVERED_HOURS` is a cut-off with no cited source, and the 3-10 activity span and the
incubation table it builds on carry stored assumptions, so every priority is flagged
`assumption=True`: shown as "assumption, agronomist to confirm".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

ACTIONS = ("spray", "wait")
LABELS = {1: "Due", 2: "High", 3: "Urgent"}
UNCOVERED_HOURS = 24  # assumption, agronomist to confirm
ASSUMPTION_TEXT = "assumption, agronomist to confirm"


@dataclass(frozen=True)
class Priority:
    level: int  # 1 | 2 | 3
    why: str  # one short sentence naming the inputs used
    assumption: bool = True

    @property
    def label(self) -> str:
        return LABELS[self.level]


def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def priority(
    decision: str,
    reasons: Iterable[dict[str, Any]],
    *,
    next_window_start: datetime | None,
    evaluated_at: datetime,
) -> Priority | None:
    """The parcel's priority from one stored advice row, or None when no action is needed."""
    if decision not in ACTIONS:
        return None
    by_rule = {r.get("rule"): r for r in reasons or []}
    risk = by_rule.get("downy_3_10", {})
    cover = by_rule.get("protection_downy", {})
    active = [t for t in map(_ts, (risk.get("inputs") or {}).get("active_events") or []) if t]
    uncovered = (
        risk.get("verdict") == "risk" and bool(active) and cover.get("verdict") != ("protected")
    )
    if not uncovered:
        return Priority(1, "A pass is due; no uncovered infection risk is active.")
    hours = max(0, int((evaluated_at - min(active)).total_seconds() // 3600))
    incubation = by_rule.get("downy_incubation", {})
    pct = _num((incubation.get("inputs") or {}).get("pct"))
    alert = _num((incubation.get("threshold") or {}).get("incub_alert_pct"))
    ends = _ts((incubation.get("inputs") or {}).get("expected_symptoms"))
    past_alert = pct is not None and alert is not None and pct >= alert
    if (incubation.get("inputs") or {}).get("coverage") == "limited":
        level = 3 if past_alert else 2 if hours >= UNCOVERED_HOURS else 1
        stage = (
            "observed-hours lower bound has reached the alert stage"
            if past_alert
            else "incubation stage is unknown"
        )
        window = (
            "no spray window is currently known"
            if next_window_start is None
            else "a spray window is known, but its relation to incubation cannot be evaluated"
        )
        return Priority(
            level,
            f"Unprotected {hours} h after an infection risk; {stage}. "
            f"Incubation timing is not evaluable; {window}.",
        )
    late = ends is not None and (next_window_start is None or next_window_start >= ends)
    stage = "past the alert stage" if past_alert else "before the alert stage"
    lead = f"Unprotected {hours} h after an infection risk; incubation {stage}"
    if past_alert or late:
        tail = "no spray window before incubation ends" if late else "treat first"
        return Priority(3, f"{lead}; {tail}.")
    if hours >= UNCOVERED_HOURS:
        return Priority(2, f"{lead}; a spray window opens before it ends.")
    return Priority(1, f"{lead}; under {UNCOVERED_HOURS} h so far.")


def as_dict(p: Priority | None) -> dict[str, Any]:
    """The JSON/GeoJSON contract: `priority`, `priority_label`, `priority_why`,
    `priority_assumption`."""
    if p is None:
        return {
            "priority": None,
            "priority_label": None,
            "priority_why": None,
            "priority_assumption": False,
        }
    return {
        "priority": p.level,
        "priority_label": p.label,
        "priority_why": p.why,
        "priority_assumption": p.assumption,
    }


def of_advice(advice: Any) -> Priority | None:
    """`priority` of a stored `Advice` row (None: no row)."""
    if advice is None:
        return None
    return priority(
        advice.decision,
        advice.reasons or [],
        next_window_start=advice.next_window_start,
        evaluated_at=advice.evaluated_at,
    )

"""`present_farm_advice`: the one farm-advice summary behind the Today hero and the `/advisor/`
home.

Pure: the latest advice per parcel (evaluated up to now) plus the full parcel list in, a
`FarmAdvice` out. It only sums what the stored advice rows already say; no rule logic.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from farmcore.apps.advisor import priority as prio
from farmcore.apps.advisor import service
from farmcore.apps.advisor.engine.types import WASHED_OFF, floor_hour
from farmcore.apps.advisor.routes import (
    REPLAY_FLAG,
    _because,
    _chip_label,
    _horizon_h,
    _no_window,
    plain_note,
)
from farmcore.core.models.canonical import Advice, Parcel

URGENCY = ("spray", "wait", "protected", service.NO_ADVICE)
ACTIONS = ("spray", "wait")  # decisions whose groups the hero lists
MAX_GROUPS = 3  # more action groups than this: one bounded line, the detail per parcel
HOME_PATH = "/advisor/"
SECTION_TITLES = {"spray": "Spray", "wait": "Wait", "protected": "Protected", "none": "No advice"}


def _day(value: datetime | None, tz: str) -> str:
    """`Sat 13 Apr · 06:00` in the display timezone."""
    if value is None:
        return ""
    return value.astimezone(ZoneInfo(tz)).strftime("%a %-d %b · %H:%M")


@dataclass(frozen=True)
class ParcelRow:
    """One parcel and the facts of its latest advice (or none)."""

    id: int
    alias: str
    name: str = ""
    decision: str = service.NO_ADVICE
    has_advice: bool = False
    assessment_status: str = "legacy-unknown"
    products: tuple[str, ...] = ()
    window_start: datetime | None = None
    window_end: datetime | None = None
    window_open_ended: bool = False  # the window runs to the forecast edge: no real deadline
    window_horizon_h: int | None = None  # the stored look-ahead (`window_horizon_h`), if any
    # Rain the spray window let through at evaluation: the stored sum over the next hours and
    # the limit and look-ahead the advice was evaluated with (None: not stored, e.g. an older
    # row). `rain_open_hour`: the evaluation hour, set only when the window was open then.
    rain_next_mm: float | None = None
    rain_next_max_mm: float | None = None
    rain_lookahead_h: int | None = None
    rain_open_hour: datetime | None = None
    protected_until: datetime | None = None
    reason: str = ""  # plain words, no rule codes
    replay: bool = False  # the observed weather stood in for the forecast
    # Display facts for the parcel lists.
    headline: str = ""
    variety: str | None = None
    area_ha: float = 0.0
    protected_until_text: str = ""
    mm_since_spray: float | None = None
    washed_off: bool = False
    # Priority (N2, `priority.py`): 1 | 2 | 3 for a parcel that needs a pass, else None.
    priority: int | None = None
    priority_label: str | None = None  # "Due", "High", "Urgent"
    priority_why: str | None = None  # one short sentence of the inputs used
    priority_assumption: bool = False  # True: "assumption, agronomist to confirm"

    @property
    def card_path(self) -> str:
        return service.CARD_PATH.format(id=self.id)

    @property
    def chip_label(self) -> str | None:
        return _chip_label(self.has_advice, self.decision, self.assessment_status)


def _open_ended(advice: Advice, by_rule: dict[str, dict[str, Any]]) -> bool:
    """The window ends at the forecast edge the advice was computed with (`window_horizon_h`
    from its evaluation hour), so its end is no deadline."""
    return service.runs_to_forecast_edge(
        advice.next_window_end, advice.evaluated_at, _horizon_h(by_rule)
    )


def _plain_reason(advice: Advice, by_rule: dict[str, dict[str, Any]]) -> str:
    """The why in the card's own words: a pass due -> `_because`; otherwise the stored
    headline's why ("Protected until …: 3.2 mm rain since spray" -> "3.2 mm rain since spray")."""
    if advice.decision in ("spray", "wait"):
        because = _because(by_rule, advice.products or [])
        text = "" if because == "no pass is due" else because
    else:
        headline = (advice.props or {}).get("headline", "")
        _, sep, why = headline.partition(": ")
        text = plain_note(why if sep else headline)
    return text[:1].upper() + text[1:]


def parcel_row(parcel: Parcel, advice: Advice | None, tz: str) -> ParcelRow:
    """A parcel and its latest advice row (None: not evaluated yet) as a `ParcelRow`."""
    base = {
        "id": parcel.id,
        "alias": parcel.alias,
        "name": parcel.name,
        "variety": (parcel.props or {}).get("variety"),
        "area_ha": float(parcel.area_ha),
    }
    if advice is None:
        return ParcelRow(**base)
    props = advice.props or {}
    flags = props.get("flags", [])
    by_rule = {r["rule"]: r for r in advice.reasons or []}
    horizon = _horizon_h(by_rule)
    window = by_rule.get("spray_window", {})
    limits = window.get("threshold", {})
    lookahead = limits.get("window_rain_lookahead_h")
    evaluated_hour = floor_hour(advice.evaluated_at) if advice.evaluated_at else None
    open_at_evaluation = (
        window.get("verdict") == "open"
        and evaluated_hour is not None
        and advice.next_window_start == evaluated_hour
    )
    return ParcelRow(
        **base,
        decision=advice.decision,
        has_advice=True,
        assessment_status=props.get("assessment_status", "legacy-unknown"),
        products=tuple(p["name"] for p in advice.products or [] if p.get("name")),
        window_start=advice.next_window_start,
        window_end=advice.next_window_end,
        window_open_ended=_open_ended(advice, by_rule),
        window_horizon_h=None if horizon is None else int(horizon),
        rain_next_mm=window.get("inputs", {}).get("rain_next_mm"),
        rain_next_max_mm=limits.get("window_rain_next_max_mm"),
        rain_lookahead_h=None if lookahead is None else int(lookahead),
        rain_open_hour=evaluated_hour if open_at_evaluation else None,
        protected_until=advice.protected_until,
        reason=_plain_reason(advice, by_rule),
        replay=REPLAY_FLAG in flags,
        headline=props.get("headline", ""),
        protected_until_text=_day(advice.protected_until, tz),
        mm_since_spray=props.get("mm_since_spray"),
        washed_off=WASHED_OFF in flags,
        **prio.as_dict(prio.of_advice(advice)),
    )


def by_priority(rows):
    """Rows by priority (3 (Urgent) first, no priority last), keeping their order within a level."""
    return sorted(rows, key=lambda r: -(r.priority or 0))


@dataclass(frozen=True)
class Group:
    """Parcels with the same decision, the same products and the same window."""

    decision: str
    rows: tuple[ParcelRow, ...]
    products: tuple[str, ...]
    when: str
    reason: str

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(r.alias for r in self.rows)


@dataclass(frozen=True)
class Section:
    """The `/advisor/` home: the parcels of one decision."""

    decision: str
    title: str
    rows: tuple[ParcelRow, ...]


@dataclass(frozen=True)
class FarmAdvice:
    decision: str
    coverage: tuple[int, int]  # (evaluated, total)
    groups: tuple[Group, ...]
    headline: tuple[str, int | None, str]  # (before, count, after)
    label: str
    awaiting: str  # "1 awaiting advice" when some parcels are not evaluated yet
    reason: str
    protected_until: str  # the first protected cover to end
    replay: bool = False
    rows: tuple[ParcelRow, ...] = ()

    @property
    def sections(self) -> tuple[Section, ...]:
        """Every parcel exactly once, by decision, most urgent first; empty ones left out. Inside
        a decision, by priority, then the given order."""
        out = []
        for d in URGENCY:
            members = tuple(by_priority(r for r in self.rows if r.decision == d))
            if members:
                out.append(Section(d, SECTION_TITLES[d], members))
        return tuple(out)

    @property
    def has_limited(self) -> bool:
        return any(r.assessment_status in ("limited", "unavailable") for r in self.rows)

    @property
    def title(self) -> str:
        before, count, after = self.headline
        return f"{before}{'' if count is None else count}{after}"

    @property
    def bounded(self) -> bool:
        """Too many different actions for one hero: point to each parcel instead."""
        return self.decision in ACTIONS and len(self.groups) > MAX_GROUPS

    @property
    def shown(self) -> tuple[Group, ...]:
        return () if self.bounded else self.groups

    @property
    def link(self) -> str | None:
        return HOME_PATH if self.bounded else None


def _until(r: ParcelRow, now: datetime, tz: str) -> str:
    """A window with a real deadline: "until <end>" while it is open, "from <start> until <end>"
    (the end's day only when it differs) when spraying cannot start yet."""
    end = _day(r.window_end, tz)
    if r.window_start is None or r.window_start <= now or r.window_end is None:
        return f"until {end}"
    zone = ZoneInfo(tz)
    same_day = r.window_start.astimezone(zone).date() == r.window_end.astimezone(zone).date()
    stop = f"{r.window_end.astimezone(zone):%H:%M}" if same_day else end
    return f"from {_day(r.window_start, tz)} until {stop}"


def _rain_note(r: ParcelRow, now: datetime) -> str:
    """The rain an open window still lets through, e.g. "· 0.2 mm rain in next 2 h (under the
    0.5 mm limit)", so "spray now" beside the station's "Rain until …" reads as intended (the
    window's limit, `window_rain_next_max_mm`, is wider than the station's rain flag).

    Only what the stored reason proves: the window was open at the evaluation hour, the rain
    was under the stored limit, and `now` is still inside that hour's look-ahead. Nothing on a
    limited or unavailable assessment, with no rain, or when a value is not stored."""
    if (
        r.assessment_status in service.UNCERTAIN
        or r.rain_open_hour is None
        or not r.rain_lookahead_h
        or r.rain_next_max_mm is None
        or r.rain_next_mm is None
        or not 0 < r.rain_next_mm < r.rain_next_max_mm
        or now >= r.rain_open_hour + timedelta(hours=r.rain_lookahead_h)
    ):
        return ""
    return (
        f" · {round(r.rain_next_mm, 2):g} mm rain in next {r.rain_lookahead_h} h"
        f" (under the {r.rain_next_max_mm:g} mm limit)"
    )


def _when(r: ParcelRow, now: datetime, tz: str) -> str:
    """spray -> "until <end>" ("from <start> until <end>" before it opens), or "window open from
    <start>" / "spray window open now" when the window runs to the forecast edge (its end is
    then not a deadline); wait -> "next window <start>", or no window in the stored horizon,
    in the card's words (`routes._no_window`)."""
    if r.decision == "wait":
        if r.window_start is None:
            phrase = _no_window(r.window_horizon_h)
            return phrase[:1].lower() + phrase[1:]
        return f"next window {_day(r.window_start, tz)}"
    if r.decision != "spray" or r.window_end is None:
        return ""
    if not r.window_open_ended:
        return _until(r, now, tz)
    if r.window_start is not None and r.window_start <= now:
        return "spray window open now" + _rain_note(r, now)
    return f"window open from {_day(r.window_start, tz)}" if r.window_start else ""


def _group(decision: str, rows: list[ParcelRow], now: datetime, tz: str) -> Group:
    first = rows[0]
    reasons = Counter(r.reason for r in rows if r.reason)
    return Group(
        decision=decision,
        rows=tuple(rows),
        products=first.products,
        when=_when(first, now, tz),
        reason=reasons.most_common(1)[0][0] if reasons else "",
    )


def _groups(lead: str, members: list[ParcelRow], now: datetime, tz: str) -> tuple[Group, ...]:
    """Same decision, same product set, same window (and the same words for it) -> one group;
    never a union of products. The most urgent first: the highest priority, then the earliest
    deadline, then the most parcels; inside a group, by priority."""
    by: dict[tuple, list[ParcelRow]] = {}
    for r in by_priority(members):
        key = (
            tuple(sorted(r.products)),
            r.window_start,
            r.window_end,
            r.window_open_ended,
            _when(r, now, tz),
        )
        by.setdefault(key, []).append(r)
    groups = [_group(lead, rows, now, tz) for rows in by.values()]
    far = datetime.max.replace(tzinfo=now.tzinfo)

    def urgency(g: Group) -> tuple:
        end = g.rows[0].window_end
        top = -(g.rows[0].priority or 0)  # rows are by priority: the first is the group's highest
        return (top, g.rows[0].window_open_ended or end is None, end or far, -len(g.rows))

    return tuple(sorted(groups, key=urgency))


def _plural(n: int, word: str = "parcel") -> str:
    return f"{word}{'s' if n != 1 else ''}"


def _headline(
    lead: str, n: int, groups: int, coverage: tuple[int, int]
) -> tuple[str, int | None, str]:
    """(before, count, after): the count is the big number in the hero."""
    evaluated, total = coverage
    if lead in ACTIONS and groups > MAX_GROUPS:
        return "", n, f" {_plural(n)} need treatment — see each parcel"
    if lead == "spray":
        return "Spray ", n, f" {_plural(n)} today"
    if lead == "wait":
        return "Wait: ", n, f" {_plural(n)} at risk"
    if lead == "protected":
        if n < total:
            return "", None, f"{n} of {total} parcels protected"
        return (
            ("", None, "Your parcel is protected")
            if total == 1
            else ("All ", n, " parcels protected")
        )
    if evaluated == 0:
        return "", None, "No advice yet"
    if evaluated < total:
        return "", None, f"No spray advised on {evaluated} evaluated {_plural(evaluated)}"
    return "", None, "Nothing to spray today"


def _label(lead: str, coverage: tuple[int, int]) -> str:
    """The word on the hero's chip; colour never comes alone."""
    evaluated, total = coverage
    if lead != service.NO_ADVICE:
        return {"spray": "Spray", "wait": "Wait", "protected": "Protected"}[lead]
    if evaluated == 0:
        return "No advice yet"
    return "Nothing to do" if evaluated == total else "No spray advised"


def present_farm_advice(rows: list[ParcelRow], now: datetime, tz: str) -> FarmAdvice:
    """The farm's most urgent decision over `rows` (one per parcel, every parcel of the farm),
    its action groups and the words for the hero. Unevaluated parcels are always counted."""
    lead = next((d for d in URGENCY if any(r.decision == d for r in rows)), service.NO_ADVICE)
    members = [r for r in rows if r.decision == lead]
    groups = _groups(lead, members, now, tz)
    evaluated = sum(r.has_advice for r in rows)
    coverage = (evaluated, len(rows))
    waiting = len(rows) - evaluated
    until = [r.protected_until for r in rows if r.decision == "protected" and r.protected_until]
    return FarmAdvice(
        decision=lead,
        coverage=coverage,
        groups=groups,
        headline=(
            ("", None, "Assessment limited or unavailable: required inputs missing")
            if lead == service.NO_ADVICE
            and any(r.assessment_status in ("limited", "unavailable") for r in rows)
            else _headline(lead, len(members), len(groups), coverage)
        ),
        label=(
            "Assessment limited"
            if lead == service.NO_ADVICE
            and any(r.assessment_status in ("limited", "unavailable") for r in rows)
            else _label(lead, coverage)
        ),
        awaiting=f"{waiting} awaiting advice" if waiting and evaluated else "",
        reason=groups[0].reason if groups else "",
        protected_until=_day(min(until), tz) if until else "",
        replay=any(r.replay for r in members),
        rows=tuple(rows),
    )

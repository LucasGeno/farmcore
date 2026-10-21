"""Map panel sections of `advisor`, mounted at `/advisor`.

- `GET /advisor/map/farm`: Farm today's advice: parcels per decision, "N of M parcels
  evaluated" whenever some parcel has no advice (never an all-clear on partial coverage), the
  parcels awaiting advice, and the nearest spray deadline. That deadline is the earliest typed
  `ParcelRow.window_end` of a spray row whose window does not run to the forecast edge and ends
  after the clock; it is never read from `Group.when` (a label: a deadline, a start, "open now"
  or a wait phrase). No finite deadline: no deadline line.
- `GET /advisor/map/parcels/{id}`: the parcel's decision, cover until / washed off, rain since
  the spray (None: "no rain reading", never 0), stated weather gaps, the card link. A stored
  window the clock has reached reads "ended …" / "no longer known …", never "open now". No advice:
  "Awaiting advice", apart from an evaluated "No spray needed". Another farm's parcel: 404.

Plain HTML fragments (a `<section>`, no page chrome). Reads the stored advice up to
`clock.now()` through the request's `Repo` (`?farm=` before the cookie), as the `/advisor/`
home; no rule logic. Links keep the farm query.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import APIRouter, Path, Request
from fastapi.responses import HTMLResponse

from farmcore.apps.advisor import service
from farmcore.apps.advisor.engine.types import DATA_GAP
from farmcore.apps.advisor.presenter import HOME_PATH, ParcelRow, _day, _when, parcel_row
from farmcore.apps.advisor.routes import PARCEL_SCOPE, _now, _parcel, unavailable_view
from farmcore.apps.fields.seed import alias_number
from farmcore.core.access import Repo
from farmcore.core.models.canonical import Advice, Parcel
from farmcore.core.navigation import map_read_url, resolved_url, workspace_parent
from farmcore.web import render

router = APIRouter()

DECISIONS = ("spray", "wait", "protected", service.NO_ADVICE)
NO_SPRAY_NEEDED = "No spray needed"  # an evaluated `none`
AWAITING = "Awaiting advice"  # no advice row up to the clock
NO_RAIN_READING = "no rain reading"
# A parcel id the database can hold (bigint); anything else is a 422, never a 500.
ParcelId = Annotated[int, Path(ge=1, le=2**63 - 1)]


def nearest_spray_deadline(
    rows: Iterable[ParcelRow], now: datetime
) -> tuple[datetime, tuple[str, ...]] | None:
    """(the earliest finite spray deadline after `now`, the parcels with that deadline)."""
    ends = [
        r
        for r in rows
        if r.decision == "spray"
        and r.window_end is not None
        and not r.window_open_ended
        and r.window_end > now
    ]
    if not ends:
        return None
    first = min(r.window_end for r in ends if r.window_end is not None)
    return first, tuple(r.alias for r in ends if r.window_end == first)


def _plural(n: int) -> str:
    return f"{n} parcel{'s' if n != 1 else ''}"


@router.get("/map/farm", response_class=HTMLResponse)
def farm_section(request: Request, repo: Repo) -> HTMLResponse:
    tz = request.app.state.settings.display_tz
    now = _now(request, repo)
    availability = service.current_availability(repo, now, tz)
    if (unavailable := unavailable_view(request, repo, availability=availability)) is not None:
        return unavailable
    parcels = sorted(
        repo.list(Parcel, scope=PARCEL_SCOPE), key=lambda p: (alias_number(p.alias), p.alias)
    )
    latest = service.latest_advice(repo, now, tz=tz, availability=availability)
    rows = [parcel_row(p, latest.get(p.id), tz) for p in parcels]
    evaluated = [r for r in rows if r.has_advice]
    counts = [
        (d, NO_SPRAY_NEEDED if d == service.NO_ADVICE else None, _plural(n))
        for d in DECISIONS
        if (
            n := sum(
                r.decision == d and r.assessment_status not in ("limited", "unavailable")
                for r in evaluated
            )
        )
    ]
    limited = [r for r in evaluated if r.assessment_status in ("limited", "unavailable")]
    if limited:
        counts.append(("none", "Assessment limited", _plural(len(limited))))
    waiting = len(rows) - len(evaluated)
    deadline = nearest_spray_deadline(rows, now)
    return render(
        request,
        "advisor/_map_farm.html",
        limited=limited,
        total=len(rows),
        evaluated=len(evaluated),
        counts=counts,
        awaiting=_plural(waiting) if waiting else "",
        awaiting_label=AWAITING,
        deadline=(_day(deadline[0], tz), ", ".join(deadline[1])) if deadline else None,
        home=HOME_PATH,
    )


def _rain_gap(advice: Advice) -> bool:
    """The downy cover's rain sum since the spray missed station hours (`rain_data_gap`)."""
    for reason in advice.reasons or []:
        if reason.get("rule") == "protection_downy":
            return bool((reason.get("inputs") or {}).get("rain_data_gap"))
    return False


def _rain_text(mm: float | None, gap: bool) -> str:
    """Rain since the spray in plain words; unknown is never 0 mm."""
    if mm is None:
        return NO_RAIN_READING
    if gap:
        if mm <= 0:
            return f"{NO_RAIN_READING} (station hours missing)"
        return f"at least {mm:.1f} mm (station hours missing)"
    return f"{mm:.1f} mm"


def _window_text(row: ParcelRow, evaluated_at: datetime, now: datetime, tz: str) -> str:
    """The window line: the presenter's own words while the stored window still ends after the
    clock; once the clock reaches its end, no "open now" / "until" / "from" wording (display
    only, the decision itself is the stored advice): "ended <end>" for a finite window, "no
    longer known" for one that ran to its stored forecast edge."""
    if row.window_end is not None and row.window_end <= now:
        if row.window_open_ended:
            return f"no longer known — advice is from {_day(evaluated_at, tz)}"
        return f"ended {_day(row.window_end, tz)}"
    return _when(row, now, tz)


@router.get("/map/parcels/{parcel_id}", response_class=HTMLResponse)
def parcel_section(parcel_id: ParcelId, request: Request, repo: Repo) -> HTMLResponse:
    parcel = _parcel(repo, parcel_id)  # 404 outside the request's farm
    if (unavailable := unavailable_view(request, repo, parcel=parcel)) is not None:
        return unavailable
    tz = request.app.state.settings.display_tz
    now = _now(request, repo)
    advice = service.latest_advice(repo, now, tz=tz, parcel_id=parcel.id).get(parcel.id)
    row = parcel_row(parcel, advice, tz)
    view: dict[str, Any] = {"row": row, "card": row.card_path}
    if advice is not None:
        flags = (advice.props or {}).get("flags", [])
        view.update(
            chip_label=(
                "Assessment " + row.assessment_status
                if row.assessment_status in ("limited", "unavailable")
                else NO_SPRAY_NEEDED
                if row.decision == service.NO_ADVICE
                else None
            ),
            rain=_rain_text(row.mm_since_spray, _rain_gap(advice)),
            weather_gap=DATA_GAP in flags,
            when=_window_text(row, advice.evaluated_at, now, tz),
        )
    else:
        view.update(chip_label=AWAITING)
    parent = workspace_parent(request, repo, parcel.id, "overview")
    detail_url = map_read_url(request, f"/advisor/parcels/{parcel.id}", parcel.id)
    poll_url = resolved_url(request, f"/advisor/map/parcels/{parcel.id}")
    if parent:
        parts = urlsplit(parent)
        plain = parts._replace(
            query=urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != "return_to"])
        ).geturl()
        detail_url = (
            resolved_url(request, f"/advisor/parcels/{parcel.id}")
            + "&"
            + urlencode({"return_to": plain})
        )
        poll_url += "&" + urlencode({"parent": parent})
    return render(
        request, "advisor/_map_parcel.html", **view, detail_url=detail_url, poll_url=poll_url
    )

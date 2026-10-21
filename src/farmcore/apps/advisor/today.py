"""`GET /advisor/today`: the Today fragment. One answer first ("Spray 8 parcels today, copper,
until Sat 13 Apr 06:00"), then the parcels that need it, then the protected ones.

Reads the latest advice per parcel up to `clock.now()` through the request's `Repo`, like the
advisor list. No new rule logic: the hero is `presenter.present_farm_advice` over the stored
advice rows, the same summary as the `/advisor/` home.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from farmcore.apps.advisor import service
from farmcore.apps.advisor.navigation import parcel_link
from farmcore.apps.advisor.presenter import _day, by_priority, parcel_row, present_farm_advice
from farmcore.apps.advisor.routes import (
    FLAG_LABELS,
    PARCEL_SCOPE,
    REPLAY_FLAG,
    _now,
    unavailable_view,
)
from farmcore.apps.fields.seed import alias_number
from farmcore.core.access import Repo
from farmcore.core.models.canonical import Parcel
from farmcore.core.tour import is_tour
from farmcore.web import render

router = APIRouter()


@router.get("/today", response_class=HTMLResponse)
def today_fragment(
    request: Request, repo: Repo, view: Literal["full", "attention", "calm"] = "full"
) -> HTMLResponse:
    tz = request.app.state.settings.display_tz
    now = _now(request, repo)
    unavailable = service.current_availability(repo, now, tz)[1]
    if unavailable and view == "full":
        return unavailable_view(request, repo)
    if unavailable:
        return render(request, "advisor/_today_home.html", view=view, unavailable=unavailable)
    tz = request.app.state.settings.display_tz
    now = _now(request, repo)
    parcels = sorted(
        repo.list(Parcel, scope=PARCEL_SCOPE), key=lambda p: (alias_number(p.alias), p.alias)
    )
    latest = service.latest_advice(repo, now, tz=tz)
    rows = [parcel_row(p, latest.get(p.id), tz) for p in parcels]
    return render(
        request,
        "advisor/_today.html" if view == "full" else "advisor/_today_home.html",
        view=view,
        workspace_links={
            p.id: parcel_link(request, p.id, f"/#home-parcel-{p.id}") for p in parcels
        },
        now_text=_day(now, tz),
        advice_checked=min(
            (latest[p.id].evaluated_at for p in parcels if p.id in latest), default=None
        ),
        brief_warnings=sorted(
            {
                (flag, FLAG_LABELS.get(flag, flag))
                for p in parcels
                if p.id in latest
                for flag in (latest[p.id].props or {}).get("flags", [])
                if flag != REPLAY_FLAG
            }
        ),
        hero=present_farm_advice(rows, now, tz),
        act=by_priority(r for r in rows if r.decision in ("spray", "wait")),
        protected=[r for r in rows if r.decision == "protected"],
        calm=[
            r
            for r in rows
            if r.has_advice
            and r.decision == service.NO_ADVICE
            and r.assessment_status not in ("limited", "unavailable")
        ],
        limited=[r for r in rows if r.assessment_status in ("limited", "unavailable")],
        unknown=[r for r in rows if not r.has_advice],
        washed=[r for r in rows if r.washed_off],
        bot_url=bot_url(request),
    )


def bot_url(request: Request) -> str | None:
    """The Telegram bot link for "Log a spray", when a bot is configured."""
    if is_tour(request):
        return None
    bot = request.app.state.settings.telegram_bot_username
    return f"https://t.me/{bot.lstrip('@')}" if bot else None

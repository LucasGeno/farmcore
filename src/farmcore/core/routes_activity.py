"""`GET /activity/today`: the Recent activity card on Today (core, `Widget` order 40, full).

Rows come from `core.activity.recent_activity`: the farm's latest events up to clock now,
grouped so one spray over many parcels is one row. An app that is not loaded, or is switched
off on the Apps page, shows no rows and gets no links.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from farmcore.core import registry
from farmcore.core.access import Repo
from farmcore.core.activity import ACTIVITY, DIARY, recent_activity
from farmcore.core.clock import get_clock
from farmcore.web import render

router = APIRouter()


def live_apps(request: Request) -> set[str]:
    """Names of the loaded apps that are not switched off on the Apps page."""
    return {m.name for m in registry.visible_manifests(request)}


@router.get("/activity/today", response_class=HTMLResponse)
def activity_today(request: Request, repo: Repo) -> HTMLResponse:
    settings = request.app.state.settings
    now = get_clock(repo.session, settings).now()
    apps = live_apps(request)
    return render(
        request,
        "core/_activity.html",
        rows=recent_activity(repo, now, settings.display_tz, apps),
        see_all=ACTIVITY if "activity" in apps else DIARY if "operations" in apps else None,
    )

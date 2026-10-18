from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from farmcore.apps.forecast import availability
from farmcore.apps.notifications import preferences, service
from farmcore.core.access import Repo
from farmcore.core.clock import get_clock
from farmcore.core.registry import visible_manifests
from farmcore.core.urls import with_farm
from farmcore.web import render

router = APIRouter()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse, include_in_schema=False)
def notifications(request: Request, repo: Repo):
    visible = {m.name for m in visible_manifests(request)}
    if "notifications" not in visible:
        return HTMLResponse("")
    settings = request.app.state.settings
    now = get_clock(repo.session, settings).now()
    items = service.items(
        repo,
        now,
        settings.display_tz,
        visible,
        busy_days=availability.parse(
            request.cookies.get(availability.COOKIE.format(farm_id=repo.farm_id)),
            now.astimezone(ZoneInfo(settings.display_tz)).date(),
        ),
    )
    cookie_name = preferences.COOKIE.format(farm_id=repo.farm_id)
    read = preferences.read_keys(request.cookies.get(cookie_name))
    unread = sum(i.severity in {"red", "amber"} and i.key not in read for i in items)
    return render(
        request,
        "notifications/_bell.html",
        items=items,
        read=read,
        unread_count=unread,
        cookie_name=cookie_name,
        refresh_url=with_farm("/notifications", repo.farm_id),
    )

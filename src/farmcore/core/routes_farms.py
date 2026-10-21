"""Farm switcher: `GET /farms/switcher` (navbar partial) and `GET /farms/select`.

- `/farms/switcher` lists the farms with a live owner grant (id and name only; no farm data, so it
  reads the session directly like `/clock`) with the selected one marked; `next` for the select
  form is the page the partial was loaded on (htmx `HX-Current-URL`, path and query), else `/`.
- `/farms/select?farm=<id>&next=<path>` sets cookie `farmcore_farm` (HttpOnly, SameSite=Lax,
  path `/`, Secure over https) and redirects 303 to `next` when it is a local path (starts with
  `/`, not `//` or `/\\`), else to `/`. A farm that does not exist or has no live owner grant is
  a 404.

Every `Repo` route then reads the selected farm (`access.selected_farm`); jobs, chat and the bot
keep the default farm.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.core.access import (
    FARM_COOKIE,
    FarmNotSeeded,
    authorize_farm_read,
    owned_farms,
    selectable_farm,
    selected_farm,
)
from farmcore.core.identity import principal_for
from farmcore.core.models.canonical import Farm
from farmcore.core.navigation import farm_switch_parent
from farmcore.core.urls import with_farm
from farmcore.db import get_session
from farmcore.web import operator_authenticated, render

router = APIRouter()

COOKIE_MAX_AGE = 60 * 60 * 24 * 365  # one year: a convenience, not a credential


def safe_next(value: str | None) -> str:
    """`value` if it is a local path, else `/` (no open redirect)."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return "/"
    return value


def _current_path(request: Request) -> str:
    current = request.headers.get("HX-Current-URL")
    if not current:
        return "/"
    parts = urlsplit(current)
    local = parts._replace(scheme="", netloc="", fragment="").geturl()
    return safe_next(local or "/")


@router.get("/farms/switcher", response_class=HTMLResponse)
def switcher(
    request: Request, slot: str = "header", session: Session = Depends(get_session)
) -> HTMLResponse:
    principal = principal_for(request)
    farms = (
        list(
            session.scalars(
                select(Farm).where(Farm.id.in_(principal.memberships)).order_by(Farm.id)
            )
        )
        if principal and not operator_authenticated(request)
        else owned_farms(session)
    )
    if not principal and not operator_authenticated(request):
        farms = [f for f in farms if f.id in request.app.state.settings.demo_farm_ids]
    try:
        selected_id = selected_farm(request, session, request.app.state.settings).id
    except FarmNotSeeded:
        selected_id = None
    return render(
        request,
        "core/_farm_switcher.html",
        switcher_view=True,
        slot="more" if slot == "more" else "header",
        farms=[{"id": f.id, "name": f.name} for f in farms],
        selected_id=selected_id,
        next_path=_current_path(request),
    )


@router.get("/farms/select")
def select_farm(
    request: Request, farm: str, next: str | None = None, session: Session = Depends(get_session)
) -> RedirectResponse:
    principal = principal_for(request)
    if principal and not operator_authenticated(request):
        try:
            farm_id = int(farm)
        except ValueError:
            raise HTTPException(403, "Farm access denied.") from None
        if farm_id not in principal.memberships:
            # PrivacyMiddleware already denies every nonempty foreign selector; defense
            # for a future caller that mounts this router without the application boundary.
            raise HTTPException(403, "Farm access denied.")  # pragma: no cover
        chosen = session.get(Farm, farm_id)
    else:
        chosen = selectable_farm(session, farm)
    if chosen is None:
        raise HTTPException(status_code=404, detail="farm not found")
    authorize_farm_read(request, chosen.id, request.app.state.settings)
    target = safe_next(next)
    if parent := farm_switch_parent(target):
        target = with_farm(parent, chosen.id)
    elif any(key == "farm" for key, _ in parse_qsl(urlsplit(target).query)):
        target = with_farm(target, chosen.id)
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        FARM_COOKIE,
        str(chosen.id),
        max_age=COOKIE_MAX_AGE,
        path="/",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response

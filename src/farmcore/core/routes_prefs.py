"""`/prefs/expert`: switch the viewer's "Expert view" on or off (a cookie), then go back.

Expert view only changes what is shown first (expert tools open in the sidebar, model details
on cards). It grants nothing: admin controls still need the admin token.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from farmcore.core.routes_farms import safe_next
from farmcore.web import EXPERT_COOKIE, SUNLIGHT_COOKIE

router = APIRouter()
ONE_YEAR = 365 * 24 * 3600


def _toggle(request: Request, cookie: str, on: int, next: str) -> RedirectResponse:
    resp = RedirectResponse(safe_next(next), status_code=303)
    if on:
        resp.set_cookie(
            cookie,
            "1",
            max_age=ONE_YEAR,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
        )
    else:
        resp.delete_cookie(cookie)
    return resp


@router.get("/prefs/expert")
def set_expert(request: Request, on: int = 1, next: str = "/") -> RedirectResponse:
    return _toggle(request, EXPERT_COOKIE, on, next)


@router.get("/prefs/sunlight")
def set_sunlight(request: Request, on: int = 1, next: str = "/") -> RedirectResponse:
    """Sunlight mode: high contrast, larger text. Changes looks only, grants nothing."""
    return _toggle(request, SUNLIGHT_COOKIE, on, next)

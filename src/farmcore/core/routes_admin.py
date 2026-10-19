"""`/admin/sign-in` and `/admin/sign-out`: the administrator becomes admin without hand-setting
a cookie.

- `GET /admin/sign-in`: a POST-only form with one password field (autocomplete off). It says
  when no `admin_token` is configured (private access stays closed) and when tour
  mode disables it. No nav entry links here: the administrator bookmarks it.
- `POST /admin/sign-in`: same-origin only (`routes_demo.same_origin`); refused in tour mode (the
  tour middleware already refuses every non-GET); `hmac.compare_digest` against
  `settings.admin_token`; on a match the `admin_token` cookie is set HttpOnly, Secure,
  SameSite=Strict, Path=/ (the cookie `web.is_admin` already reads) and the response is a 303 to
  `/demo/`. A wrong token is a 403 that sets nothing. Every failed attempt waits `FAIL_DELAY_S`;
  wrong attempts are limited per client (`_client_key`, in memory, `MAX_FAILURES` per
  `WINDOW_S`), then 429 until the window passes. The right token always signs in, even while
  that limit is tripped: behind a proxy many visitors can share one key, and the administrator must
  never be locked out by someone else's wrong guesses.
- `POST /admin/sign-out`: same-origin; clears the cookie.

CSRF: the cookie is SameSite=Strict and both actions are same-origin POSTs. The token is never
echoed, logged or put in a URL: the form is never refilled and failures log nothing but a count.
"""

from __future__ import annotations

import asyncio
import hmac
from collections import OrderedDict, deque
from threading import Lock
from time import monotonic
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from farmcore.core.routes_demo import same_origin
from farmcore.core.tour import is_tour
from farmcore.web import is_admin, render

router = APIRouter()

COOKIE = "admin_token"  # the cookie `web.is_admin` reads
COOKIE_MAX_AGE = 12 * 3600  # 12 h; sign in again after
MAX_FAILURES = 5
WINDOW_S = 600
MAX_CLIENTS = 1024
FAIL_DELAY_S = 0.5  # every failed attempt: brute force stays slow even with a spoofed key
_sleep = asyncio.sleep  # tests replace it; nothing real waits in tests
_lock = Lock()


def _configured(request: Request):
    return request.app.state.settings.admin_token


def _client_key(request: Request) -> str:
    """The limiter key: Cloudflare's `CF-Connecting-IP`, else the first `X-Forwarded-For` hop,
    else the socket peer. Both headers can be spoofed by a client that reaches the app directly;
    that only lets an attacker dodge their own limit (and still pay `FAIL_DELAY_S` per guess).
    Security rests on the token's entropy, not on this limit."""
    cf = request.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded:
        return forwarded
    return request.client.host if request.client else "unknown"


def _failures(request: Request) -> deque:
    """This client's failed attempts inside the window (pruned)."""
    if not hasattr(request.app.state, "admin_sign_in_failures"):
        request.app.state.admin_sign_in_failures = OrderedDict()
    buckets = request.app.state.admin_sign_in_failures
    key = _client_key(request)
    times = buckets.pop(key, deque())
    now = monotonic()
    while times and times[0] <= now - WINDOW_S:
        times.popleft()
    buckets[key] = times
    while len(buckets) > MAX_CLIENTS:
        buckets.popitem(last=False)
    return times


def _page(request: Request, status: int = 200, error: str = "") -> HTMLResponse:
    response = render(
        request,
        "core/admin_sign_in.html",
        configured=_configured(request) is not None,
        tour=is_tour(request),
        signed_in=_configured(request) is not None and is_admin(request),
        error=error,
    )
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/admin/sign-in", response_class=HTMLResponse)
def sign_in_page(request: Request) -> HTMLResponse:
    return _page(request)


@router.post("/admin/sign-in", dependencies=[Depends(same_origin)])
async def sign_in(request: Request, token: Annotated[str, Form(max_length=512)] = ""):
    if is_tour(request):
        raise HTTPException(status_code=403, detail="Sign-in is disabled in tour mode.")
    expected = _configured(request)
    if expected is None:
        return _page(request, 409, "No admin token is configured on this server.")
    if hmac.compare_digest(token.encode(), expected.get_secret_value().encode()):
        # Always: a tripped limit throttles wrong guesses only, never the administrator.
        response = RedirectResponse("/demo/", status_code=303)
        response.set_cookie(
            COOKIE,
            token,
            max_age=COOKIE_MAX_AGE,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response
    await _sleep(FAIL_DELAY_S)
    with _lock:
        failures = _failures(request)
        limited = len(failures) >= MAX_FAILURES
        if not limited:
            failures.append(monotonic())
    if limited:
        response = _page(request, 429, "Too many failed attempts. Try again later.")
        response.headers["Retry-After"] = str(WINDOW_S)
        return response
    return _page(request, 403, "That token is not right.")


@router.post("/admin/sign-out", dependencies=[Depends(same_origin)])
def sign_out(request: Request) -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response

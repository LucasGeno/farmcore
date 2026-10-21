"""Plain-words HTML error pages for browser requests.

`register(app)` installs two handlers. A request that accepts `text/html` gets
`core/error.html` (extends `base.html`) with a plain message; everything else keeps FastAPI's
JSON body and status. Only the exception's `detail` (or, for a validation error, the bad value
and the parameter's name) is shown, never a traceback.

- `HTTPException`: a 404 shows its detail ("No station with id 7."), or "Page not found." for an
  unknown address; a 503 on an unseeded farm names the seed command.
- `RequestValidationError` (a path or query value of the wrong type, e.g. `/stations/NOPE`):
  "“NOPE” is not a valid station id: it should be a whole number.", status 422.

`refusal(request, status, detail)` is the same choice for the refusals the HTTP middlewares
answer before routing (privacy, tour). A signed-out browser asked to authenticate is sent to
`/login?next=<this page>` (303) instead; JSON clients keep the 403 body.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.exception_handlers import (
    http_exception_handler as fastapi_http_exception_handler,
)
from fastapi.exception_handlers import (
    request_validation_exception_handler as fastapi_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.utils import is_body_allowed_for_status_code
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, RedirectResponse, Response

from farmcore.core.access import FarmNotSeeded
from farmcore.web import is_sunlight, operator_authenticated, render, templates

AUTH_PATHS = frozenset({"/login", "/admin/sign-in", "/admin/sign-out"})
AUTH_ERROR = "Sign-in request refused. Open the sign-in form and try again."

DATA_SUFFIXES = (".json", ".csv", ".geojson")
NO_FARM = "No farm data yet. Run `farmcore seed` (or `seed --scene B`)."
NO_PAGE = "Page not found."
AUTH_REQUIRED = "Authentication required."
FARM_DENIED = "Farm access denied."
# refusal detail -> what a browser shows (the JSON body keeps the detail)
PLAIN = {
    AUTH_REQUIRED: "Your account cannot open this page.",  # signed out: the sign-in redirect
    FARM_DENIED: "Your account has no access to that farm.",
    "Installation operator required.": "Only the installation operator can open this page.",
    "Tour mode is read-only.": "The tour is read-only, so this action is not available here.",
}
STARLETTE_404 = "Not Found"  # the detail Starlette gives an unknown route
LOC_SOURCES = ("path", "query", "header", "cookie", "body")
MAX_SHOWN = 40  # characters of a bad value shown back
# pydantic error type prefix -> what the value should be
EXPECTED = {"int": "a whole number", "float": "a number", "bool": "yes or no", "date": "a date"}


def _message(exc: StarletteHTTPException) -> str:
    if exc.status_code == 503 and isinstance(exc.__cause__, FarmNotSeeded):
        return NO_FARM
    if exc.status_code == 404:
        detail = str(exc.detail or "")
        if not detail or detail == STARLETTE_404:
            return NO_PAGE
        return detail[0].upper() + detail[1:]
    if exc.detail in PLAIN:
        return PLAIN[exc.detail]
    return f"{exc.status_code}: {exc.detail}"


def _validation_line(error: dict[str, Any]) -> str:
    name = " ".join(str(p) for p in error.get("loc", ()) if p not in LOC_SOURCES)
    name = name.replace("_", " ") or "value"
    if error.get("type") == "missing":
        return f"The {name} is missing."
    shown = str(error.get("input", ""))
    if len(shown) > MAX_SHOWN:
        shown = shown[:MAX_SHOWN] + "…"
    kind = str(error.get("type", "")).split("_")[0]
    expected = EXPECTED.get(kind)
    reason = f"it should be {expected}" if expected else str(error.get("msg", "")).lower()
    return f"“{shown}” is not a valid {name}: {reason}."


def wants_html(request: Request) -> bool:
    """A browser page load: `Accept` names `text/html`, not an HTMX partial, not a data path."""
    if request.headers.get("hx-request") or request.url.path.endswith(DATA_SUFFIXES):
        return False
    return "text/html" in request.headers.get("accept", "")


def _page(request: Request, status_code: int, message: str) -> Response:
    resp = render(request, "core/error.html", status_code=status_code, message=message)
    resp.status_code = status_code
    return resp


def _signed_out(request: Request) -> bool:
    from farmcore.core.identity import principal_for

    return principal_for(request) is None and not operator_authenticated(request)


def login_redirect(request: Request) -> Response:
    """303 to `/login`, with this page as `next` for a GET (a form post is not replayed)."""
    url = "/login"
    if request.method in {"GET", "HEAD"} and request.url.path != "/":
        here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        url += "?" + urlencode({"next": here})
    return RedirectResponse(url, status_code=303, headers={"Cache-Control": "no-store"})


def _refusal_page(request: Request, status: int, detail: Any, message: str) -> Response:
    """A browser's answer to a refusal: the sign-in redirect when a signed-out visitor must
    authenticate (or, outside a demo, is refused at all: they never get the private shell),
    else the error page with a link home."""
    demo = getattr(request.app.state.settings, "demo_mode", False)
    if status == 403 and _signed_out(request) and (detail == AUTH_REQUIRED or not demo):
        return login_redirect(request)
    if detail == FARM_DENIED:
        # The refused selection must not follow the page's links (`farm_url`).
        request.state.selected_farm_id = None
    return _page(request, status, message)


def refusal(request: Request, status: int, detail: str) -> Response:
    """A refusal answered before routing (privacy, tour): JSON `{"detail": …}` for an API
    client, the browser page otherwise. The status is the same either way."""
    if not wants_html(request):
        return JSONResponse({"detail": detail}, status_code=status)
    response = _refusal_page(request, status, detail, PLAIN.get(detail, f"{status}: {detail}"))
    response.headers["Cache-Control"] = "no-store"
    return response


def _auth_error(request: Request, status: int) -> Response:
    """Credential parsing errors never echo inputs or load the private application shell."""
    headers = {"Cache-Control": "no-store"}
    if not wants_html(request):
        return JSONResponse({"detail": AUTH_ERROR}, status_code=status, headers=headers)
    return templates.TemplateResponse(
        request,
        "core/auth_error.html",
        {
            "message": AUTH_ERROR,
            "return_path": "/login" if request.url.path == "/login" else "/admin/sign-in",
            "sunlight": is_sunlight(request),
        },
        status_code=status,
        headers=headers,
    )


def _grant_context(request: Request) -> bool:
    return request.url.path == "/share/received" or request.url.path.startswith("/share/as/")


def _grant_error(request: Request, status: int) -> Response:
    """Denied recipient pages reveal no grant metadata and load no private farm widgets."""
    return templates.TemplateResponse(
        request,
        "core/recipient_error.html",
        {"status_code": status, "sunlight": is_sunlight(request)},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    if request.url.path in AUTH_PATHS:
        return _auth_error(request, exc.status_code)
    if not wants_html(request) or not is_body_allowed_for_status_code(exc.status_code):
        return await fastapi_http_exception_handler(request, exc)
    resp = (
        _grant_error(request, exc.status_code)
        if _grant_context(request)
        else _refusal_page(request, exc.status_code, exc.detail, _message(exc))
    )
    resp.headers.update(exc.headers or {})
    return resp


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
    if request.url.path in AUTH_PATHS:
        return _auth_error(request, 422)
    if not wants_html(request):
        return await fastapi_validation_exception_handler(request, exc)
    if _grant_context(request):
        return _grant_error(request, 422)
    return _page(request, 422, " ".join(_validation_line(e) for e in exc.errors()))


def register(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]

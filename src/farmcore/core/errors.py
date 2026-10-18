"""Plain-words HTML error pages for browser requests.

`register(app)` installs two handlers. A request that accepts `text/html` gets
`core/error.html` (extends `base.html`) with a plain message; everything else keeps FastAPI's
JSON body and status. Only the exception's `detail` (or, for a validation error, the bad value
and the parameter's name) is shown, never a traceback.

- `HTTPException`: a 404 shows its detail ("No station with id 7."), or "Page not found." for an
  unknown address; a 503 on an unseeded farm names the seed command.
- `RequestValidationError` (a path or query value of the wrong type, e.g. `/stations/NOPE`):
  "“NOPE” is not a valid station id: it should be a whole number.", status 422.
"""

from __future__ import annotations

from typing import Any

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
from starlette.responses import Response

from farmcore.core.access import FarmNotSeeded
from farmcore.web import render

DATA_SUFFIXES = (".json", ".csv", ".geojson")
NO_FARM = "No farm data yet. Run `farmcore seed` (or `seed --scene B`)."
NO_PAGE = "Page not found."
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


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    if not wants_html(request) or not is_body_allowed_for_status_code(exc.status_code):
        return await fastapi_http_exception_handler(request, exc)
    resp = _page(request, exc.status_code, _message(exc))
    resp.headers.update(exc.headers or {})
    return resp


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
    if not wants_html(request):
        return await fastapi_validation_exception_handler(request, exc)
    return _page(request, 422, " ".join(_validation_line(e) for e in exc.errors()))


def register(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_exception_handler)  # type: ignore[arg-type]

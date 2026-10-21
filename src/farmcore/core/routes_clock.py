"""`/clock`: the navbar replay control (mode, local time, step-to form, presets, last step)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse

from farmcore.core import jobs
from farmcore.core.clock import clock_row, get_clock, parse_local
from farmcore.db import get_session
from farmcore.settings import Settings
from farmcore.web import is_admin, render

router = APIRouter()

# The two hero scenes: (label, local `datetime-local` value).
PRESETS: tuple[tuple[str, str], ...] = (
    ("10 Apr 06:00", "2024-04-10T06:00"),
    ("15 May 06:00", "2024-05-15T06:00"),
)
INPUT_FORMAT = "%Y-%m-%dT%H:%M"


def require_admin(request: Request) -> None:
    """Require a configured `admin_token` as `X-Admin-Token` header or `admin_token` cookie."""
    if not is_admin(request):
        raise HTTPException(status_code=403, detail="admin token required")


def _partial(request: Request, session: Session) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    row = clock_row(session, settings)
    now = get_clock(session, settings).now()
    last_step: dict[str, Any] | None = row.job_runs.get("last_step")
    return render(
        request,
        "core/_replay_control.html",
        clock_view=True,
        mode=row.mode,
        now=now,
        tz=settings.display_tz,
        input_value=now.astimezone(ZoneInfo(settings.display_tz)).strftime(INPUT_FORMAT),
        presets=PRESETS,
        last_step_to=datetime.fromisoformat(last_step["to"]) if last_step else None,
        last_runs=last_step["runs"] if last_step else [],
    )


@router.get("/clock", response_class=HTMLResponse)
def clock_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
    return _partial(request, session)


@router.post("/clock/step", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
def clock_step(request: Request, to: str = Form(...)) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    try:
        t = parse_local(to, settings.display_tz)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session_factory = request.app.state.session_factory
    jobs.step_to(session_factory, request.app.state.manifests, t, settings)
    with session_factory() as session:
        response = _partial(request, session)
    response.headers["HX-Refresh"] = "true"
    return response

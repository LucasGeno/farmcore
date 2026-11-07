"""Machine/operator configuration forms: owner-only writes, farm-scoped reads on the page."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from farmcore.apps.planning import resources
from farmcore.core.access import Repo, require_farm_owner
from farmcore.core.clock import get_clock, parse_local

router = APIRouter()

_TRUE = {"1", "true", "on", "yes"}
_FALSE = {"0", "false", "off", "no"}


def _now(request: Request, repo: Repo) -> datetime:
    return get_clock(repo.session, request.app.state.settings).now()


def _tz(request: Request) -> str:
    return request.app.state.settings.display_tz


def _back(repo: Repo, error: str | None = None) -> RedirectResponse:
    values = {"farm": repo.farm_id}
    if error:
        values["error"] = error[:500]
    return RedirectResponse("/planning/?" + urlencode(values), status_code=303)


def _available(value: object) -> bool:
    text = str(value or "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise resources.ResourceError("Choose whether the window is available or busy.")


@router.post("/resources/machines-form", dependencies=[Depends(require_farm_owner)])
async def machine_form(request: Request, repo: Repo):
    form = await request.form()
    try:
        resources.create_machine(
            repo,
            _now(request, repo),
            name=str(form.get("name", "")),
            kind=str(form.get("kind", "")),
            work_rate_ha_per_hour=form.get("work_rate_ha_per_hour", ""),
            tank_litres=form.get("tank_litres", ""),
            notes=str(form.get("notes", "")),
        )
    except (resources.ResourceError, ValueError) as exc:
        return _back(repo, f"Check the machine: {exc}")
    return _back(repo)


@router.post("/resources/operators-form", dependencies=[Depends(require_farm_owner)])
async def operator_form(request: Request, repo: Repo):
    form = await request.form()
    try:
        resources.create_operator(
            repo,
            _now(request, repo),
            name=str(form.get("name", "")),
            notes=str(form.get("notes", "")),
        )
    except (resources.ResourceError, ValueError) as exc:
        return _back(repo, f"Check the operator: {exc}")
    return _back(repo)


@router.post("/resources/windows-form", dependencies=[Depends(require_farm_owner)])
async def window_form(request: Request, repo: Repo):
    form = await request.form()
    tz = _tz(request)
    try:
        kind, resource_id = resources.parse_resource(form.get("resource", ""))
        resources.create_window(
            repo,
            _now(request, repo),
            kind=kind,
            resource_id=resource_id,
            starts_at=parse_local(str(form.get("starts_at", "")), tz),
            ends_at=parse_local(str(form.get("ends_at", "")), tz),
            available=_available(form.get("available", "")),
            notes=str(form.get("notes", "")),
        )
    except (resources.ResourceError, ValueError) as exc:
        return _back(repo, f"Check the availability: {exc}")
    return _back(repo)


@router.post(
    "/resources/{kind}/{resource_id}/active-form", dependencies=[Depends(require_farm_owner)]
)
async def active_form(request: Request, repo: Repo, kind: str, resource_id: int):
    form = await request.form()
    try:
        if kind not in resources.RESOURCE_KINDS:
            raise resources.ResourceError("Choose a machine or an operator.")
        resources.set_active(
            repo,
            _now(request, repo),
            kind=kind,
            resource_id=resource_id,
            active=_available(form.get("active", "")),
        )
    except (resources.ResourceError, ValueError) as exc:
        return _back(repo, f"Check the resource: {exc}")
    return _back(repo)

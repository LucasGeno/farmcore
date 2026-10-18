"""Admin-only demo controls. Tour mode remains restricted even with admin credentials."""

from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from farmcore.core import demo_control
from farmcore.core.clock import parse_local
from farmcore.core.demo import collect_presets
from farmcore.core.registry import visible_manifests
from farmcore.core.routes_clock import require_admin
from farmcore.web import render

router = APIRouter(dependencies=[Depends(require_admin)])


class Action(BaseModel):
    action: Literal["reset", "preset", "step", "replay", "live"]
    preset: str = "demo-start"
    farm_id: int
    generation: int = Field(ge=0)
    confirm: Literal["yes"] | None = None
    to: str = ""


def same_origin(request: Request):
    origin = request.headers.get("origin") or request.headers.get("referer")
    try:
        value = urlsplit(origin or "")
        allowed = [
            urlsplit(str(request.base_url)),
            urlsplit(request.app.state.settings.public_base_url),
        ]
        valid = any((value.scheme, value.netloc) == (v.scheme, v.netloc) for v in allowed)
    except ValueError:
        valid = False
    if not origin or not valid or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(status_code=403, detail="Same-origin demo confirmation required.")


def view(request):
    with request.app.state.session_factory() as session:
        return demo_control.snapshot(session, request.app.state.settings)


def target_time(request, form):
    if form.action != "step":
        return None
    try:
        return parse_local(form.to, request.app.state.settings.display_tz)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Enter a valid local clock time.") from exc


def selected(request, form):
    key = view(request)["selected_preset"] if form.action == "replay" else form.preset
    for preset in collect_presets(visible_manifests(request)):
        if preset.key == key:
            return preset
    raise HTTPException(status_code=422, detail="Unknown or unavailable demo preset.")


@router.get("/demo", include_in_schema=False)
@router.get("/demo/")
def page(request: Request):
    return render(
        request,
        "core/demo.html",
        presets=collect_presets(visible_manifests(request)),
        **view(request),
    )


@router.post("/demo/preview", dependencies=[Depends(same_origin)])
def preview(request: Request, form: Annotated[Action, Form()]):
    preset = selected(request, form)
    target_time(request, form)
    state = view(request)
    if form.farm_id != state["farm"]["id"]:
        raise HTTPException(
            status_code=403, detail="Only the registered default demo farm may reset."
        )
    if form.generation != state["generation"]:
        raise HTTPException(status_code=409, detail="Demo state changed; preview again.")
    return render(
        request, "core/demo_confirm.html", command=form.model_dump(), preset=preset, **state
    )


@router.post("/demo/apply", dependencies=[Depends(same_origin)])
def apply(request: Request, form: Annotated[Action, Form()]):
    if form.confirm != "yes":
        raise HTTPException(status_code=422, detail="Preview and confirm this demo action first.")
    demo_control.apply_action(
        request.app.state.session_factory,
        visible_manifests(request),
        request.app.state.settings,
        preset=selected(request, form),
        farm_id=form.farm_id,
        generation=form.generation,
        action=form.action,
        target=target_time(request, form),
    )
    return RedirectResponse("/demo/", status_code=303)

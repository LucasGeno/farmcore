"""The read-only tour lives in core so app installation cannot disable the restriction."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.core.access import FarmNotSeeded, selected_farm
from farmcore.core.models.canonical import Parcel
from farmcore.core.registry import visible_manifests
from farmcore.core.routes_demo import same_origin
from farmcore.core.routes_identity import NEXT_MAX, return_path
from farmcore.core.tour import COOKIE, EXIT, end_tour
from farmcore.db import get_session
from farmcore.web import operator_authenticated, render

router = APIRouter()


@router.get("/tour")
def tour(request: Request, session: Session = Depends(get_session)):
    # The cookie is sent with the response; suppress admin UI on this first request too.
    request.state.tour = True
    apps = {m.name for m in visible_manifests(request)}
    parcel_id = None
    if "advisor" in apps:
        try:
            farm = selected_farm(request, session, request.app.state.settings)
        except FarmNotSeeded:
            pass
        else:
            request.state.selected_farm_id = farm.id
            parcel_id = session.scalar(
                select(Parcel.id).where(Parcel.farm_id == farm.id).order_by(Parcel.id).limit(1)
            )
    steps = []
    for app, title, path, description in (
        ("today", "Today", "/today/", "Start with the farm overview and crop protection summary."),
        ("fields", "Map", "/fields/", "Select a parcel to see its context and available evidence."),
        (
            "advisor",
            "Parcel advice",
            f"/advisor/parcels/{parcel_id}" if parcel_id else None,
            "Open an advice card and follow its evidence. These are demo-season records.",
        ),
        (
            "operations",
            "Register",
            "/operations/register",
            "Inspect recorded operations and the spray register; downloads are available.",
        ),
        ("activity", "Activity", "/activity/", "Trace the farm's recorded actions over time."),
        (
            "stations",
            "Data sources",
            "/stations/",
            "Inspect the weather stations and observations behind the demo.",
        ),
        (
            "share",
            "Share",
            "/share/",
            "Inspect the consent ledger and access log. Granting and revoking are disabled here.",
        ),
    ):
        if app == "share" and not _reads_sharing(request):
            continue  # the ledger is the owner's: a step that would refuse is not offered
        if app in apps:
            steps.append({"title": title, "path": path, "description": description})
    response = render(request, "core/tour.html", steps=steps)
    response.set_cookie(
        COOKIE, "1", path="/", httponly=True, samesite="lax", secure=request.url.scheme == "https"
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _reads_sharing(request: Request) -> bool:
    from farmcore.core.identity import principal_for

    principal = principal_for(request)
    farm_id = getattr(request.state, "selected_farm_id", None)
    return operator_authenticated(request) or bool(
        principal and principal.memberships.get(farm_id) == "owner"
    )


@router.post(EXIT, dependencies=[Depends(same_origin)])
def exit_tour(request: Request, next: str = Form(default="", max_length=NEXT_MAX)):
    """Leave the tour for the page the visitor was on (never the tour itself), else home."""
    target = return_path(next)
    if target.split("?")[0].rstrip("/") == "/tour":
        target = ""
    response = RedirectResponse(target or "/", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    end_tour(request, response)
    return response

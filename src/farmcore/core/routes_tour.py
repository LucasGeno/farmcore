"""The read-only tour lives in core so app installation cannot disable the restriction."""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.core.access import FarmNotSeeded, selected_farm
from farmcore.core.models.canonical import Parcel
from farmcore.core.registry import visible_manifests
from farmcore.core.tour import COOKIE
from farmcore.db import get_session
from farmcore.web import render

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
        if app in apps:
            steps.append({"title": title, "path": path, "description": description})
    response = render(request, "core/tour.html", steps=steps)
    response.set_cookie(
        COOKIE, "1", path="/", httponly=True, samesite="lax", secure=request.url.scheme == "https"
    )
    response.headers["Cache-Control"] = "no-store"
    return response

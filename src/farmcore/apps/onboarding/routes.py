"""First-run location step: place the farm by map, browser location or coordinates.

`GET /onboarding/` detects a farm without a location and sends the owner to the step; a farm
that already has one goes Home. `POST /onboarding/location` derives the time zone and country
offline and stores the accepted facts. A sea, border or unknown-time-zone point returns the
step with a question instead of a guess. `POST /onboarding/location/search` is the optional
address search (only when `GEOCODER_BASE_URL` is configured; owner only, one request per
submit, with a descriptive User-Agent).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any
from zoneinfo import available_timezones

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from farmcore.apps.onboarding import geocode, service
from farmcore.core.access import Repo, ScopedRepository, require_farm_owner
from farmcore.core.farm_location import (
    Located,
    country_options,
    farm_location,
    location_needed,
)
from farmcore.core.models.canonical import Farm
from farmcore.web import context_url, render

router = APIRouter()

SEARCH_MAX_LEN = 200


@lru_cache(maxsize=1)
def _time_zones() -> list[str]:
    return sorted(available_timezones())


def _farm(repo: ScopedRepository) -> Farm:
    farm = repo.session.get(Farm, repo.farm_id)
    if farm is None:  # the repository is bound to a committed farm
        raise HTTPException(status_code=404, detail="Farm not found.")
    return farm


def _page(
    request: Request,
    repo: ScopedRepository,
    *,
    form: dict[str, Any] | None = None,
    located: Located | None = None,
    results: list[geocode.Place] | None = None,
    error: str | None = None,
    search_error: str | None = None,
    status: int = 200,
) -> HTMLResponse:
    farm = _farm(repo)
    settings = request.app.state.settings
    response = render(
        request,
        "onboarding/location.html",
        farm=farm,
        form=form if form is not None else service.current(farm),
        located=located,
        question=located.question if located is not None else None,
        results=results or [],
        error=error,
        search_error=search_error,
        geocoder_on=bool(settings.geocoder_base_url),
        time_zones=_time_zones(),
        countries=country_options(),
        location_needed=location_needed(farm),
        start=farm_location(farm),
    )
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/")
def index(request: Request, repo: Repo) -> RedirectResponse:
    """First run: the location step until the farm has a point, then Home."""
    target = "/onboarding/location" if location_needed(_farm(repo)) else "/today/"
    return RedirectResponse(context_url(request, target), status_code=303)


@router.get("/location", response_class=HTMLResponse)
def location(request: Request, repo: Repo) -> HTMLResponse:
    return _page(request, repo)


@router.get("/today", response_class=HTMLResponse)
def today(request: Request, repo: Repo) -> HTMLResponse:
    """Home's first-run prompt; hidden (`data-home-empty`) once the farm has a location."""
    farm = _farm(repo)
    return render(
        request,
        "onboarding/_today.html",
        needed=location_needed(farm),
        location_url=context_url(request, "/onboarding/location"),
    )


@router.post("/location", dependencies=[Depends(require_farm_owner)], response_class=HTMLResponse)
def save_location(
    request: Request,
    repo: Repo,
    latitude: Annotated[str, Form()] = "",
    longitude: Annotated[str, Form()] = "",
    time_zone: Annotated[str, Form()] = "",
    country: Annotated[str, Form()] = "",
    mode: Annotated[str, Form()] = "derive",
) -> Response:
    try:
        lat, lon = service.parse_point(latitude, longitude)
        choice, located = service.choose(
            lat, lon, time_zone=time_zone, country=country, manual=mode == "manual"
        )
    except service.LocationError as exc:
        return _page(
            request,
            repo,
            form={
                "latitude": latitude,
                "longitude": longitude,
                "time_zone": time_zone,
                "country": country,
                "mode": mode,
            },
            error=str(exc),
            status=422,
        )
    if choice is None:
        proposed = {
            "latitude": f"{lat:.6f}",
            "longitude": f"{lon:.6f}",
            "time_zone": located.time_zone or "",
            "country": located.country or "",
            "mode": "manual",
        }
        return _page(request, repo, form=proposed, located=located, status=422)
    service.store(_farm(repo), choice)
    return RedirectResponse(context_url(request, "/today/"), status_code=303)


@router.post(
    "/location/search", dependencies=[Depends(require_farm_owner)], response_class=HTMLResponse
)
def search_location(request: Request, repo: Repo, q: Annotated[str, Form()] = "") -> HTMLResponse:
    settings = request.app.state.settings
    if not settings.geocoder_base_url:
        raise HTTPException(status_code=404, detail="Address search is off.")
    query = q.strip()[:SEARCH_MAX_LEN]
    if not query:
        return _page(request, repo, search_error="Enter an address to search.")
    try:
        results = geocode.search(
            settings.geocoder_base_url,
            query,
            public_base_url=settings.public_base_url,
        )
    except geocode.GeocoderError as exc:
        return _page(request, repo, search_error=str(exc), status=502)
    if not results:
        return _page(request, repo, search_error=f'No place found for "{query}".')
    return _page(request, repo, results=results)

"""Owner-only CAP map; signed selections become parcel drafts without outbound POST work."""

from uuid import uuid4

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from farmcore.apps.onboarding import registries
from farmcore.apps.onboarding.import_routes import BoundedImportRoute
from farmcore.apps.onboarding.importers import service
from farmcore.apps.onboarding.registries import candidates as snapshots
from farmcore.core import drafts
from farmcore.core.access import Repo, require_farm_owner
from farmcore.core.farm_location import farm_location
from farmcore.core.models.canonical import Farm, Parcel
from farmcore.core.registry import visible_manifests
from farmcore.core.routes_drafts import request_actor
from farmcore.web import context_url, render

router = APIRouter(dependencies=[Depends(require_farm_owner)], route_class=BoundedImportRoute)


def page(request, repo, message=""):
    farm = repo.session.get(Farm, repo.farm_id)
    settings = request.app.state.settings
    return render(
        request,
        "onboarding/registry.html",
        point=farm_location(farm),
        message=message,
        tile_url=settings.location_map_tiles_url,
        tile_attribution=settings.location_map_attribution,
    )


@router.get("/registry")
def registry_page(request: Request, repo: Repo):
    return page(request, repo)


def lookup_context(repo, value):
    farm = repo.session.get(Farm, repo.farm_id)
    bbox = registries.bounding_box(value, farm_location(farm) or {})
    country = (farm.props or {}).get("country")
    # Finish the read/audit transaction before waiting on the public service.
    repo.session.commit()
    return country, bbox


def validate_candidates(request, repo, rows):
    require_farm_owner(request, repo)
    if not rows:
        return []
    return service.validate(repo.session, repo.farm_id, {"parcels": rows})["parcels"]


@router.get("/registry/features")
async def features(request: Request, repo: Repo, bbox: str = ""):
    try:
        country, bounds = await run_in_threadpool(lookup_context, repo, bbox)
        rows = await registries.lookup(country, bounds)
        rows = await run_in_threadpool(validate_candidates, request, repo, rows)
        return {
            "type": "FeatureCollection",
            "candidates": await run_in_threadpool(
                snapshots.sign, rows, repo.farm_id, request_actor(request)
            ),
            "features": [
                {
                    "type": "Feature",
                    "id": index,
                    "geometry": row["geometry"],
                    "properties": {k: v for k, v in row.items() if k != "geometry"},
                }
                for index, row in enumerate(rows)
            ],
            "message": "" if rows else "No public parcels in this window. Import a file instead.",
        }
    except ValueError as exc:
        return {
            "type": "FeatureCollection",
            "features": [],
            "message": str(exc) if isinstance(exc, registries.Unavailable) else registries.FALLBACK,
        }


@router.post("/registry/claim")
async def claim(request: Request, repo: Repo):
    async with request.form(max_fields=205) as form:
        token = form.get("candidates", "")
        selected = form.getlist("selected")
    # Repo and require_farm_owner already locked the farm and rechecked authority.
    return await run_in_threadpool(propose_selection, request, repo, token, selected)


def propose_selection(request, repo, token, selected):
    try:
        if not selected or len(selected) > 200 or not all(isinstance(v, str) for v in selected):
            raise ValueError("Select your parcels first.")
        selected = set(selected)
        rows = snapshots.verify(token, repo.farm_id, request_actor(request))
        chosen = [row for row in rows if row["source_ref"] in selected]
        if {row["source_ref"] for row in chosen} != selected:
            raise ValueError("The public map changed. Load the parcels again before selecting.")
        chosen = service.validate(repo.session, repo.farm_id, {"parcels": chosen})["parcels"]
        existing = list(
            repo.execute(
                select(Parcel).where(Parcel.farm_id == repo.farm_id),
                scope="fields:read",
                resource="parcel",
            ).scalars()
        )
        chosen = service.review_items(existing, chosen)
        if not chosen:
            return page(request, repo, "All selected parcels are already imported.")
        draft, _ = drafts.propose(
            repo.session,
            visible_manifests(request),
            farm_id=repo.farm_id,
            actor=request_actor(request),
            kind="parcels",
            payload={"parcels": chosen},
            provenance={
                "source_type": "registry",
                "registry_ref": chosen[0]["source_scope"],
                "details": {"method": "public CAP map selection"},
            },
            request_key=uuid4().hex,
        )
    except (ValueError, drafts.DraftError) as exc:
        return page(request, repo, str(exc))
    return RedirectResponse(context_url(request, f"/drafts/{draft.id}"), status_code=303)

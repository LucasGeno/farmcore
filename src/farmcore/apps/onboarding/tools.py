"""Onboarding propose tools for an external agent: `propose_parcels`, `propose_crop_assignments`
and `list_import_candidates`.

`propose_parcels` and `propose_crop_assignments` write drafts (effect `propose`) the farmer
confirms on `/drafts/<id>`; neither confirms, deletes or edits a record. `list_import_candidates`
(effect `read`) lists public-registry parcels near the farm so the agent can propose them next.
Deterministic validation is reused from the parcel import (`importers.service.validate`,
`parsers.normalise`) and the crop crosswalk (`crops`). Text from the agent is data, never
instructions.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any
from uuid import uuid4

from shapely import get_num_coordinates
from shapely.geometry import shape
from sqlalchemy import func, select

from farmcore.apps.onboarding import registries
from farmcore.apps.onboarding.importers import parsers, service
from farmcore.core import drafts
from farmcore.core.farm_location import farm_location
from farmcore.core.models.canonical import AccessLog, Farm, Parcel
from farmcore.core.models.drafts import Draft
from farmcore.core.models.identity import AccessToken
from farmcore.core.tools import ToolContext, ToolResult, ToolSpec, object_schema

SCOPE = "drafts:propose"
FIELDS_SCOPE = "fields:read"
REVIEW_PATH = "/drafts/{id}"


def _propose(ctx: ToolContext, kind: str, payload: dict[str, Any]) -> Draft:
    """Validate and store one draft as the agent token; returns the open draft."""
    draft, _ = drafts.propose(
        ctx.repo.session,
        ctx.manifests,
        farm_id=ctx.repo.farm_id,
        actor=ctx.repo.actor,
        kind=kind,
        payload=payload,
        provenance={"source_type": "agent"},
        request_key=uuid4().hex,
    )
    return draft


TOOLS = (
    ToolSpec(
        name="propose_parcels",
        description=(
            "Propose parcel boundaries as a draft for the farmer to confirm: a WGS84 GeoJSON "
            "FeatureCollection of polygons, one feature per parcel."
        ),
        params=object_schema({"geojson": {"type": "object"}}, required=("geojson",)),
        effect="propose",
        func="farmcore.apps.onboarding.tools:propose_parcels",
        scope=SCOPE,
    ),
    ToolSpec(
        name="propose_crop_assignments",
        description=(
            "Propose crop assignments as a draft: the season start date and a map of parcel id "
            "to FarmCore crop key (e.g. vine, olive). The farmer confirms each."
        ),
        params=object_schema(
            {
                "starts_on": {"type": "string"},
                "assignments": {"type": "object", "additionalProperties": {"type": "string"}},
            },
            required=("starts_on", "assignments"),
        ),
        effect="propose",
        func="farmcore.apps.onboarding.tools:propose_crop_assignments",
        scope=SCOPE,
    ),
    ToolSpec(
        name="list_import_candidates",
        description=(
            "Public-registry parcels near the farm (by bounding box) that the agent could propose "
            "as parcels."
        ),
        params=object_schema({"bbox": {"type": "string"}}, required=("bbox",)),
        effect="read",
        func="farmcore.apps.onboarding.tools:list_import_candidates",
        scope=FIELDS_SCOPE,
    ),
)


def propose_parcels(ctx: ToolContext, geojson: Any) -> ToolResult:
    if not isinstance(geojson, dict) or geojson.get("type") != "FeatureCollection":
        raise drafts.DraftInvalid("Send a WGS84 GeoJSON FeatureCollection.")
    if "crs" in geojson:
        raise drafts.DraftInvalid("GeoJSON must be WGS84 without a CRS override.")
    features = geojson.get("features")
    if not isinstance(features, list) or not 1 <= len(features) <= parsers.MAX_PARCELS:
        raise drafts.DraftInvalid("Send between 1 and 200 parcel boundaries.")
    items = []
    try:
        for index, feature in enumerate(features, 1):
            if not isinstance(feature, dict) or not isinstance(feature.get("geometry"), dict):
                raise ValueError("Each feature needs a polygon geometry.")
            props = dict(feature.get("properties") or {})
            # Review notices and registry metadata are authored by the server only.
            for field in ("messages", "attribution", "licence", "year", "retrieved_at"):
                props.pop(field, None)
            props["source_scope"] = "agent"
            geometry = shape(feature["geometry"])
            if get_num_coordinates(geometry) > parsers.MAX_VERTICES:
                raise ValueError("A boundary exceeds the vertex limit.")
            items.append(parsers.normalise(geometry, props, index))
        payload = service.validate(ctx.repo.session, ctx.repo.farm_id, {"parcels": items})
    except ValueError as exc:
        raise drafts.DraftInvalid(str(exc)) from exc
    existing = list(
        ctx.repo.session.scalars(select(Parcel).where(Parcel.farm_id == ctx.repo.farm_id))
    )
    payload["parcels"] = service.review_items(existing, payload["parcels"])
    if not payload["parcels"]:
        raise drafts.DraftInvalid("All parcels are already imported.")
    draft = _propose(ctx, "parcels", payload)
    text = f"Proposed {len(payload['parcels'])} parcel(s); the farmer reviews and confirms them."
    return ToolResult(
        facts={"draft_id": draft.id, "parcels": len(payload["parcels"])},
        text=text,
        links=(("Review parcels", REVIEW_PATH.format(id=draft.id)),),
    )


def propose_crop_assignments(
    ctx: ToolContext, starts_on: str, assignments: dict[str, str]
) -> ToolResult:
    draft = _propose(ctx, "crop-assignments", {"starts_on": starts_on, "assignments": assignments})
    text = f"Proposed crops for {len(assignments)} parcel(s); the farmer reviews and confirms them."
    return ToolResult(
        facts={"draft_id": draft.id, "parcels": len(assignments)},
        text=text,
        links=(("Review crops", REVIEW_PATH.format(id=draft.id)),),
    )


def list_import_candidates(ctx: ToolContext, bbox: str) -> ToolResult:
    farm = ctx.repo.session.get(Farm, ctx.repo.farm_id)
    country = (farm.props or {}).get("country") if farm else None
    try:
        bounds = registries.bounding_box(bbox, farm_location(farm) or {})
        _registry_budget(ctx)
        rows = asyncio.run(registries.lookup(country, bounds))
        validated = (
            service.validate(ctx.repo.session, ctx.repo.farm_id, {"parcels": rows})["parcels"]
            if rows
            else []
        )
    except ValueError as exc:
        raise drafts.DraftInvalid(str(exc)) from exc
    candidates = [
        {
            "name": row["name"],
            "source_ref": row["source_ref"],
            "crop_key": row.get("crop_key", ""),
            "crop_name": row.get("crop_name", ""),
            "area_ha": round(row["area_ha"], 4),
        }
        for row in validated
    ]
    text = f"{len(candidates)} public parcel candidate(s) near the farm."
    return ToolResult(
        facts={"candidates": candidates},
        text=text,
        links=(("Open registry", "/onboarding/registry"),),
    )


def _registry_budget(ctx: ToolContext) -> None:
    """Assumed product policy: six lookup attempts per token per rolling minute.

    The MCP audit includes this call and failed lookups. Serialize on the token row so
    concurrent transports/processes share the budget; no in-memory reset on reconnect.
    The bounded connector holds this lock for at most its eight-second deadline.
    """
    actor = ctx.repo.actor
    prefix, _, token_id = actor.partition(":")
    if prefix != "token" or not token_id.isdigit():
        raise drafts.DraftInvalid("Registry lookups require an agent token.")
    ctx.repo.session.execute(
        select(AccessToken.id).where(AccessToken.id == int(token_id)).with_for_update()
    ).one()
    attempts = ctx.repo.session.scalar(
        select(func.count())
        .select_from(AccessLog)
        .where(
            AccessLog.farm_id == ctx.repo.farm_id,
            AccessLog.actor == actor,
            AccessLog.resource == "mcp:list_import_candidates",
            AccessLog.ts >= drafts.wall_now() - timedelta(minutes=1),
        )
    )
    if attempts > 6:
        raise drafts.DraftInvalid("Registry lookup rate limit reached. Try again in a minute.")

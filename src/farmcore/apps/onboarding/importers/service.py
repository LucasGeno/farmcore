"""Parcel drafts: revalidate at confirm and deduplicate under the core farm lock."""

import hashlib
import json
from functools import lru_cache
from importlib.resources import files
from uuid import uuid4

from geoalchemy2.shape import from_shape, to_shape
from pydantic import BaseModel, ConfigDict, Field
from shapely import get_num_coordinates
from shapely.geometry import MultiPolygon, box, shape
from sqlalchemy import select

from farmcore.apps.onboarding.importers.parsers import MAX_PARCELS, MAX_VERTICES, normalise
from farmcore.core.models.canonical import ExtId, Farm, Parcel


class ParcelInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(max_length=200)
    crop_code: str = Field(default="", max_length=200)
    source_ref: str = Field(default="", max_length=200)
    source_scope: str = Field(default="", max_length=255)
    attribution: str = Field(default="", max_length=255)
    licence: str = Field(default="", max_length=100)
    year: int | None = Field(default=None, ge=2000, le=2100)
    geometry: dict
    area_ha: float = 0  # Always recomputed; never trust a proposed area.
    messages: list[str] = Field(default_factory=list, max_length=10)


class ParcelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parcels: list[ParcelInput] = Field(min_length=1, max_length=MAX_PARCELS)


@lru_cache(maxsize=1)
def country_boxes():
    data = json.loads(files("farmcore.core").joinpath("geo/countries.geojson").read_text())
    return {
        f["properties"]["iso"]: [
            box(*p.bounds)
            for p in (
                list(shape(f["geometry"]).geoms)
                if f["geometry"]["type"] == "MultiPolygon"
                else [shape(f["geometry"])]
            )
        ]
        for f in data["features"]
    }


def validate(session, farm_id, payload):
    payload = ParcelPayload.model_validate(payload).model_dump(mode="json")
    farm = session.get(Farm, farm_id)
    country = (farm.props or {}).get("country") if farm else None
    boxes = country_boxes().get(country)
    if not boxes:
        raise ValueError("Set a supported farm country before importing parcels.")
    for item in payload["parcels"]:
        if get_num_coordinates(shape(item["geometry"])) > MAX_VERTICES:
            raise ValueError("A boundary exceeds the vertex limit.")
        if not any(b.covers(shape(item["geometry"])) for b in boxes):
            raise ValueError("Boundary is outside the farm country bounding box; check the CRS.")
    return {
        "parcels": [
            normalise(shape(p["geometry"]), p, i) for i, p in enumerate(payload["parcels"], 1)
        ]
    }


def matching(rows, item):
    geometry = shape(item["geometry"])
    for row in rows:
        existing = to_shape(row.geom)
        # Assumed product policy: >=99% intersection / union is the same boundary.
        if existing.intersection(geometry).area / existing.union(geometry).area >= 0.99:
            return row
        if (
            item["source_ref"]
            and row.props.get("import_source_ref") == item["source_ref"]
            and row.props.get("import_source_scope") == item["source_scope"]
        ):
            return row
    return None


def review_items(existing, items):
    """Drop duplicates and expose redrawn overlaps before any confirmation."""
    result, geometries = [], []
    for original in items:
        if matching(existing, original) is not None:
            continue
        item = {**original, "messages": list(original["messages"])}
        geom = shape(item["geometry"])
        if any(geom.intersection(g).area / geom.union(g).area >= 0.99 for g in geometries):
            continue
        for row in existing:
            other = to_shape(row.geom)
            overlap = geom.intersection(other).area
            if overlap > 0:
                item["messages"].append(
                    f"Overlaps existing parcel {row.name} "
                    f"({overlap / geom.area:.1%} of this boundary)."
                )
        result.append(item)
        geometries.append(geom)
    return result


def confirm(session, draft, *, actor, now, services):
    payload = validate(session, draft.farm_id, draft.payload)
    rows = list(session.scalars(select(Parcel).where(Parcel.farm_id == draft.farm_id)))
    ids = []
    for item in payload["parcels"]:
        row = matching(rows, item)
        if row is None:
            geom = shape(item["geometry"])
            row = Parcel(
                farm_id=draft.farm_id,
                name=item["name"],
                alias=f"I-{uuid4().hex[:12]}",
                area_ha=item["area_ha"],
                geom=from_shape(
                    MultiPolygon([geom]) if geom.geom_type == "Polygon" else geom, srid=4326
                ),
                props={
                    "import_provenance": draft.provenance,
                    "import_source_ref": item["source_ref"],
                    "import_source_scope": item["source_scope"],
                    "crop_code": item["crop_code"],
                    "registry": {k: item[k] for k in ("attribution", "licence", "year")},
                },
            )
            session.add(row)
            session.flush()
            key = hashlib.sha256(json.dumps(item["geometry"], sort_keys=True).encode()).hexdigest()
            session.add(
                ExtId(
                    source="parcel-file",
                    key=f"{draft.farm_id}:{key}",
                    model="parcel",
                    res_id=row.id,
                )
            )
            rows.append(row)
        ids.append(row.id)
    return "parcel", ids[0]

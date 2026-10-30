"""Operator-only, create-only parcel provisioning. No public upload or implicit farm."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import typer
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError

from farmcore.core.models.canonical import Farm, Parcel
from farmcore.db import make_engine, make_session_factory, session_scope
from farmcore.settings import get_settings


class Properties(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)

    alias: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    area_ha: Decimal = Field(gt=0, max_digits=14, decimal_places=6)
    iacs_id: str | None = Field(default=None, max_length=200)
    eppo_crop: str | None = Field(default=None, max_length=20)
    variety: str | None = Field(default=None, max_length=200)
    crop_label: str | None = Field(default=None, max_length=200)
    planted_year: int | None = Field(default=None, ge=1800, le=2200)


def register(cli: typer.Typer) -> None:
    parcels = typer.Typer(help="Operator parcel provisioning (GeoJSON, WGS84).")
    cli.add_typer(parcels, name="parcels")

    @parcels.command("import")
    def import_parcels(path: Path, farm_id: int = typer.Option(..., min=1)) -> None:
        """Atomically add parcels; identical aliases are skipped, changed aliases refused."""
        engine = make_engine(get_settings())
        try:
            if path.stat().st_size > 5_000_000:
                raise ValueError("GeoJSON must be at most 5 MB.")
            data = json.loads(path.read_text())
            if (
                not isinstance(data, dict)
                or data.get("type") != "FeatureCollection"
                or "crs" in data
                or not isinstance(data.get("features"), list)
                or not 1 <= len(data["features"]) <= 1000
            ):
                raise ValueError(
                    "Expected 1–1000 WGS84 features in a FeatureCollection without crs."
                )
            created = skipped = 0
            with session_scope(make_session_factory(engine)) as session:
                # Serialize imports for this farm, including a retry in another process.
                farm = session.scalar(select(Farm).where(Farm.id == farm_id).with_for_update())
                if farm is None:
                    raise ValueError("No such farm.")
                aliases = set()
                for feature in data["features"]:
                    if not isinstance(feature, dict) or feature.get("type") != "Feature":
                        raise ValueError("Expected a GeoJSON Feature.")
                    p = Properties.model_validate(feature.get("properties"))
                    if p.alias in aliases:
                        raise ValueError("Duplicate alias in file.")
                    aliases.add(p.alias)
                    geometry = feature.get("geometry")
                    if (
                        not isinstance(geometry, dict)
                        or geometry.get("type") not in {"Polygon", "MultiPolygon"}
                        or "crs" in geometry
                    ):
                        raise ValueError("Expected Polygon or MultiPolygon without crs.")
                    geom = func.ST_Multi(
                        func.ST_SetSRID(
                            func.ST_GeomFromGeoJSON(json.dumps(geometry, allow_nan=False)), 4326
                        )
                    )
                    valid = session.scalar(
                        select(
                            func.ST_IsValid(geom)
                            & ~func.ST_IsEmpty(geom)
                            & (func.ST_NDims(geom) == 2)
                            & func.ST_CoveredBy(
                                geom, func.ST_MakeEnvelope(-180, -90, 180, 90, 4326)
                            )
                        )
                    )
                    if not valid:
                        raise ValueError("Invalid, empty, non-2D or out-of-range WGS84 polygon.")
                    props = p.model_dump(
                        exclude_none=True, exclude={"alias", "name", "area_ha", "iacs_id"}
                    )
                    existing = session.scalar(
                        select(Parcel).where(Parcel.farm_id == farm_id, Parcel.alias == p.alias)
                    )
                    if existing:
                        same = (
                            existing.name == p.name
                            and existing.area_ha == p.area_ha
                            and existing.iacs_id == p.iacs_id
                            and existing.props == props
                            and session.scalar(select(func.ST_Equals(existing.geom, geom)))
                        )
                        if not same:
                            raise ValueError("Existing alias differs; no parcels changed.")
                        skipped += 1
                    else:
                        session.add(
                            Parcel(
                                farm_id=farm_id,
                                alias=p.alias,
                                name=p.name,
                                area_ha=p.area_ha,
                                iacs_id=p.iacs_id,
                                props=props,
                                geom=geom,
                            )
                        )
                        created += 1
            typer.echo(f"farm {farm_id}: {created} parcels created, {skipped} unchanged")
        except (OSError, ValueError, ValidationError, DBAPIError) as exc:
            # Never echo database parameters, file content or private parcel values.
            message = (
                str(exc) if type(exc) is ValueError else "Invalid file or database write refused."
            )
            typer.echo(f"Parcel import refused: {message}", err=True)
            raise typer.Exit(1) from None
        finally:
            engine.dispose()

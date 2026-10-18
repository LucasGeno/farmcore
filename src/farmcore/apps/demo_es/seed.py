"""Spanish demo data (scene B, local files): no operations, forecasts or hourly observations."""

from pathlib import Path

from sqlalchemy import func

from farmcore.apps.demo_es import archive, sigpac
from farmcore.core import ext_ids
from farmcore.core.access import ensure_owner_grant
from farmcore.core.models.canonical import Asset, Farm, Parcel

SOURCE = "farmcore-demo-es"
FARM_KEY = "valbuena"


def seed(session, *, scene: str | None, data_dir: Path) -> dict[str, int]:
    if (scene or "").lower() != "b":
        return {}
    shape_file, weather_file = data_dir / sigpac.FILE, data_dir / archive.FILE
    if not shape_file.is_file() or not weather_file.is_file():
        return {"spanish_farms": 0}
    plots = sigpac.read(shape_file)
    coverage = archive.summary(archive.read(weather_file))
    ext_ids.ensure_source(session, SOURCE, "Valbuena Spanish demo grouping", "seed", "Demo data")
    ext_ids.ensure_source(
        session,
        "sigpac-2024",
        "SIGPAC 2024 · ITACyL / Junta de Castilla y León",
        "geodata",
        "SIGPAC 2024 · FEGA/MAPA and Junta de Castilla y León (ITACyL). "
        "Licence: CC BY 4.0 (FEGA, national) / IGCYL-NC "
        "(Junta de Castilla y León regional catalogue, non-commercial). "
        "Used for a non-commercial demo with attribution.",
    )
    ext_ids.ensure_source(
        session,
        "inforiego",
        "InfoRiego · ITACyL",
        "weather",
        "InfoRiego · ITACyL, station VA07. Reuse terms set by ITACyL; "
        "not redistributed here, shown with attribution to ITACyL.",
    )
    existing = ext_ids.lookup(session, SOURCE, FARM_KEY)
    props = dict(existing.props) if isinstance(existing, Farm) else {}
    farm, _ = ext_ids.upsert(
        session,
        source=SOURCE,
        key=FARM_KEY,
        model=Farm,
        values={
            "name": "Valbuena de Duero · Spain (demo data)",
            "props": {
                **props,
                "country": "ES",
                "region": "ES418",
                "demo_data": True,
                "advisor_unavailable_reason": archive.NOTICE,
                "product_ids": [],
                "map_notice": "Demo grouping of ten SIGPAC 2024 · ITACyL vineyard recintos; "
                "no claim of common ownership. " + archive.NOTICE,
            },
        },
    )
    ensure_owner_grant(session, farm)
    for index, plot in enumerate(plots, 1):
        geometry = func.ST_Multi(
            func.ST_Transform(func.ST_BuildArea(func.ST_GeomFromText(plot.rings_wkt, 4258)), 4326)
        )
        ext_ids.upsert(
            session,
            source="sigpac-2024",
            key=plot.reference,
            model=Parcel,
            values={
                "farm_id": farm.id,
                "alias": f"ES{index}",
                "name": f"SIGPAC {plot.reference}",
                "iacs_id": plot.reference,
                "area_ha": plot.area_ha,
                "geom": geometry,
                "props": {
                    "uso_sigpac": "VI",
                    "c_refrec": plot.original,
                    "source_year": 2024,
                    "crop_label": "Vineyard",
                    "eppo_crop": "VITVI",
                    "demo_data": True,
                    "bbch_config": False,
                },
            },
        )
    station, _ = ext_ids.upsert(
        session,
        source="inforiego",
        key="VA07",
        model=Asset,
        values={
            "farm_id": farm.id,
            "kind": "station",
            "name": "InfoRiego VA07 · Valbuena de Duero",
            "geom": None,
            "props": {
                "provider": "InfoRiego · ITACyL",
                "file": archive.FILE,
                "archive": coverage,
                "archive_only": True,
            },
        },
    )
    station.props = {**station.props, "archive_page": f"/demo_es/archive/{station.id}"}
    return {"spanish_farms": 1, "spanish_parcels": len(plots), "spanish_archives": 1}

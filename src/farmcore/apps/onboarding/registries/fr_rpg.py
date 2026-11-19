"""IGN APICarto RPG v2; latest published millésime in its official catalogue."""

import json

from shapely.geometry import box, mapping, shape

from farmcore.apps.onboarding.importers.parsers import normalise

from . import document

# https://apicarto.ign.fr/api/doc/rpg — checked 2026-11-19.
YEAR = 2024
ATTRIBUTION = "IGN — Registre parcellaire graphique"
LICENCE = "Etalab 2.0"


async def fetch(client, bbox):
    features = await document(
        client,
        "https://apicarto.ign.fr/api/rpg/v2",
        {
            "annee": YEAR,
            "geom": json.dumps(mapping(box(*bbox))),
        },
    )
    rows = []
    for feature in features:
        props = feature["properties"]
        ref = str(props["id_parcel"])
        rows.append(
            normalise(
                shape(feature["geometry"]),
                {
                    "name": f"RPG {ref}",
                    "crop_code": props.get("code_cultu", ""),
                    "source_ref": ref,
                    "source_scope": f"ign-rpg-{YEAR}",
                    "attribution": ATTRIBUTION,
                    "licence": LICENCE,
                    "year": YEAR,
                },
                len(rows) + 1,
            )
        )
    return rows

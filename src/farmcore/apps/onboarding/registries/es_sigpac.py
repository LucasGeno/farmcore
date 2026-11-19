"""FEGA SIGPAC boundaries joined to declared crops by the public recinto code."""

from shapely.geometry import shape

from farmcore.apps.onboarding.importers.parsers import normalise

from . import document

ATTRIBUTION = "FEGA — SIGPAC"
LICENCE = "CC BY 4.0"
KEY = ("provincia", "municipio", "agregado", "zona", "poligono", "parcela", "recinto")


def reference(props):
    return ":".join(str(props[key]) for key in KEY)


async def fetch(client, bbox):
    params = {"bbox": ",".join(str(v) for v in bbox), "f": "json", "limit": 200}
    base = "https://sigpac-hubcloud.es/ogcapi/collections/"
    boundaries = await document(client, base + "recintos/items", params)
    declarations = await document(client, base + "cultivo_declarado/items", params)
    crops = {}
    for feature in declarations:
        props = feature["properties"]
        code = props.get("parc_producto")
        if code is not None:
            crops.setdefault(reference(props), set()).add(str(code))
    rows = []
    for feature in boundaries:
        ref = reference(feature["properties"])
        codes = crops.get(ref, set())
        # Several declarations can share a recinto: never pick an arbitrary crop.
        messages = ["Campaign year is not published by this service."]
        if len(codes) > 1:
            messages.append("Multiple declared crop codes: " + ", ".join(sorted(codes)))
        rows.append(
            normalise(
                shape(feature["geometry"]),
                {
                    "name": f"SIGPAC {ref}",
                    "source_ref": ref,
                    "source_scope": "sigpac-current",
                    "crop_code": next(iter(codes)) if len(codes) == 1 else "",
                    "messages": messages,
                    "attribution": ATTRIBUTION,
                    "licence": LICENCE,
                    "year": None,
                },
                len(rows) + 1,
            )
        )
    return rows

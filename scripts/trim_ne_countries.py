#!/usr/bin/env python3
"""Trim a Natural Earth Admin 0 Countries GeoJSON to the bundled farm-location file.

Source: Natural Earth v5.1.2 (commit ca96624a56bd078437bca8184e78163e5039ad19),
`geojson/ne_110m_admin_0_countries.geojson`. Natural Earth is public domain.

Usage:
    curl -sSLo /tmp/ne_110m_admin_0_countries.geojson \
      https://raw.githubusercontent.com/nvkelso/natural-earth-vector/\
ca96624a56bd078437bca8184e78163e5039ad19/geojson/ne_110m_admin_0_countries.geojson
    python scripts/trim_ne_countries.py /tmp/ne_110m_admin_0_countries.geojson \
      src/farmcore/core/geo/countries.geojson

Keeps only `ISO_A2_EH` (as `iso`) and `NAME_EN` (as `name`), rounds coordinates to three
decimals (about 110 m at the equator) and drops features without an ISO alpha-2 code.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SOURCE = "Natural Earth 1:110m Admin 0 Countries (public domain, https://www.naturalearthdata.com/)"
DECIMALS = 3


def rounded(value: Any) -> Any:
    if isinstance(value, list):
        if value and isinstance(value[0], (int, float)):
            return [round(float(number), DECIMALS) for number in value]
        return [rounded(item) for item in value]
    return value


def trim(document: dict[str, Any]) -> dict[str, Any]:
    features = []
    for feature in document["features"]:
        properties = feature["properties"]
        iso = properties.get("ISO_A2_EH") or properties.get("ISO_A2")
        if not iso or iso in ("-99", "-1"):
            continue
        geometry = feature["geometry"]
        features.append(
            {
                "type": "Feature",
                "properties": {"iso": iso, "name": properties.get("NAME_EN") or properties["NAME"]},
                "geometry": {
                    "type": geometry["type"],
                    "coordinates": rounded(geometry["coordinates"]),
                },
            }
        )
    return {"type": "FeatureCollection", "source": SOURCE, "features": features}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    source, target = Path(argv[1]), Path(argv[2])
    document = json.loads(source.read_text(encoding="utf-8"))
    target.write_text(
        json.dumps(trim(document), separators=(",", ":"), ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

"""Manifest of app `fields`: demo farm parcels, parcel GeoJSON, map."""

from farmcore.apps.fields.tools import TOOLS
from farmcore.core.manifest import Manifest, Widget

MANIFEST = Manifest(
    name="fields",
    version="0.1.0",
    summary="Your parcel records, crop details and farm map.",
    depends=("core",),
    scopes=("fields:read",),
    routes=(
        "farmcore.apps.fields.routes:router",
        "farmcore.apps.fields.outlines:router",
        "farmcore.apps.fields.aerial:router",
    ),
    title="Map & parcels",
    category="Essentials",
    required=True,
    nav=("Map", "/fields/"),
    nav_group="farmer",
    nav_order=20,
    nav_icon="map",
    # Home pairs the map preview with Recent activity on desktop.
    widgets=(Widget("/fields/today?desktop=1", 30, "half"),),
    tools=TOOLS,
    seed="farmcore.apps.fields.seed:seed",
    maturity="alpha",
)

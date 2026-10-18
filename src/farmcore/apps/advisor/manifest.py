"""Manifest of app `advisor`: the `advisor.evaluate` job, advice rows, the parcel advice card.

The job name `advisor.evaluate` is a contract: chat calls `jobs.run_now` with it after a confirm.
"""

from datetime import timedelta

from farmcore.apps.advisor.tools import TOOLS
from farmcore.core.manifest import JobSpec, Manifest, MapLayer, ParcelView, Widget

JOB_NAME = "advisor.evaluate"

MANIFEST = Manifest(
    name="advisor",
    version="0.1.0",
    summary="Tells you which parcels to spray, when, and why.",
    depends=("fields", "stations", "operations"),
    # advice:read is the app's own; the others are the reads its job and card make.
    scopes=("advice:read", "fields:read", "stations:read", "logs:read"),
    jobs=(
        JobSpec(
            name=JOB_NAME,
            func="farmcore.apps.advisor.job:evaluate",
            interval=timedelta(hours=1),
        ),
    ),
    routes=(
        "farmcore.apps.advisor.routes:router",
        "farmcore.apps.advisor.today:router",
        "farmcore.apps.advisor.layers:router",
        "farmcore.apps.advisor.debug:router",
        "farmcore.apps.advisor.map_sections:router",
        "farmcore.apps.advisor.about:router",
    ),
    title="Crop protection",
    category="Crop protection",
    nav=("Crop protection", "/advisor/"),
    nav_group="apps",
    nav_order=10,
    nav_icon="advisor",
    widgets=(
        Widget("/advisor/today?view=attention", 10),
        Widget("/advisor/today?view=calm", 55),
    ),
    map_layers=(
        MapLayer("advice", "Spray advice", "/advisor/layer.geojson", "parcels", 10, True, "spray"),
    ),
    # Map panel sections: Farm today's advice, one parcel's advice.
    farm_sections=("/advisor/map/farm",),
    parcel_sections=("/advisor/map/parcels/{id}",),
    parcel_views=(
        ParcelView(
            "protection",
            "Protection",
            "/advisor/parcels/{id}/workspace",
            "/advisor/parcels/{id}",
            order=10,
        ),
        ParcelView(
            "history",
            "Season history",
            "/advisor/parcels/{id}/history",
            "/advisor/parcels/{id}#history-heading",
            order=20,
            query_keys=("season",),
        ),
    ),
    debug_panels=("/advisor/debug/parcels/{id}",),
    demo_presets=(
        "farmcore.apps.advisor.demo:LIVE_LOOP",
        "farmcore.apps.advisor.demo:SEASON_2024",
    ),
    tools=TOOLS,
    maturity="alpha",
)

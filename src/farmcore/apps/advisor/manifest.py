"""Manifest of app `advisor`: the `advisor.evaluate` job, advice rows, the parcel advice card.

The job name `advisor.evaluate` is a contract: chat calls `jobs.run_now` with it after a confirm
(in live mode that queues the farm's durable task and waits briefly for it).
"""

from datetime import timedelta

from farmcore.apps.advisor.assessment import KIND
from farmcore.apps.advisor.tools import TOOLS
from farmcore.core.access import SCOPES
from farmcore.core.manifest import JobSpec, Manifest, MapLayer, ParcelView, Widget

JOB_NAME = "advisor.evaluate"
EVALUATION_INTERVAL = timedelta(hours=1)

MANIFEST = Manifest(
    name="advisor",
    version="0.1.0",
    summary="Tells you which parcels to spray, when, and why.",
    depends=("fields", "stations", "operations"),
    # advice:read is the app's own; the others are the reads its job and card make.
    scopes=("advice:read", "parameters:read", "fields:read", "stations:read", "logs:read"),
    models=("farmcore.apps.advisor.catalog", "farmcore.apps.advisor.advice"),
    demo_counts="farmcore.apps.advisor.demo:decision_counts",
    query_values=(("model", ("all", "downy", "powdery")),),
    # Shared reference data, like `crop_protection_item` (planning and chat read it too).
    resource_scopes=(
        ("advisor_product", tuple(sorted(SCOPES))),
        ("advice", ("advice:read",)),  # stored advice: crop protection's own read scope
    ),
    jobs=(
        JobSpec(
            name=JOB_NAME,
            func="farmcore.apps.advisor.job:evaluate",
            interval=EVALUATION_INTERVAL,
            task="farmcore.apps.advisor.job:evaluate_live",
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
    icon="advisor",
    color="protected",
    keywords={
        "en": ("crop protection", "spray", "disease", "mildew", "advice"),
        "fr": ("protection des cultures", "pulvérisation", "mildiou", "conseil"),
        "es": ("protección de cultivos", "mildiu", "consejo"),
        "it": ("protezione delle colture", "peronospora", "consiglio"),
    },
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
    assessment_kinds=(KIND,),
    maturity="alpha",
)

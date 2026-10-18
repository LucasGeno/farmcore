"""Manifest of app `stations`: Météo-France replay ingest, hourly chart, connector health."""

from datetime import timedelta

from farmcore.apps.stations.tools import TOOLS
from farmcore.core.manifest import JobSpec, Manifest, MapLayer, Widget

MANIFEST = Manifest(
    data_sources="farmcore.apps.stations.data_sources:summary",
    name="stations",
    version="0.1.0",
    summary="Rain, temperature and wind from your weather station.",
    depends=("core", "fields"),
    scopes=("stations:read",),
    jobs=(
        JobSpec(
            name="stations.ingest_meteofrance",
            func="farmcore.apps.stations.jobs:ingest_meteofrance",
            interval=timedelta(hours=1),
        ),
    ),
    routes=(
        # Before routes: `/stations/{station_id}` would otherwise catch `/stations/today`.
        "farmcore.apps.stations.today:router",
        "farmcore.apps.stations.layers:router",
        "farmcore.apps.stations.routes:router",
    ),
    seed="farmcore.apps.stations.seed:seed",
    title="Weather",
    category="Weather",
    nav=("Weather", "/stations/"),
    nav_group="apps",
    nav_order=20,
    nav_icon="station",
    widgets=(Widget("/stations/today?compact=1", 20),),
    map_layers=(
        MapLayer("weather", "Weather", "/stations/layer.geojson", "points", 20, False, "station"),
    ),
    tools=TOOLS,
    maturity="alpha",
)

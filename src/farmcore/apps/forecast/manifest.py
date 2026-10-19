"""Saved forecasts and bounded live receipts, ranked for spraying."""

from datetime import timedelta

from farmcore.core.manifest import JobSpec, Manifest, Widget

MANIFEST = Manifest(
    data_sources="farmcore.apps.forecast.data_sources:summary",
    name="forecast",
    version="0.1.0",
    summary="The next five days ranked for spraying, from an imported forecast.",
    depends=("stations", "advisor"),
    scopes=("stations:read", "advice:read", "fields:read"),
    models=("farmcore.apps.forecast.models",),
    routes=("farmcore.apps.forecast.routes:router",),
    jobs=(
        JobSpec(
            "forecast.fetch_live", "farmcore.apps.forecast.jobs:fetch_live", timedelta(hours=1)
        ),
    ),
    seed="farmcore.apps.forecast.seed:seed",
    widgets=(
        Widget("/forecast/home-weather", 21, label="Forecast weather"),
        Widget("/forecast/today", 50, label="Spray window"),
    ),
    parcel_sections=("/forecast/parcels/{id}",),
    nav=("Plan", "/forecast/plan"),
    nav_group="farmer",
    nav_order=30,
    nav_icon="calendar",
    title="Spray window",
    category="Weather",
    maturity="alpha",
)

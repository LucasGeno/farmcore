"""Manifest of app `forecast`: imported weather forecasts (`farmcore forecast import`) and the
graded spray window on Today and the parcel advice card.

No job and no live call: forecasts reach the database only from saved files (`farmcore forecast
import`, or this app's seed reading `DATA_DIR/open-meteo/`), each run with its run and
availability time. Switching the app off removes the window; the advice itself never reads it.
"""

from farmcore.core.manifest import Manifest, Widget

MANIFEST = Manifest(
    data_sources="farmcore.apps.forecast.data_sources:summary",
    name="forecast",
    version="0.1.0",
    summary="The next five days ranked for spraying, from an imported forecast.",
    depends=("stations", "advisor"),
    scopes=("stations:read", "advice:read", "fields:read"),
    models=("farmcore.apps.forecast.models",),
    routes=("farmcore.apps.forecast.routes:router",),
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

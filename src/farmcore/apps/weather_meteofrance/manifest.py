"""Manifest of app `weather_meteofrance`: live Météo-France station observations.

No UI surface: the hours land under the farm's existing Météo-France station, which the Weather
app shows. Runs only in live clock mode, with the installation's portal credentials
(`METEOFRANCE_CLIENT_ID`, `METEOFRANCE_CLIENT_SECRET`).
"""

from farmcore.apps.weather_meteofrance.ingest import DEADLINE, INTERVAL, JOB_NAME
from farmcore.core.manifest import JobSpec, Manifest

MANIFEST = Manifest(
    name="weather_meteofrance",
    version="0.1.0",
    summary="Live observations from your Météo-France station (DPObs, DPClim archive).",
    depends=("core", "stations"),
    scopes=("stations:read",),
    jobs=(
        JobSpec(
            name=JOB_NAME,
            func="farmcore.apps.weather_meteofrance.ingest:ingest",
            interval=INTERVAL,
            task="farmcore.apps.weather_meteofrance.ingest:ingest_farm",
            deadline=DEADLINE,
        ),
    ),
    title="Météo-France live",
    category="Weather",
    icon="station",
    color="wait",
    keywords={
        "en": ("weather", "Météo-France", "live observations", "station"),
        "fr": ("météo", "Météo-France", "observations", "station"),
        "es": ("tiempo", "Météo-France", "observaciones", "estación"),
        "it": ("meteo", "Météo-France", "osservazioni", "stazione"),
    },
    weather_providers=("farmcore.apps.weather_meteofrance.provider:provider",),
    maturity="alpha",
)

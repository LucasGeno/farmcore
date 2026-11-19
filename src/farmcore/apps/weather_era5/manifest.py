"""Manifest of app `weather_era5`: season history from ERA5 through Copernicus.

No UI surface: the hours land under an ERA5 grid-point station, flagged as model data, which
the Weather app and the merge layer read. Runs only in live clock mode, with the installation's
free Copernicus account key (`CDSAPI_KEY`).
"""

from farmcore.apps.weather_era5.ingest import DEADLINE, INTERVAL, JOB_NAME
from farmcore.core.manifest import JobSpec, Manifest

MANIFEST = Manifest(
    name="weather_era5",
    version="0.1.0",
    summary="Season history for your farm's pin from ERA5 (Copernicus), marked as model data.",
    depends=("core", "stations"),
    scopes=("stations:read",),
    jobs=(
        JobSpec(
            name=JOB_NAME,
            func="farmcore.apps.weather_era5.ingest:ingest",
            interval=INTERVAL,
            task="farmcore.apps.weather_era5.ingest:backfill_farm",
            deadline=DEADLINE,
        ),
    ),
    title="ERA5 history",
    category="Weather",
    icon="history",
    color="muted",
    keywords={
        "en": ("weather", "ERA5", "history", "reanalysis", "Copernicus"),
        "fr": ("météo", "ERA5", "historique", "réanalyse", "Copernicus"),
        "es": ("tiempo", "ERA5", "historial", "reanálisis", "Copernicus"),
        "it": ("meteo", "ERA5", "storico", "rianalisi", "Copernicus"),
    },
    weather_providers=("farmcore.apps.weather_era5.provider:provider",),
    maturity="alpha",
)

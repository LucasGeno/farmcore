"""Manifest of app `weather_push`: readings pushed by the farm's own sensors.

A device (a station or soil probe) is registered with `farmcore weather-push add-device`: a
personal access token on a grant holding only `weather:push` (it shows and is revoked on
Sharing like any grant), and the device's own station. It then posts SensorThings-shaped JSON or
CSV to `POST /weather_push/observations` (`routes`). No UI surface: the readings join the farm's
weather as on-farm sensors.
"""

from farmcore.core.manifest import Manifest

MANIFEST = Manifest(
    name="weather_push",
    version="0.1.0",
    summary="Readings pushed by your own weather station or soil moisture sensor.",
    depends=("core",),
    routes=("farmcore.apps.weather_push.routes:router",),
    title="Sensor push",
    category="Weather",
    icon="station",
    color="wait",
    keywords={
        "en": ("weather", "sensor", "soil moisture", "station", "push"),
        "fr": ("météo", "capteur", "humidité du sol", "station"),
        "es": ("tiempo", "sensor", "humedad del suelo", "estación"),
        "it": ("meteo", "sensore", "umidità del suolo", "stazione"),
    },
    maturity="alpha",
)

"""Manifest of app `onboarding`: the first-run location step (no models, no jobs)."""

from farmcore.core.manifest import Manifest, Widget

MANIFEST = Manifest(
    name="onboarding",
    version="0.1.0",
    summary="Set your farm's location so times and weather are right.",
    depends=("core",),
    routes=("farmcore.apps.onboarding.routes:router",),
    seed="farmcore.apps.onboarding.seed:seed",
    title="Set up your farm",
    category="Getting started",
    nav=("Set up", "/onboarding/"),
    nav_group="more",
    nav_order=10,
    nav_icon="map",
    icon="map",
    color="brand",
    keywords={
        "en": ("location", "farm", "place", "set up"),
        "fr": ("emplacement", "ferme", "lieu", "installation"),
        "es": ("ubicación", "granja", "lugar", "configuración"),
        "it": ("posizione", "azienda", "luogo", "configurazione"),
    },
    widgets=(Widget("/onboarding/today", 5, "full", label="Farm setup"),),
    maturity="alpha",
)

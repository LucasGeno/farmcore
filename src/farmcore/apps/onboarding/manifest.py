"""Manifest of app `onboarding`: the first-run location step (no models, no jobs)."""

from farmcore.core.manifest import DraftKind, Manifest, NavItem, Widget

MANIFEST = Manifest(
    name="onboarding",
    version="0.1.0",
    summary="Set your farm's location, crops and apps.",
    depends=("core",),
    routes=(
        "farmcore.apps.onboarding.routes:router",
        "farmcore.apps.onboarding.import_routes:router",
        "farmcore.apps.onboarding.routes_setup:router",
    ),
    draft_kinds=(
        DraftKind(
            "parcels",
            "farmcore.apps.onboarding.importers.service:validate",
            "/onboarding/import/review",
            "farmcore.apps.onboarding.importers.service:confirm",
            max_payload_bytes=2_000_000,
        ),
    ),
    seed="farmcore.apps.onboarding.seed:seed",
    title="Set up your farm",
    category="Getting started",
    nav=("Set up", "/onboarding/"),
    nav_group="more",
    nav_order=10,
    nav_icon="map",
    extra_nav=(
        NavItem("Import parcels", "/onboarding/import", "more", 11, "map"),
        NavItem("Crops", "/onboarding/crops", "more", 12, "leaf"),
        NavItem("App bundles", "/onboarding/bundles", "more", 13, "apps"),
    ),
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

"""Manifest of the kernel itself (app `core`)."""

from datetime import timedelta

from farmcore.core.manifest import JobSpec, Manifest, NavItem, Widget

MANIFEST = Manifest(
    name="core",
    version="0.1.0",
    summary="Kernel: app registry, clock and jobs, canonical farm model, timeline.",
    models=("farmcore.core.models",),
    routes=(
        "farmcore.core.routes_activity:router",
        "farmcore.core.routes_sources:router",
        "farmcore.core.routes_apps:router",
        "farmcore.core.routes_clock:router",
        "farmcore.core.routes_farms:router",
        "farmcore.core.routes_prefs:router",
        "farmcore.core.routes_tour:router",
        "farmcore.core.routes_demo:router",
        "farmcore.core.routes_admin:router",
        "farmcore.core.routes_identity:router",
        "farmcore.core.routes_members:router",
        "farmcore.core.routes_drafts:router",
        "farmcore.core.debug:router",
    ),
    seed="farmcore.core.seed:seed",
    # Marks open drafts past their wall-clock expiry; confirming one is refused either way.
    jobs=(JobSpec("drafts.expire", "farmcore.core.drafts:expire_job", timedelta(minutes=15)),),
    nav=("Home", "/"),
    nav_group="farmer",
    nav_order=10,
    nav_icon="home",
    icon="leaf",
    color="brand",
    keywords={
        "en": ("apps", "settings", "data sources"),
        "fr": ("applications", "paramètres", "sources de données"),
        "es": ("aplicaciones", "ajustes", "fuentes de datos"),
        "it": ("applicazioni", "impostazioni", "fonti di dati"),
    },
    # The phone bottom nav is core-only chrome: Home, Map, Plan, Activity (Search and Record
    # arrive with 13/05). Apps cannot add slots; these point at the app pages they used to own.
    extra_nav=(
        NavItem("Map", "/fields/", "farmer", 20, "map"),
        NavItem("Plan", "/forecast/plan", "farmer", 30, "calendar"),
        NavItem("Activity", "/activity/", "farmer", 40, "history"),
        NavItem("Demo", "/demo/", "admin", 10, "clock"),
        NavItem("Data sources", "/data-sources/", "more", 15, "database"),
    ),
    # Recent activity on Today: after the Crop protection hero (10) and Weather (20).
    widgets=(
        Widget("/activity/today", 40, "half", "Recent activity"),
        Widget("/data-sources/today", 60, "full", "Data sources"),
    ),
    parcel_sections=("/data-sources/parcels/{id}",),
    section_labels=(("/data-sources/parcels/{id}", "Data sources"),),
    maturity="alpha",
)

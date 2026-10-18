"""Manifest of the kernel itself (app `core`)."""

from farmcore.core.manifest import Manifest, NavItem, Widget

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
        "farmcore.core.debug:router",
    ),
    seed="farmcore.core.seed:seed",
    nav=None,
    extra_nav=(
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

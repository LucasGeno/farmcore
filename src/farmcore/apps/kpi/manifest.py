"""Manifest of app `kpi`: one replayed station season as KPIs (no seed, no jobs)."""

from farmcore.core.manifest import Manifest

MANIFEST = Manifest(
    name="kpi",
    version="0.1.0",
    summary="What the season saved: sprays, copper, time and money.",
    depends=("stations", "advisor", "operations"),
    scopes=("stations:read", "logs:read", "fields:read"),
    routes=("farmcore.apps.kpi.routes:router", "farmcore.apps.kpi.map_section:router"),
    title="Season & impact",
    category="Insights",
    nav=("Season & impact", "/kpi/"),
    nav_group="apps",
    nav_order=30,
    nav_icon="chart",
    # Map panel: Farm today's one link to Season & impact, no figures.
    farm_sections=("/kpi/map/farm",),
    maturity="alpha",
)

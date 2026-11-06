"""Persistent planned spray jobs from stored advice; planned work is never completed work."""

from farmcore.core.manifest import Manifest

MANIFEST = Manifest(
    name="planning",
    version="0.1.0",
    summary="Planned spray jobs from stored advice, with revisions and farmer availability.",
    category="Crop protection",
    depends=("fields", "operations", "advisor", "forecast"),
    scopes=("planning:read", "planning:write", "fields:read", "logs:read", "advice:read"),
    models=("farmcore.apps.planning.models",),
    routes=("farmcore.apps.planning.routes:router",),
    demo_reset=("farmcore.apps.planning.demo:reset",),
    title="Planned jobs",
    nav=("Planned jobs", "/planning/"),
    nav_group="apps",
    nav_order=15,
    nav_icon="calendar",
    maturity="alpha",
)

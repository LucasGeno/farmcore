"""Reviewed agronomic parameter versions; opinions never imply field accuracy."""

from farmcore.apps.review.tools import TOOLS
from farmcore.core.manifest import Manifest, NavItem

MANIFEST = Manifest(
    name="review",
    version="0.1.0",
    summary="Farm-scoped agronomic parameter versions and human review history.",
    category="Crop protection",
    depends=("advisor",),
    scopes=("advice:read", "parameters:read"),
    models=("farmcore.apps.review.models",),
    routes=("farmcore.apps.review.routes:router",),
    title="Parameter review",
    nav=("Parameter review", "/review/"),
    extra_nav=(
        NavItem("Recommendation review", "/review/recommendations/", "more", 36, "advisor"),
    ),
    nav_group="more",
    nav_order=35,
    tools=TOOLS,
)

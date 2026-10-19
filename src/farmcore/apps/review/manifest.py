"""Reviewed agronomic parameter versions; opinions never imply field accuracy."""

from farmcore.core.manifest import Manifest

MANIFEST = Manifest(
    name="review",
    version="0.1.0",
    summary="Farm-scoped agronomic parameter versions and human review history.",
    category="Crop protection",
    depends=("advisor",),
    scopes=("advice:read", "parameters:read"),
    models=("farmcore.apps.review.models",),
)

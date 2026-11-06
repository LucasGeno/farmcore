"""Persistent planned spray jobs from stored advice; planned work is never completed work."""

from farmcore.core.manifest import Manifest

MANIFEST = Manifest(
    name="planning",
    version="0.1.0",
    summary="Planned spray jobs from stored advice, with revisions and farmer availability.",
    category="Crop protection",
    # Schema scaffold only: declaring real dependencies (fields, operations, advisor, forecast,
    # review) blocks switching those apps off, so it lands with the routes and their tests.
    models=("farmcore.apps.planning.models",),
    title="Planned jobs",
)

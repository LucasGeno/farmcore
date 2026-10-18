"""Manifest of app `today`: the home page. No models, no jobs; it never imports another app."""

from farmcore.core.manifest import Manifest

MANIFEST = Manifest(
    name="today",
    version="0.1.0",
    summary="Your home page: what to do on the farm today.",
    depends=("core",),
    routes=("farmcore.apps.today.routes:router",),
    title="Home",
    category="Essentials",
    required=True,
    nav=("Home", "/"),
    nav_group="farmer",
    nav_order=10,
    nav_icon="home",
    maturity="alpha",
)

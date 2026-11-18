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
    icon="home",
    color="brand",
    keywords={
        "en": ("home", "today", "overview"),
        "fr": ("accueil", "aujourd'hui", "aperçu"),
        "es": ("inicio", "hoy", "resumen"),
        "it": ("home", "oggi", "riepilogo"),
    },
    maturity="alpha",
)

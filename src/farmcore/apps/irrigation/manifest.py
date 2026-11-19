"""Manifest of app `irrigation`: sourced crop coefficients (pure domain, no routes yet)."""

from farmcore.core.manifest import Manifest

MANIFEST = Manifest(
    name="irrigation",
    version="0.1.0",
    summary="Sourced crop coefficients (Kc) by crop and growth stage.",
    depends=("core",),
    title="Irrigation",
    category="Insights",
    icon="rain",
    color="brand",
    maturity="alpha",
)

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
    keywords={
        "en": ("irrigation", "crop coefficient", "kc"),
        "fr": ("irrigation", "coefficient cultural"),
        "es": ("riego", "coeficiente de cultivo"),
        "it": ("irrigazione", "coefficiente colturale"),
    },
    maturity="alpha",
)

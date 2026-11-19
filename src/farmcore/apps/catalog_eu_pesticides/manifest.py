"""Manifest of app `catalog_eu_pesticides`: the EU's active-substance approval register.

No UI surface: the register is imported weekly and read by the record sheet's product picker to
show a substance's approval status and expiry. The job only downloads in live clock mode. Other
country catalogs (`catalog_it_salute`, `catalog_fr_ephy`) depend on this app for the normalised
substance key that links their products to these substances.
"""

from datetime import timedelta

from farmcore.core.manifest import JobSpec, Manifest

MANIFEST = Manifest(
    name="catalog_eu_pesticides",
    version="0.1.0",
    summary="EU plant-protection active substances and their approval status.",
    models=("farmcore.apps.catalog_eu_pesticides.models",),
    jobs=(
        JobSpec(
            "catalog_eu_pesticides.import",
            "farmcore.apps.catalog_eu_pesticides.jobs:import_substances",
            timedelta(weeks=1),
        ),
    ),
    title="EU active substances",
    category="Compliance",
    icon="database",
    color="brand",
    keywords={
        "en": ("catalog", "active substance", "approval", "pesticides", "EU"),
        "fr": ("catalogue", "substance active", "approbation", "pesticides", "UE"),
        "es": ("catálogo", "sustancia activa", "aprobación", "plaguicidas", "UE"),
        "it": ("catalogo", "sostanza attiva", "approvazione", "pesticidi", "UE"),
    },
    maturity="alpha",
)

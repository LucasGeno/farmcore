"""Manifest of app `catalog_it_salute`: Italy's plant-protection product register.

No UI surface: the register is imported weekly and read by the record sheet's product picker. The
job only downloads in live clock mode. The app depends on `catalog_eu_pesticides` for the
normalised substance key that links each product's active substances to the EU approval register.
"""

from datetime import timedelta

from farmcore.core.manifest import JobSpec, Manifest

MANIFEST = Manifest(
    name="catalog_it_salute",
    version="0.1.0",
    summary="Italy's register of plant-protection products authorised by the Ministero della "
    "Salute.",
    depends=("catalog_eu_pesticides",),
    jobs=(
        JobSpec(
            "catalog_it_salute.import",
            "farmcore.apps.catalog_it_salute.jobs:import_salute",
            timedelta(weeks=1),
        ),
    ),
    title="IT plant-protection catalog",
    category="Compliance",
    icon="database",
    color="brand",
    keywords={
        "en": ("catalog", "plant protection", "product register", "fitosanitari", "Italy"),
        "fr": ("catalogue", "phytosanitaire", "produits", "fitosanitari", "Italie"),
        "es": ("catálogo", "fitosanitario", "productos", "fitosanitari", "Italia"),
        "it": ("catalogo", "fitosanitario", "prodotti", "fitosanitari", "Italia"),
    },
    maturity="alpha",
)

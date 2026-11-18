"""Manifest of app `catalog_fr_ephy`: France's official plant-protection product register.

No UI surface: the catalog is imported weekly and read by the record sheet's product picker
(records-compliance 04). The job only downloads in live clock mode.
"""

from datetime import timedelta

from farmcore.core.manifest import JobSpec, Manifest

MANIFEST = Manifest(
    name="catalog_fr_ephy",
    version="0.1.0",
    summary="France's official register of plant-protection products and their registered uses.",
    models=("farmcore.apps.catalog_fr_ephy.models",),
    jobs=(
        JobSpec(
            "catalog_fr_ephy.import",
            "farmcore.apps.catalog_fr_ephy.jobs:import_ephy",
            timedelta(weeks=1),
        ),
    ),
    title="FR plant-protection catalog",
    category="Compliance",
    icon="database",
    color="brand",
    keywords={
        "en": ("catalog", "plant protection", "product register", "E-Phy"),
        "fr": ("catalogue", "phytosanitaire", "produits", "E-Phy"),
        "es": ("catálogo", "fitosanitario", "productos", "E-Phy"),
        "it": ("catalogo", "fitosanitario", "prodotti", "E-Phy"),
    },
    maturity="alpha",
)

# E-Phy recorded fixture

A frozen subset of the weekly E-Phy archive, used by the `catalog_fr_ephy` tests (tests never
download). Regenerate only when the upstream format drifts: `uv run python scripts/make_ephy_fixture.py`.

- **Source:** Anses, "Données ouvertes du catalogue E-Phy des produits phytopharmaceutiques,
  matières fertilisantes et supports de culture, adjuvants, produits mixtes et mélanges"
  (data.gouv.fr), the `decisionamm-intrant-format-csv-20261117-utf8.zip` and
  `-windows-1252.zip` archives of 2026-11-17.
- **Licence:** Licence Ouverte / Open Licence (data.gouv `fr-lo`, v1.0). Cite « Données E-Phy -
  Anses ».
- **Contents:** 202 products (100 authorised, 100 withdrawn, plus one composite-AMM MELANGE
  product and one product with `Autorisé (Provisoire)` uses) and their 1,509 usage rows, in both
  encodings the source publishes. Withdrawn products are kept so history stays linkable.

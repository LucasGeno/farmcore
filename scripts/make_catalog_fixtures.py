"""Regenerate the recorded EU-substance and Italian-product test fixtures.

The committed fixtures under `tests/apps/catalog_eu_pesticides/fixtures/` and
`tests/apps/catalog_it_salute/fixtures/` are small, frozen subsets of the two official registers;
tests read them, they never download. Regenerate only when the upstream format drifts.

Sources:
- EU: the DG SANTE active-substances download
  (https://api.datalake.sante.service.ec.europa.eu/sante/pesticides/active-substances-download?format=json&api-version=v3.0),
  one JSON object per line. Commission re-use (CC BY 4.0); cite « European Commission, EU
  Pesticides Database (DG SANTE) ».
- Italy: the Ministero della Salute "Fitosanitari" open data
  (https://www.dati.salute.gov.it/it/dataset/fitosanitari/), a JSON array. Italian Open Data
  Licence v2.0 (IODL 2.0); cite « Ministero della Salute — Prodotti fitosanitari ».

Usage:
    uv run python scripts/make_catalog_fixtures.py active-substances.ndjson prodotti.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

EU_FIXTURES = Path("tests/apps/catalog_eu_pesticides/fixtures")
IT_FIXTURES = Path("tests/apps/catalog_it_salute/fixtures")

# EU substances selected by id; one id is repeated to pin the exact-duplicate collapse, and 472
# (Boscalid, a `(formerly …)` name) pins the alias lookup.
EU_SUBSTANCE_IDS = (811, 486, 1028, 194, 1280, 472, 811)
# Italian products selected by registration number: an authorised product whose substance links
# to the EU register (Buprofezin), one with a `|`-separated pair, one with a `(formerly …)`
# qualifier, a provisional, a revoked, an expired and a suspended.
IT_NUMBERS = ("000001", "000017", "015230", "012862", "012498", "004662", "010128")


def main(eu_path: str, it_path: str) -> None:
    eu_lines: list[str] = []
    for line in Path(eu_path).read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record.get("substance_id") in EU_SUBSTANCE_IDS:
            eu_lines.append(line.rstrip("\n"))
    EU_FIXTURES.mkdir(parents=True, exist_ok=True)
    (EU_FIXTURES / "active_substances.ndjson").write_text(
        "\n".join(eu_lines) + "\n", encoding="utf-8"
    )

    records = json.loads(Path(it_path).read_text(encoding="utf-8"))
    chosen = [r for r in records if r.get("num_registrazione") in IT_NUMBERS]
    IT_FIXTURES.mkdir(parents=True, exist_ok=True)
    (IT_FIXTURES / "products.json").write_text(
        json.dumps(chosen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print(f"wrote {len(eu_lines)} EU substances and {len(chosen)} IT products")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])

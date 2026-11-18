"""Regenerate the recorded E-Phy test fixture.

The committed fixture under `tests/apps/catalog_fr_ephy/fixtures/` is a small, frozen subset of
the weekly E-Phy archive (about 200 products — half authorised, half withdrawn — with all their
usage rows, in both the UTF-8 and Windows-1252 encodings the source publishes). It is regenerated
only when the upstream format drifts, and it is what the tests read: tests never download.

Source: the two "decisionamm-intrant-format-csv-<date>-{utf8,windows-1252}.zip" archives of
https://www.data.gouv.fr/fr/datasets/donnees-ouvertes-du-catalogue-e-phy-des-produits-phytopharmaceutiques-matieres-fertilisantes-et-supports-de-culture-adjuvants-produits-mixtes-et-melanges
(Anses, Licence Ouverte / Open Licence 2.0; cite « Données E-Phy - Anses »).

Usage:
    uv run python scripts/make_ephy_fixture.py /extracted/utf8 /extracted/windows-1252
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

FIXTURES = Path("tests/apps/catalog_fr_ephy/fixtures")
AUTHORISED = 100
WITHDRAWN = 100

AMM = "numero AMM"
ETAT = "Etat d’autorisation"


def _load(path: Path, encoding: str) -> tuple[list[str], list[list[str]]]:
    with path.open(encoding=encoding, newline="") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader)
        rows = list(reader)
    return header, rows


def _write(path: Path, header: list[str], rows: list[list[str]], encoding: str) -> None:
    with path.open("w", encoding=encoding, newline="") as f:
        writer = csv.writer(f, delimiter=";", lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def main(utf8_dir: str, cp1252_dir: str) -> None:
    utf8 = Path(utf8_dir)
    cp1252 = Path(cp1252_dir)
    products = "produits_utf8.csv"
    uses = "produits_usages_utf8.csv"
    if not (utf8 / products).is_file() or not (utf8 / uses).is_file():
        sys.exit(f"{utf8} must contain {products} and {uses}")

    header, product_rows = _load(utf8 / products, "utf-8")
    amm_at = header.index(AMM)
    etat_at = header.index(ETAT)

    chosen: list[list[str]] = []
    amms: set[str] = set()
    for bucket in ("AUTORISE", "RETIRE"):
        taken = 0
        for row in product_rows:
            if row[etat_at].strip() == bucket:
                chosen.append(row)
                amms.add(row[amm_at].strip())
                taken += 1
                if taken >= (AUTHORISED if bucket == "AUTORISE" else WITHDRAWN):
                    break

    use_header, use_rows = _load(utf8 / uses, "utf-8")
    use_amm_at = use_header.index(AMM)
    kept_uses = [row for row in use_rows if row[use_amm_at].strip() in amms]

    # The Windows-1252 archive is the same data in the other encoding; a few characters in the
    # UTF-8 file are not representable in cp1252, so the cp1252 fixture is read from the cp1252
    # archive rather than re-encoded.
    cp_products = cp1252 / "produits_Windows-1252.csv"
    cp_uses = cp1252 / "produits_usages_Windows-1252.csv"
    if not cp_products.is_file() or not cp_uses.is_file():
        sys.exit(f"{cp1252} must contain produits_Windows-1252.csv and its usages file")
    cp_header, cp_rows = _load(cp_products, "cp1252")
    cp_amm_at = cp_header.index(AMM)
    cp_use_header, cp_use_rows = _load(cp_uses, "cp1252")
    cp_use_amm_at = cp_use_header.index(AMM)

    FIXTURES.mkdir(parents=True, exist_ok=True)
    _write(FIXTURES / "products_utf8.csv", header, chosen, "utf-8")
    _write(FIXTURES / "uses_utf8.csv", use_header, kept_uses, "utf-8")
    _write(
        FIXTURES / "products_cp1252.csv",
        cp_header,
        [row for row in cp_rows if row[cp_amm_at].strip() in amms],
        "cp1252",
    )
    _write(
        FIXTURES / "uses_cp1252.csv",
        cp_use_header,
        [row for row in cp_use_rows if row[cp_use_amm_at].strip() in amms],
        "cp1252",
    )

    print(f"wrote {len(chosen)} products and {len(kept_uses)} uses into {FIXTURES}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])

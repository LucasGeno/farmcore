# Fitosanitari recorded fixture

A frozen subset of the Ministero della Salute "Fitosanitari" open-data download, used by the
`catalog_it_salute` tests (tests never download). Regenerate only when the upstream format
drifts: `uv run python scripts/make_catalog_fixtures.py active-substances.ndjson prodotti.json`.

- **Source:** Ministero della Salute, "Open Data — Fitosanitari"
  (https://www.dati.salute.gov.it/it/dataset/fitosanitari/), the `PROD_FTS_6_20261116.json`
  download of 2026-11-16.
- **Licence:** Italian Open Data Licence v2.0 (IODL 2.0) — not CC BY 4.0. Cite « Ministero della
  Salute — Prodotti fitosanitari ».
- **Contents:** six products — two authorised (one whose substance links to the EU register, one
  with a `|`-separated substance pair), one provisional, one revoked, one expired and one
  suspended — so every normalised state and the substance split are tested.

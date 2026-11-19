# EU active-substances recorded fixture

A frozen subset of the DG SANTE active-substances download, used by the `catalog_eu_pesticides`
tests (tests never download). Regenerate only when the upstream format drifts:
`uv run python scripts/make_catalog_fixtures.py active-substances.ndjson prodotti.json`.

- **Source:** European Commission, DG SANTE "Pesticides — APIs V3.0" active-substances download
  (https://api.datalake.sante.service.ec.europa.eu/sante/pesticides/active-substances-download?format=json&api-version=v3.0),
  the same dataset the EU Pesticides Database search uses. No API key. Retrieved 2026-11-19.
- **Licence:** Commission re-use, Decision 2011/833/EU — CC BY 4.0. Cite « European Commission,
  EU Pesticides Database (DG SANTE) ».
- **Contents:** six substances — Glyphosate (approved), Buprofezin (approved), Sulphur
  (approved, no CAS), Warfarin (aka coumaphene) (not approved) and Aluminium potassium sulphate
  dodecahydrate (pending), plus Boscalid (formerly nicobifen) — with the source's repeated rows (one per national authorisation)
  left in place, so the importer's exact-duplicate collapse is exercised.

# Bundled country boundaries

`countries.geojson` is a cut-down copy of **Natural Earth 1:110m Admin 0 – Countries**
(`ne_110m_admin_0_countries`). Natural Earth is in the **public domain**
(<https://www.naturalearthdata.com/about/terms-of-use/>); attribution is not required but is
recorded here and in `THIRD_PARTY_NOTICES.md`.

- Version: **Natural Earth v5.1.2**, repository commit
  `ca96624a56bd078437bca8184e78163e5039ad19` (2022-06-02).
- Source: <https://github.com/nvkelso/natural-earth-vector/blob/ca96624a56bd078437bca8184e78163e5039ad19/geojson/ne_110m_admin_0_countries.geojson>
- Downloaded: 2026-11-18.
- Transform: `python scripts/trim_ne_countries.py <source> <this file>` keeps only `ISO_A2_EH`
  (as `iso`) and `NAME_EN` (as `name`), rounds polygon coordinates to three decimal places
  (about 110 m at the equator) and drops features without an ISO alpha-2 code. Nothing is
  dissolved or invented.
- Used by `farmcore.core.farm_location.locate` to name the country of a point offline. A border
  point is deliberately not guessed: the owner confirms it.

Refresh by re-running the same selection on the current Natural Earth release.

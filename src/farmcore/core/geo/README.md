# Bundled country boundaries

`countries.geojson` is a cut-down copy of **Natural Earth 1:110m Admin 0 – Countries**
(`ne_110m_admin_0_countries`). Natural Earth is in the **public domain**
(<https://www.naturalearthdata.com/about/terms-of-use/>); attribution is not required but is
recorded here and in `THIRD_PARTY_NOTICES.md`.

- Source: <https://github.com/nvkelso/natural-earth-vector/blob/master/geojson/ne_110m_admin_0_countries.geojson>
- Downloaded: 2026-11-18
- Transform: each feature keeps only `ISO_A2_EH` (as `iso`) and `NAME_EN` (as `name`); polygon
  coordinates are rounded to three decimal places (about 110 m at the equator); features without
  an ISO alpha-2 code are dropped. Nothing is dissolved or invented.
- Used by `farmcore.core.farm_location.locate` to name the country of a point offline. A border
  point is deliberately not guessed: the owner confirms it.

Refresh by re-running the same selection on the current Natural Earth release.

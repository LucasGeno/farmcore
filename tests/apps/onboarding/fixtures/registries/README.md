# Recorded public registry responses

Recorded 2026-11-19 with GET requests; no live calls are made in tests.
These are small public-data fixture excerpts, not farm/customer data.

- `rpg.json`: IGN APICarto `/api/rpg/v2`, `annee=2024`, `geom` a WGS84
  rectangle with bounds `-0.290,48.960,-0.289,48.961`. Complete response, unchanged.
  Attribution: IGN — Registre parcellaire graphique. Licence: Etalab 2.0
  ([IGN RPG](https://geoservices.ign.fr/rpg),
  [licence](https://www.etalab.gouv.fr/licence-ouverte-open-licence/)).
  [API catalogue](https://apicarto.ign.fr/api/doc/rpg) lists 2024 as its newest year.
- `recintos.json`, `cultivo_declarado.json`: FEGA SIGPAC OGC API Features,
  `https://sigpac-hubcloud.es/ogcapi/collections/{collection}/items`,
  `bbox=-4.80,41.70,-4.799,41.701&f=json&limit=200`.
  Each excerpt retains one feature (municipio 67), with its original geometry.
  Only public parcel identifiers and the declared crop code are retained;
  administrative declaration fields (including expediente and aid codes) are removed.
  Counts are updated to the excerpt size and response navigation links removed.
  Attribution: FEGA — SIGPAC. Licence: CC BY 4.0. The service's
  [terms](https://sigpac-hubcloud.es/ogcapi?f=html) and
  [service description](https://sigpac-hubcloud.es/html/ogc-api/descServicio.html)
  explicitly state [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
  The responses do not publish a campaign year; it remains unknown, never inferred
  from the retrieval date. Recinto `47:67:0:0:23:443:5` has crop code `1`.

Production retains the same identifier/crop allowlist, normalized boundaries,
licence and attribution. It does not retain or expose raw declaration properties.

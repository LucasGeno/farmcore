# Third-party licences and data sources

FarmCore's own code is licensed under [Apache-2.0](LICENSE). That licence does not cover the
third-party libraries, fonts, data and map tiles listed here; each keeps its own terms.

## Python libraries

Exact versions are pinned in `uv.lock`; each package keeps its own licence.

| Package | Licence | Used for |
|---|---|---|
| fastapi, starlette | MIT, BSD-3-Clause | Web app and API |
| uvicorn[standard] | BSD-3-Clause | ASGI server |
| sqlalchemy, geoalchemy2, alembic | MIT | Database access, PostGIS columns, migrations |
| psycopg[binary] | **LGPL-3.0-only** | PostgreSQL driver |
| pydantic, pydantic-settings | MIT | Validation, settings |
| jinja2 | BSD-3-Clause | HTML templates |
| python-multipart | Apache-2.0 | File uploads (ISOXML) |
| httpx | BSD-3-Clause | HTTP client (Vertex AI) |
| google-auth[requests] | Apache-2.0 | Vertex AI credentials |
| apscheduler | MIT | Live-mode job scheduler |
| python-telegram-bot | **LGPL-3.0-only** | Telegram bot |
| faster-whisper (ctranslate2, onnxruntime, av) | MIT; av BSD-3-Clause, its wheels bundle FFmpeg libraries (LGPL) | Local speech-to-text |
| typer | MIT | Command line |

The LGPL libraries are used unmodified, as separately installed packages imported by FarmCore.
Transitive dependencies are permissive (MIT, BSD, Apache-2.0, ISC, PSF, MPL-2.0 for `certifi`
and `tqdm`). If you redistribute container images or wheels, review the licences of the
binaries they contain; this file is not a binary licence-compliance audit.

Development tools: pytest (MIT), ruff (MIT), uv (Apache-2.0 OR MIT), hatchling (MIT).

## Bundled front-end assets

Shipped in `src/farmcore/core/static/vendor/` with their upstream licence files; versions,
sources and SHA-256 hashes are in [the vendor inventory](src/farmcore/core/static/vendor/README.md).

| Asset | Licence |
|---|---|
| htmx 2.0.4 | 0BSD |
| MapLibre GL JS 4.7.1 | BSD-3-Clause (with its bundled notices) |
| Chart.js 4.4.1 | MIT |
| Tailwind CSS 3.4.17 (generated stylesheet) | MIT |
| DM Sans | SIL Open Font License 1.1 |

## Model weights

faster-whisper `small` (CTranslate2 conversion of OpenAI Whisper small, MIT) is downloaded from
the Hugging Face Hub on first use. It is not bundled.

## Container images

`python:3.12-slim` (PSF-2.0; Debian packages under their own licences) for the app and bot;
`postgis/postgis:17-3.5` (PostgreSQL License; PostGIS GPL-2.0-or-later, running as a separate
database server, not linked into FarmCore).

## Bundled data

| File | Source | Licence | Notes |
|---|---|---|---|
| `sample-data/meteofrance/demo_station_33394002_2024_2025.csv` and `src/farmcore/apps/kpi/seasons/meteofrance_33394002_2020_2023_marsep.csv` | **Météo-France**, *Données climatologiques de base – horaires*, station 33394002 (St-Émilion), via data.gouv.fr | Licence Ouverte / Etalab 2.0 | Attribution: Météo-France. Hourly rows, columns renamed and wind converted to km/h; nothing filled or interpolated ([extract notes](src/farmcore/apps/kpi/seasons/README.md)) |
| `sample-data/ign/demo_parcels_named.geojson` | **IGN**, Registre Parcellaire Graphique (RPG) 2024, via the Géoplateforme WFS | Licence Ouverte / Etalab 2.0 | Attribution: IGN. Ten real vine-parcel shapes near Saint-Émilion. Parcel names, varieties and planting years are **fictional**; no ownership is implied |
| `sample-data/open-meteo/ecmwf_ifs/*.json` | **Open-Meteo** Single Runs API, ECMWF IFS model | CC BY 4.0 | Attribution: Open-Meteo.com; ECMWF. Raw API responses for April–May 2024 |

## Figures and identifiers in the code

| Source | Licence | Use |
|---|---|---|
| **Agreste**, *Pratiques culturales en viticulture 2019* (Chiffres et Données 2023-4) | French public statistics | One figure: 20.1 fungicide treatments per season in the Bordelais (Season & impact reference) |
| Chambres d'agriculture + Fédération Cuma Bourgogne; Arrêté du 26 avril 2017 | Cited figures | 8.7 L/ha diesel per sprayer pass; 3.17 kg CO₂e per litre of GNR |
| **BSV Vigne Nord Aquitaine** 2020–2025 (DRAAF Nouvelle-Aquitaine) | Public plant-health bulletins | Dates only (first symptoms, risk periods) to check the advisor; no bulletin text is reused |
| **E-Phy** (ANSES) | Licence Ouverte | Names, authorisation (AMM) numbers and vine-use limits of the three demo products |
| Agronomic thresholds | Cited per value | Each advisor threshold names its publication in `apps/advisor/engine/config.py`; unsourced values are labelled "assumed" |
| EPPO codes, BBCH scale | Reference codes | Identifiers in the register |

## External services (not bundled)

- **Map tiles.** IGN orthophotos (Géoplateforme WMTS, `data.geopf.fr`, credited "© IGN") and
  OpenStreetMap raster tiles (`tile.openstreetmap.org`, © OpenStreetMap contributors, ODbL).
  OSM's public tile servers are for light use under the
  [OSMF tile usage policy](https://operations.osmfoundation.org/policies/tiles/); a busy
  installation should use its own or a commercial tile provider. Each tile request discloses the
  visitor's address and map area to the tile host.
- **Optional datasets you load yourself.** The `demo_es` app reads SIGPAC 2024 (FEGA/MAPA,
  CC BY 4.0; Junta de Castilla y León regional catalogue, IGCYL-NC) and InfoRiego (ITACyL)
  station files from `DATA_DIR`. They are not bundled; check their terms before using or
  publishing them.
- **Optional services.** Vertex AI (Google Cloud) and Telegram, under your own accounts and
  their terms.

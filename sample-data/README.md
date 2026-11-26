# Sample data

Open data for the demo farm: a fictional Bordeaux estate on ten real vine parcels near
Saint-Émilion, with the real Météo-France station 33394002 and archived forecasts, replaying the
2024 season. `farmcore seed --scene B` reads these files from `DATA_DIR` (default: this folder).

| Path | Source | Licence |
|---|---|---|
| `ign/demo_parcels_named.geojson` | IGN, Registre Parcellaire Graphique 2024 (vine parcels, Saint-Émilion) | Licence Ouverte / Etalab 2.0, © IGN |
| `meteofrance/demo_station_33394002_2024_2025.csv` | Météo-France, hourly climatology, station 33394002 ST EMILION, March–October of 2024 and 2025 | Licence Ouverte / Etalab 2.0, © Météo-France |
| `open-meteo/ecmwf_ifs/run-*.json` | Open-Meteo Single Runs API, ECMWF IFS, runs 7 Apr – 16 May 2024 at the station's coordinates; each file keeps its request URL and raw response | CC BY 4.0, Open-Meteo.com and ECMWF |
| `soilgrids/demo_topsoil_ph.json` | ISRIC – World Soil Information, [SoilGrids 2.0](https://docs.isric.org/globaldata/soilgrids/) (2021), queried through SoilHive on 25 Nov 2026 | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |

Parcel names, varieties and planting years are **fictional**; the IGN geometry and parcel
identifiers are real, and no ownership is implied. Station rows are unchanged except for the
renamed columns and wind in km/h; missing values stay missing.

The SoilGrids snapshot holds normalized minimum and maximum predictions for pH in water at
0–5 cm depth, keyed by IGN `id_parcel`. It uses the mean prediction layer (`phh2o_mean`) at
250 m resolution, with pH values converted from the source scale of pH × 10. The data came
from the [SoilHive geometry API](https://developer.soilhive.ag/reference/post_soil-data-by-geometry.md)
using dataset 19 and property 1. Each range spans raster values returned for the parcel polygon.
For P4 and P8, SoilHive rejected the polygon, so the snapshot uses a representative interior
point. The fixture gives the 2024 replay regional context from a global model. ISRIC advises
against using SoilGrids at farm scale. [SoilGrids 2.0 publication](https://doi.org/10.5194/soil-7-217-2021) ·
[ISRIC guidance](https://docs.isric.org/globaldata/soilgrids/SoilGrids_faqs_04.html)

To use your own data, point `DATA_DIR` at a folder with the same layout. Optional datasets for
the Spanish archive app (`sigpac/`, `inforiego/`) are not included: download them from ITACyL
and check their terms first.

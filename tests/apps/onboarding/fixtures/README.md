# Parcel importer fixtures

All coordinates and attributes are synthetic, created for FarmCore; Apache-2.0.
No farmer export, personal identifier or external dataset is included.

`parcel.geojson` is a WGS84 rectangle from (2, 48) to (2.001, 48.001).
Its independent ellipsoidal area is **0.8297537684 ha** (WGS84 geodesic polygon,
PROJ Geod). The importer uses EPSG:6933 equal-area projection; tolerance ±0.5%.
`parcel.gpkg` and the `parcel.{shp,shx,dbf,prj,cpg}` files were generated from
that GeoJSON with GeoPandas/pyogrio. The shapefile uses EPSG:2154 (Lambert-93).
ZIP and KMZ containers are assembled in memory in the tests; no archives are committed.

`parcel.kml`, `parcel.csv` (WKT), `vertices.csv` (ordered lon/lat boundary vertices
grouped by name), and `TASKDATA.XML` describe the same rectangle. ISOXML uses
PFD/PLN(type 1)/LSG(exterior type 1)/PNT(C latitude, D longitude).
Inline boundaries only: external ISOXML files/binary points are not resolved.

No redistributable real telepac sample was obtainable. The permitted fallback is
implemented as a shapefile ZIP, based on the official telepac description:
[Telepac 2015 presentation, import/export pp. 44–45](https://www3.telepac.agriculture.gouv.fr/telepac/pdf/tas/2015/Dossier-PAC-2015_TelePAC_presentation.pdf)
and the [Ministry's 2023 transfer procedure](https://info.agriculture.gouv.fr/boagri/instruction-2023-423/telechargement).
These describe ESRI shapefiles and Lambert-93 for metropolitan France, not a stable
XML schema. Telepac XML is explicitly refused with a shapefile-export instruction;
real farmer telepac compatibility remains to be qualified. Attribute names vary:
use the expert name/crop column override rather than guessing an annual schema.

Round 2: runtime readers use pyogrio.raw plus Shapely/pyproj. The fixture binaries
remain unchanged from their original generation. ZIP imports ignore `.shp.xml`
metadata, prefer a uniquely named `parcell*` layer, and expose an archive-layer
field for other multi-layer archives. CSV accepts UTF-8 or cp1252 and comma,
semicolon or tab delimiters. Uploaded attributes cannot supply review notices.

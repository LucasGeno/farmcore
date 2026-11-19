"""Bounded local files only; XML never reaches GDAL and ZIP paths never reach extractall."""

import csv
import io
import json
import math
import stat
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

import pyogrio
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring
from pyogrio.raw import read
from pyproj import Transformer
from shapely import from_wkb, from_wkt, make_valid, transform
from shapely.errors import GEOSException
from shapely.geometry import MultiPolygon, Polygon, mapping, shape

# Assumed product limits, not agronomic thresholds. Reject whole uploads, never truncate.
MAX_BYTES = 5_000_000
MAX_EXPANDED = 20_000_000
MAX_XML_BYTES = 5_000_000
MAX_MEMBERS = 100
MAX_PARCELS = 200
MAX_VERTICES = 10000
MIN_AREA_HA, MAX_AREA_HA = 0.0001, 100000
AREA_TRANSFORM = Transformer.from_crs(4326, 6933, always_xy=True)


def normalise(geometry, properties, index):
    messages = list(properties.get("messages", []))
    if geometry.geom_type not in {"Polygon", "MultiPolygon"} or geometry.is_empty:
        raise ValueError("Parcel boundaries must be non-empty polygons, not points or lines.")
    if geometry.has_z:
        raise ValueError("Use two-dimensional boundaries.")
    bounds = geometry.bounds
    if not all(math.isfinite(v) for v in bounds) or not (
        -180 <= bounds[0] <= bounds[2] <= 180 and -90 < bounds[1] <= bounds[3] < 90
    ):
        raise ValueError("Coordinates are outside WGS84; check the source CRS.")
    if not geometry.is_valid:
        geometry = make_valid(geometry)
        if geometry.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError("Invalid boundary could not be repaired as a polygon.")
        messages.append("Invalid boundary repaired; check it before confirming.")
    area = transform(geometry, AREA_TRANSFORM.transform, interleaved=False).area / 10000
    if not math.isfinite(area) or not MIN_AREA_HA <= area <= MAX_AREA_HA:
        raise ValueError("Parcel area is outside the supported range (0.0001–100000 ha).")
    return {
        "name": str(properties.get("name") or f"Parcel {index}")[:200],
        "crop_code": str(properties.get("crop_code") or "")[:200],
        "source_ref": str(properties.get("source_ref") or "")[:200],
        "source_scope": str(properties.get("source_scope") or "")[:255],
        "geometry": json.loads(json.dumps(mapping(geometry))),
        "area_ha": area,
        "messages": list(dict.fromkeys(messages)),
    }


def unpack(data, root, layer=""):
    with ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS or sum(m.file_size for m in members) > MAX_EXPANDED:
            raise ValueError("Archive exceeds the expanded size or file count limit.")
        names = set()
        for member in members:
            path = PurePosixPath(member.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in member.filename
                or ":" in member.filename
                or stat.S_ISLNK(member.external_attr >> 16)
                or member.filename.casefold() in names
            ):
                raise ValueError("Unsafe or duplicate archive path.")
            names.add(member.filename.casefold())
            if member.is_dir():
                continue
            if member.compress_size and member.file_size / member.compress_size > 200:
                raise ValueError("Archive compression ratio exceeds the safety limit.")
            if path.suffix.lower() in {".xml", ".kml"} and member.file_size > MAX_XML_BYTES:
                raise ValueError("XML exceeds the 5 MB expanded size limit.")
            destination = root.joinpath(*path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source:
                content = source.read(MAX_EXPANDED + 1)
            if len(content) != member.file_size or len(content) > MAX_EXPANDED:
                raise ValueError("Archive expanded size is invalid.")
            destination.write_bytes(content)
    shapefiles = sorted(p for p in root.rglob("*") if p.suffix.lower() == ".shp")
    candidates = shapefiles or sorted(
        p
        for p in root.rglob("*")
        if p.suffix.lower() == ".kml" or p.name.casefold() == "taskdata.xml"
    )
    if layer:
        candidates = [p for p in candidates if p.relative_to(root).as_posix() == layer]
    elif len(shapefiles) > 1:
        parcels = [p for p in shapefiles if "parcell" in p.stem.casefold()]
        if len(parcels) == 1:
            candidates = parcels
    if len(candidates) != 1:
        choices = ", ".join(p.relative_to(root).as_posix() for p in shapefiles)
        raise ValueError(
            "Choose an archive layer in Attribute mapping: "
            + (choices or "TASKDATA.XML or one KML required.")
        )
    return candidates[0]


def xml_features(data):
    if len(data) > MAX_XML_BYTES:
        raise ValueError("XML exceeds the 5 MB expanded size limit.")
    root = fromstring(data, forbid_dtd=True)
    # Namespace removal is local to the parsed, hardened tree.
    for element in root.iter():
        element.tag = element.tag.rsplit("}", 1)[-1]
    if root.tag == "kml":
        out = []
        for placemark in root.iter("Placemark"):
            polygons = []
            for polygon in placemark.iter("Polygon"):
                rings = []
                for ring in polygon.iter("LinearRing"):
                    coords = ring.findtext("coordinates", "")
                    rings.append(
                        [tuple(float(v) for v in point.split(",")[:2]) for point in coords.split()]
                    )
                polygons.append(Polygon(rings[0], rings[1:]))
            if not polygons:
                raise ValueError("KML placemarks must contain polygon boundaries.")
            attrs = {"name": placemark.findtext("name", "")}
            for element in placemark.iter("Data"):
                attrs[element.get("name")] = element.findtext("value", "")
            out.append((MultiPolygon(polygons), attrs))
        return out
    if root.tag == "ISO11783_TaskData":
        out = []
        for partfield in root.iter("PFD"):
            polygons = []
            for polygon in partfield.findall("PLN"):
                if polygon.get("A") != "1":  # Partfield boundary, not treatment zone.
                    continue
                outer, holes = [], []
                for line in polygon.findall("LSG"):
                    coords = [
                        (float(p.attrib["D"]), float(p.attrib["C"])) for p in line.findall("PNT")
                    ]
                    if line.get("A") == "1":
                        outer.append(coords)
                    elif line.get("A") == "2":
                        holes.append(coords)
                if len(outer) != 1:
                    raise ValueError("ISOXML boundary needs one exterior ring.")
                polygons.append(Polygon(outer[0], holes))
            if not polygons:
                raise ValueError("ISOXML partfield has no inline polygon boundary.")
            out.append(
                (
                    MultiPolygon(polygons),
                    {"name": partfield.get("C", ""), "source_ref": partfield.get("A", "")},
                )
            )
        return out
    raise ValueError("Unsupported XML. For telepac, export the parcel shapefile ZIP with its .prj.")


def csv_features(data, name_field):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise ValueError("Cannot decode CSV; save as UTF-8.") from exc
    # Header sniffing avoids commas inside a quoted WKT changing the delimiter.
    header = text.splitlines()[0] if text else ""
    delimiter = (
        csv.Sniffer().sniff(header, delimiters=",;\t").delimiter
        if any(c in header for c in ",;\t")
        else ","
    )
    rows = list(csv.DictReader(io.StringIO(text), delimiter=delimiter))
    if not rows:
        raise ValueError("CSV is empty.")
    if "wkt" in rows[0]:
        return [(from_wkt(row["wkt"]), row) for row in rows]
    groups = {}
    for row in rows:
        name = row.get(name_field or "name", "")
        if not name:
            raise ValueError("CSV vertices need a name for each boundary.")
        groups.setdefault(name, []).append((float(row["lon"]), float(row["lat"])))
    return [(Polygon(coords), {name_field or "name": name}) for name, coords in groups.items()]


def read_features(path, name_field):
    suffix = path.suffix.lower()
    if suffix in {".xml", ".kml"}:
        return xml_features(path.read_bytes())
    if suffix == ".csv":
        return csv_features(path.read_bytes(), name_field)
    if suffix in {".geojson", ".json"}:
        document = json.loads(path.read_bytes())
        if document.get("type") != "FeatureCollection" or "crs" in document:
            raise ValueError("GeoJSON must be a WGS84 FeatureCollection without a CRS override.")
        return [(shape(f["geometry"]), f.get("properties") or {}) for f in document["features"]]
    if suffix not in {".shp", ".gpkg"}:
        raise ValueError("Unsupported file format.")
    if suffix == ".shp" and not any(
        p.suffix.lower() == ".prj" and p.stem == path.stem for p in path.parent.iterdir()
    ):
        raise ValueError("Shapefile CRS is missing: include the .prj file.")
    # Extension alone is not a driver restriction: GDAL can auto-detect VRTs and
    # follow their external sources. Refuse non-native signatures before GDAL sees them.
    header = path.read_bytes()[:100]
    if suffix == ".shp":
        if header[:4] != bytes.fromhex("0000270a"):
            raise ValueError("Invalid shapefile signature.")
    elif header[:16] != b"SQLite format 3\x00" or header[68:72] != b"GPKG":
        raise ValueError("Invalid GeoPackage signature.")
    layers = pyogrio.list_layers(path)
    if len(layers) != 1:
        raise ValueError("Choose a file containing one parcel layer.")
    metadata, _, geometries, columns = read(path, max_features=MAX_PARCELS + 1)
    if metadata["crs"] is None:
        raise ValueError("The file has no CRS.")
    reproject = Transformer.from_crs(metadata["crs"], 4326, always_xy=True)
    features = []
    for i, wkb in enumerate(geometries):
        attrs = {
            name: column[i]
            for name, column in zip(metadata["fields"], columns, strict=True)
            if column[i] is not None
        }
        geometry = transform(from_wkb(wkb), reproject.transform, interleaved=False)
        features.append((geometry, attrs))
    return features


def parse(data, filename, *, name_field="", crop_field="", layer=""):
    try:
        if len(data) > MAX_BYTES:
            raise ValueError("File exceeds the 5 MB limit.")
        with TemporaryDirectory(prefix="farmcore-parcels-") as directory:
            root = Path(directory)
            suffix = Path(filename or "").suffix.lower()
            if suffix in {".zip", ".kmz"}:
                path = unpack(data, root, layer)
            else:
                path = root / ("upload" + suffix)
                path.write_bytes(data)
            features = read_features(path, name_field)
        if not 1 <= len(features) <= MAX_PARCELS:
            raise ValueError("Choose between 1 and 200 parcel boundaries.")
        out = []
        for index, (geometry, attrs) in enumerate(features, 1):
            from shapely import get_num_coordinates

            if get_num_coordinates(geometry) > MAX_VERTICES:
                raise ValueError("A boundary exceeds the 10000 vertex limit.")
            for override, target in ((name_field, "name"), (crop_field, "crop_code")):
                if override:
                    if override not in attrs:
                        raise ValueError(f"Column {override!r} is missing.")
                    attrs[target] = attrs[override]
            attrs.pop("messages", None)  # Only the server can author review notices.
            attrs["source_scope"] = Path(filename or "").name
            out.append(normalise(geometry, attrs, index))
        return out
    except (
        BadZipFile,
        csv.Error,
        OSError,
        RuntimeError,
        NotImplementedError,
        DefusedXmlException,
        ParseError,
        GEOSException,
        KeyError,
        IndexError,
        TypeError,
        AttributeError,
        RecursionError,
        UnicodeError,
        pyogrio.errors.DataSourceError,
        pyogrio.errors.DataLayerError,
    ) as exc:
        raise ValueError(
            "Cannot read this parcel file safely; check its format, geometry and CRS."
        ) from exc

"""Read the selected RECFE records from the saved SIGPAC archive, without a GIS dependency.

Only this source's DBF + PolygonZ shapefile format is supported. Rings go to PostGIS BuildArea
so holes and disconnected parts retain their topology; Z/M values are deliberately discarded.
"""

import struct
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from zipfile import ZipFile

FILE = "sigpac/47180_Valbuena_de_Duero_2024.zip"
MAX_MEMBER_BYTES = 64_000_000
SELECTED = (
    (250, 1),
    (245, 1),
    (227, 1),
    (229, 1),
    (267, 1),
    (261, 1),
    (225, 1),
    (230, 4),
    (242, 1),
    (231, 1),
)
PARTS = ("PROVINCIA", "MUNICIPIO", "AGREGADO", "ZONA", "POLIGONO", "PARCELA", "RECINTO")


@dataclass(frozen=True)
class Plot:
    reference: str
    original: str
    area_ha: Decimal
    rings_wkt: str


def _records(data: bytes):
    if len(data) < 32:
        raise ValueError("Truncated DBF header")
    count = struct.unpack_from("<I", data, 4)[0]
    header, width = struct.unpack_from("<HH", data, 8)
    if header < 32 or width <= 0:
        raise ValueError("Invalid DBF header")
    if header + count * width > len(data):
        raise ValueError("Truncated DBF records")
    fields = []
    offset = 32
    while True:
        if offset >= len(data):
            raise ValueError("Truncated DBF field descriptor")
        if data[offset] == 13:
            if offset >= header:
                raise ValueError("DBF field descriptors overrun the declared header")
            break
        if offset + 32 > header:
            raise ValueError("DBF field descriptors overrun the declared header")
        descriptor = data[offset : offset + 32]
        fields.append((descriptor[:11].split(b"\0")[0].decode(), descriptor[16]))
        offset += 32
    for i in range(count):
        raw = data[header + i * width : header + (i + 1) * width]
        if raw[:1] != b" ":
            continue
        record, offset = {}, 1
        for name, length in fields:
            record[name] = raw[offset : offset + length].decode("utf-8").strip()
            offset += length
        yield i, record


def _shapes(data: bytes):
    offset, index = 100, 0
    while offset < len(data):
        if offset + 8 > len(data):
            raise ValueError("Truncated shapefile record header")
        _, words = struct.unpack_from(">ii", data, offset)
        offset += 8
        if words <= 0:
            raise ValueError("Invalid shapefile record length")
        end = offset + words * 2
        if end > len(data):
            raise ValueError("Truncated shapefile record")
        shape = data[offset:end]
        offset = end
        yield index, shape
        index += 1


def _rings(shape: bytes) -> str:
    if len(shape) < 44:
        raise ValueError("Truncated shapefile record")
    if struct.unpack_from("<i", shape)[0] != 15:
        raise ValueError("Expected SIGPAC RECFE PolygonZ geometry")
    parts, points = struct.unpack_from("<ii", shape, 36)
    if parts < 0 or points < 0 or 44 + 4 * parts + 16 * points > len(shape):
        raise ValueError("Truncated polygon coordinates")
    if parts == 0:
        if points:
            raise ValueError("Invalid polygon: points without a ring")
        return "MULTILINESTRING EMPTY"
    part_starts = struct.unpack_from(f"<{parts}i", shape, 44)
    if (
        part_starts[0] != 0
        or any(a >= b for a, b in zip(part_starts, part_starts[1:], strict=False))
        or part_starts[-1] >= points
    ):
        raise ValueError("Invalid polygon ring start indices")
    starts = (*part_starts, points)
    coords = [struct.unpack_from("<dd", shape, 44 + 4 * parts + i * 16) for i in range(points)]
    rings = [
        "(" + ",".join(f"{x} {y}" for x, y in coords[a:b]) + ")"
        for a, b in zip(starts[:-1], starts[1:], strict=True)
    ]
    return "MULTILINESTRING(" + ",".join(rings) + ")"


def read(path: Path) -> list[Plot]:
    with ZipFile(path) as archive:

        def member(suffix):
            info = next((i for i in archive.infolist() if i.filename.endswith(suffix)), None)
            if info is None:
                raise ValueError(f"SIGPAC archive is missing the expected {suffix} member")
            if info.file_size > MAX_MEMBER_BYTES:
                raise ValueError(f"SIGPAC archive member {suffix} is unexpectedly large")
            return archive.read(info)

        attrs = dict(_records(member("47180_RECFE.dbf")))
        shapes = dict(_shapes(member("47180_RECFE.shp")))
    found = {}
    for index, row in attrs.items():
        reference = ":".join(row[k] for k in PARTS)
        if reference not in {f"47:180:0:0:9:{p}:{r}" for p, r in SELECTED}:
            continue
        if row["USO_SIGPAC"] != "VI":
            raise ValueError(f"Selected SIGPAC recinto is not vineyard use VI: {reference}")
        found[reference] = Plot(
            reference, row["C_REFREC"], Decimal(row["SUPERFICIE"]) / 10000, _rings(shapes[index])
        )
    return [found[f"47:180:0:0:9:{p}:{r}"] for p, r in SELECTED]

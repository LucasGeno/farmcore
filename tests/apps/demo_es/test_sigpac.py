import struct
from zipfile import ZipFile

import pytest

from farmcore.apps.demo_es import archive, sigpac


def dbf(header, count, width, body):
    """A DBF file: 32-byte header + field descriptors/terminator + records."""
    return b"\x03" + b"\0" * 3 + struct.pack("<IHH", count, header, width) + b"\0" * 20 + body


def field_descriptor(name, length):
    return name.ljust(11, b"\0") + b"C" + b"\0" * 4 + bytes([length]) + b"\0" * 15


def test_valid_empty_dbf_is_accepted():
    data = dbf(33, 0, 1, b"\x0d")
    assert list(sigpac._records(data)) == []


def test_minimal_dbf_record_is_parsed():
    descriptor = field_descriptor(b"NAME", 5)
    record = b" " + b"AB   "
    data = dbf(32 + 32 + 1, 1, 6, descriptor + b"\x0d" + record)
    assert list(sigpac._records(data)) == [(0, {"NAME": "AB"})]


def test_dbf_missing_terminator_is_refused():
    data = dbf(40, 0, 1, field_descriptor(b"NAME", 5) + b"\x0d")
    with pytest.raises(ValueError, match="overrun the declared header"):
        list(sigpac._records(data))


def test_dbf_truncated_file_is_refused():
    data = dbf(65, 0, 1, b"\x00" * 10)
    with pytest.raises(ValueError, match="Truncated DBF records"):
        list(sigpac._records(data))


def test_dbf_non_terminator_body_is_refused():
    data = dbf(33, 0, 1, b"\x00")
    with pytest.raises(ValueError, match="overrun the declared header"):
        list(sigpac._records(data))


def test_empty_summary_reports_zero_coverage_without_crashing():
    result = archive.summary([])
    assert result["rows"] == 0
    assert result["duplicates"] == 0
    assert result["first"] == "" and result["last"] == ""
    assert result["missing_slots"] == 0 and result["gaps"] == []
    assert result["coverage_pct"] == 0.0
    assert result["time_basis"] == "unverified"
    assert result["notice"] == archive.NOTICE


def test_shapefile_truncated_record_header_is_refused():
    with pytest.raises(ValueError, match="Truncated shapefile record header"):
        list(sigpac._shapes(b"\0" * 104))


def test_shapefile_negative_record_length_is_refused():
    data = b"\0" * 100 + struct.pack(">ii", 1, -4)
    with pytest.raises(ValueError, match="record length"):
        list(sigpac._shapes(data))


def test_shapefile_record_past_end_of_file_is_refused():
    data = b"\0" * 100 + struct.pack(">ii", 1, 10)
    with pytest.raises(ValueError, match="Truncated shapefile record"):
        list(sigpac._shapes(data))


def test_rings_rejects_point_count_past_record_end():
    shape = struct.pack("<i", 15) + b"\0" * 32 + struct.pack("<ii", 1, 1000)
    with pytest.raises(ValueError, match="Truncated polygon coordinates"):
        sigpac._rings(shape)


def polygon(parts, points, starts, coords):
    head = struct.pack("<i", 15) + b"\0" * 32 + struct.pack("<ii", parts, points)
    return (
        head
        + struct.pack(f"<{parts}i", *starts)
        + b"".join(struct.pack("<dd", x, y) for x, y in coords)
    )


def test_rings_splits_valid_rings_at_start_indices():
    shape = polygon(2, 6, (0, 3), [(0, 0), (1, 0), (1, 1), (2, 2), (3, 2), (3, 3)])
    assert (
        sigpac._rings(shape)
        == "MULTILINESTRING((0.0 0.0,1.0 0.0,1.0 1.0),(2.0 2.0,3.0 2.0,3.0 3.0))"
    )


def test_rings_rejects_first_start_not_zero():
    shape = polygon(1, 3, (1,), [(0, 0), (1, 0), (1, 1)])
    with pytest.raises(ValueError, match="ring start indices"):
        sigpac._rings(shape)


def test_rings_rejects_non_increasing_starts():
    shape = polygon(2, 4, (0, 0), [(0, 0), (1, 0), (1, 1), (2, 1)])
    with pytest.raises(ValueError, match="ring start indices"):
        sigpac._rings(shape)


def test_rings_rejects_start_past_point_count():
    shape = polygon(2, 3, (0, 3), [(0, 0), (1, 0), (1, 1)])
    with pytest.raises(ValueError, match="ring start indices"):
        sigpac._rings(shape)


def test_rings_allows_an_empty_polygon():
    assert sigpac._rings(polygon(0, 0, (), [])) == "MULTILINESTRING EMPTY"


def test_rings_rejects_points_without_a_ring():
    shape = polygon(0, 2, (), [(0, 0), (1, 1)])
    with pytest.raises(ValueError, match="points without a ring"):
        sigpac._rings(shape)


def test_archive_member_size_cap_is_enforced(tmp_path, monkeypatch):
    path = tmp_path / "archive.zip"
    with ZipFile(path, "w") as archive_file:
        archive_file.writestr("47180_RECFE.dbf", "x" * 20)
    monkeypatch.setattr(sigpac, "MAX_MEMBER_BYTES", 10)
    with pytest.raises(ValueError, match="unexpectedly large"):
        sigpac.read(path)


def test_archive_missing_member_is_reported(tmp_path):
    path = tmp_path / "archive.zip"
    with ZipFile(path, "w") as archive_file:
        archive_file.writestr("readme.txt", "not a SIGPAC archive")
    with pytest.raises(ValueError, match="missing the expected"):
        sigpac.read(path)

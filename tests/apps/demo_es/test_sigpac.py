import struct
from zipfile import ZipFile

import pytest

from farmcore.apps.demo_es import archive, sigpac


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


def test_archive_missing_member_is_reported(tmp_path):
    path = tmp_path / "archive.zip"
    with ZipFile(path, "w") as archive_file:
        archive_file.writestr("readme.txt", "not a SIGPAC archive")
    with pytest.raises(ValueError, match="missing the expected"):
        sigpac.read(path)

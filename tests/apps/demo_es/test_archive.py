from datetime import datetime

import pytest

from farmcore.apps.demo_es import archive


def row(day, time):
    return {"Código": "VA07", "Fecha (AAAA-MM-DD)": day, "Hora (HHMM)": time}


def test_2400_rolls_source_date_without_inventing_a_timezone():
    value = archive.source_time(row("2024-10-31", "2400"))
    assert value == datetime(2024, 11, 1)
    assert value.tzinfo is None
    # A nonexistent Europe/Madrid civil time remains an uninterpreted source label.
    assert archive.source_time(row("2024-03-31", "0230")) == datetime(2024, 3, 31, 2, 30)


def test_gap_and_duplicate_counts_do_not_turn_missing_records_into_readings():
    result = archive.summary([row("2024-03-01", t) for t in ("0030", "0030", "0130", "2400")])
    assert result["rows"] == 4 and result["duplicates"] == 1
    assert result["missing_slots"] == 45 and result["coverage_pct"] == 6.25
    assert result["time_basis"] == "unverified"


def test_combined_station_file_is_refused_instead_of_silently_mixing_stations(tmp_path):
    path = tmp_path / "mixed.csv"
    path.write_text(
        "Código;Fecha (AAAA-MM-DD);Hora (HHMM)\nVA07;2024-03-01;0030\nVA103;2024-03-01;0030\n"
    )
    with pytest.raises(ValueError, match="single-station"):
        archive.read(path)

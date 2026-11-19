"""EU active-substance source parsing, at the pure seam: the normalised key, status/date mapping,
exact-duplicate collapse, and one row per malformed class."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from farmcore.apps.catalog_eu_pesticides import sources
from farmcore.apps.catalog_eu_pesticides.substances import substance_key

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("Buprofezin", "buprofezin"),
        ("BUPROFEZIN", "buprofezin"),
        ("  buprofezin  ", "buprofezin"),
        ("Thiophanate-methyl", "thiophanate-methyl"),
        ("2,4-D", "2,4-d"),
    ],
)
def test_substance_key_normalises_across_sources(given, expected):
    assert substance_key(given) == expected


def test_parse_collapses_duplicates_and_maps_statuses_and_dates():
    parsed = sources.parse((FIXTURES / "active_substances.ndjson").read_bytes())

    assert parsed.skipped == 0
    by_key = {row.key: row for row in parsed.rows}
    assert len(parsed.rows) == 5  # the source's repeated rows collapse onto one substance
    assert by_key["buprofezin"].status == "approved"
    assert by_key["buprofezin"].cas_number == "69327-76-0"
    assert by_key["glyphosate"].approval_date.isoformat() == "2023-12-16"
    assert by_key["glyphosate"].expiry_date.isoformat() == "2033-12-15"
    assert by_key["warfarin (aka coumaphene)"].status == "not_approved"
    pending = by_key["aluminium potassium sulphate dodecahydrate"]
    assert pending.status == "pending"
    assert (pending.approval_date, pending.expiry_date) == (None, None)


def _line(record: dict) -> bytes:
    return (json.dumps(record) + "\n").encode()


MALFORMED = [
    pytest.param(
        _line({"substance_name": "X", "substance_status": "Suspended"}),
        "unknown status",
        id="unknown-status",
    ),
    pytest.param(
        _line(
            {
                "substance_name": "X",
                "substance_status": "Approved",
                "approval_date": "31/13/2020",
            }
        ),
        "malformed date",
        id="bad-date",
    ),
    pytest.param(b"{not json\n", "malformed JSON", id="bad-json"),
    pytest.param(_line({"substance_status": "Approved"}), "missing substance name", id="no-name"),
]


@pytest.mark.parametrize("payload,reason", MALFORMED)
def test_malformed_row_is_reported_and_skipped(payload, reason):
    result = sources.parse(payload)

    assert result.skipped == 1
    assert len(result.rows) == 0
    assert reason in result.reasons[0]

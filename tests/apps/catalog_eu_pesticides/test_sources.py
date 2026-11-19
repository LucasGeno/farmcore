"""EU active-substance source parsing, at the pure seam: the alias lookup, status/date mapping,
exact-duplicate collapse (with the latest expiry), and one row per malformed class."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from farmcore.apps.catalog_eu_pesticides import sources
from farmcore.apps.catalog_eu_pesticides.substances import aliases_for, build_alias_map
from farmcore.apps.catalog_it_salute import sources as it_sources

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "Boscalid (formerly nicobifen)",
            {"boscalid (formerly nicobifen)", "boscalid", "nicobifen"},
        ),
        ("SULPHUR (ZOLFO)", {"sulphur (zolfo)", "sulphur"}),
        # A `(CAS …)` group is part of the identity, not a qualifier to strip.
        ("Paraffin oil (CAS 8042-47-5)", {"paraffin oil (cas 8042-47-5)"}),
    ],
)
def test_aliases_for_include_exact_stripped_and_aka(name, expected):
    assert aliases_for(name) == expected


def test_ambiguous_aliases_are_dropped():
    alias_map = build_alias_map(
        [
            "Sulphur",
            "Sodium hydrogen carbonate (basic substance)",
            "Sodium hydrogen carbonate (low risk active substance)",
        ],
        ["SULPHUR (ZOLFO)", "Sodium hydrogen carbonate (other qualifier)"],
    )

    assert alias_map["sulphur (zolfo)"] == "sulphur"
    assert "sodium hydrogen carbonate (other qualifier)" not in alias_map
    assert "sodium hydrogen carbonate" not in alias_map
    assert "sodium hydrogen carbonate (basic substance)" in alias_map


def test_parse_collapses_duplicates_and_maps_statuses_and_dates():
    parsed = sources.parse((FIXTURES / "active_substances.ndjson").read_bytes())

    assert parsed.skipped == 0
    by_key = {row.key: row for row in parsed.rows}
    assert len(parsed.rows) == 6  # the source's repeated rows collapse onto one substance
    assert by_key["buprofezin"].status == "approved"
    assert by_key["glyphosate"].approval_date.isoformat() == "2023-12-16"
    assert by_key["glyphosate"].expiry_date.isoformat() == "2033-12-15"
    assert by_key["warfarin (aka coumaphene)"].status == "not_approved"
    pending = by_key["aluminium potassium sulphate dodecahydrate"]
    assert pending.status == "pending"
    assert (pending.approval_date, pending.expiry_date) == (None, None)


def test_conflicting_expiry_keeps_the_latest_deterministically():
    lines = (
        '{"substance_name":"X","substance_status":"Approved","expiry_date":"31/12/2021"}\n'
        '{"substance_name":"X","substance_status":"Approved","expiry_date":"31/03/2027"}\n'
    )

    for ordered in (lines, "\n".join(reversed(lines.splitlines()))):
        parsed = sources.parse(ordered.encode())
        assert len(parsed.rows) == 1
        assert parsed.rows[0].expiry_date.isoformat() == "2027-03-31"


@pytest.mark.parametrize(
    "parser,payload",
    [
        (sources, b"<html>not data</html>"),
        (sources, b""),
        (sources, b"[]"),
        (it_sources, b"<html>not data</html>"),
        (it_sources, b"[]"),
        (it_sources, b'{"error":"unavailable"}'),
    ],
)
def test_non_data_body_is_a_source_error(parser, payload):
    with pytest.raises(parser.SourceError):
        parser.parse(payload)


def _line(record: dict) -> bytes:
    return (json.dumps(record) + "\n").encode()


_VALID = _line({"substance_name": "Glyphosate", "substance_status": "Approved"})

MALFORMED = [
    pytest.param(
        _VALID + _line({"substance_name": "X", "substance_status": "Suspended"}),
        "unknown status",
        id="unknown-status",
    ),
    pytest.param(
        _VALID
        + _line(
            {
                "substance_name": "X",
                "substance_status": "Approved",
                "approval_date": "31/13/2020",
            }
        ),
        "malformed date",
        id="bad-date",
    ),
    pytest.param(_VALID + b"{not json\n", "malformed JSON", id="bad-json"),
    pytest.param(
        _VALID + _line({"substance_status": "Approved"}), "missing substance name", id="no-name"
    ),
]


@pytest.mark.parametrize("payload,reason", MALFORMED)
def test_malformed_row_is_reported_and_skipped(payload, reason):
    result = sources.parse(payload)

    assert result.skipped == 1
    assert len(result.rows) == 1  # the valid row is kept, the malformed one skipped
    assert reason in result.reasons[0]

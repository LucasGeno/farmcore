"""Italian product source parsing, at the pure seam: state normalisation, substance splitting,
and one row per malformed class."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from farmcore.apps.catalog_it_salute import sources

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_maps_products_states_and_substances():
    parsed = sources.parse((FIXTURES / "products.json").read_bytes())

    assert parsed.skipped == 0
    by_number = {row.number: row for row in parsed.rows}
    assert len(parsed.rows) == 6
    assert by_number["015230"].state == "authorised"
    assert by_number["015230"].expiry.isoformat() == "2026-12-15"
    assert by_number["015230"].substances == (("buprofezin", "BUPROFEZIN"),)
    assert by_number["000017"].substances == (("mcpa", "MCPA"), ("2,4-d", "2,4-D"))
    assert by_number["012498"].state == "provisional"
    for withdrawn in ("000001", "004662", "010128"):
        assert by_number[withdrawn].state == "withdrawn"


def _record(**changes: str) -> bytes:
    record = {
        "num_registrazione": "000001",
        "denominazione_prodotto": "X",
        "stato_amministrativo": "Autorizzato",
        "data_scadenza_autorizzazione": "-",
        "sostanze_attive": "MCPA",
    }
    record.update(changes)
    return json.dumps([record]).encode()


MALFORMED = [
    pytest.param(
        _record(stato_amministrativo="Sconosciuto"),
        "unknown administrative state",
        id="unknown-state",
    ),
    pytest.param(
        _record(data_scadenza_autorizzazione="31/13/2020"),
        "malformed date",
        id="bad-date",
    ),
    pytest.param(_record(num_registrazione=""), "missing or invalid", id="no-number"),
]


@pytest.mark.parametrize("payload,reason", MALFORMED)
def test_malformed_row_is_reported_and_skipped(payload, reason):
    result = sources.parse(payload)

    assert result.skipped == 1
    assert len(result.rows) == 0
    assert reason in result.reasons[0]


def test_not_json_is_a_source_error():
    with pytest.raises(sources.SourceError):
        sources.parse(b"not json")

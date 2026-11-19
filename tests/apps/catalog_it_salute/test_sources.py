"""Italian product source parsing, at the pure seam: state normalisation, substance splitting and
exact substance keys, and one row per malformed class."""

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
    assert len(parsed.rows) == 7
    assert by_number["015230"].state == "authorised"
    assert by_number["015230"].expiry.isoformat() == "2026-12-15"
    assert by_number["015230"].substances == (("buprofezin", "BUPROFEZIN"),)
    assert by_number["000017"].substances == (("mcpa", "MCPA"), ("2,4-d", "2,4-D"))
    # Qualified names keep their exact key; the alias table resolves them.
    assert by_number["012862"].substances == (
        ("boscalid (formerly nicobifen)", "BOSCALID (FORMERLY NICOBIFEN)"),
    )
    assert by_number["012498"].state == "provisional"
    for withdrawn in ("000001", "004662", "010128"):
        assert by_number[withdrawn].state == "withdrawn"


def test_dash_is_empty_and_extra_fields_are_mapped():
    record = {
        "num_registrazione": "000001",
        "denominazione_prodotto": "X",
        "stato_amministrativo": "Revocato",
        "data_scadenza_autorizzazione": "-",
        "sostanze_attive": "-",
        "indicazioni_di_pericolo": "-",
        "attivita": "DISERBANTE",
        "contenuto_per_100g_di_prodotto": "50.0 g",
        "data_decorrenza_revoca": "07/04/1990",
    }

    parsed = sources.parse(json.dumps([record]).encode())

    row = parsed.rows[0]
    assert row.substances == ()  # `-` is the source's null, not a substance
    assert row.props["hazard"] == ""
    assert row.props["concentration"] == "50.0 g"
    assert row.props["revocation_effective_date"] == "1990-04-07"


_VALID_RECORD = {
    "num_registrazione": "000001",
    "denominazione_prodotto": "X",
    "stato_amministrativo": "Autorizzato",
    "data_scadenza_autorizzazione": "-",
    "sostanze_attive": "MCPA",
}


def _record(**changes: str) -> dict:
    record = dict(_VALID_RECORD)
    record.update(changes)
    return record


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


@pytest.mark.parametrize("bad,reason", MALFORMED)
def test_malformed_row_is_reported_and_skipped(bad, reason):
    result = sources.parse(json.dumps([_VALID_RECORD, bad]).encode())

    assert result.skipped == 1
    assert len(result.rows) == 1  # the valid record is kept, the malformed one skipped
    assert reason in result.reasons[0]


def test_not_json_is_a_source_error():
    with pytest.raises(sources.SourceError):
        sources.parse(b"not json")

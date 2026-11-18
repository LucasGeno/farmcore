"""E-Phy source parsing, at the pure seam: exact archive file names, both encodings, and one row
per malformed class (blank AMM, wrong column count, non-numeric dose, unknown state)."""

from __future__ import annotations

import csv
import io
from pathlib import Path

import pytest

from farmcore.apps.catalog_fr_ephy import sources

FIXTURES = Path(__file__).parent / "fixtures"


def _load(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader)
        return header, list(reader)


def _bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


PRODUCT_HEADER, PRODUCT_ROWS = _load(FIXTURES / "products_utf8.csv")
USE_HEADER, USE_ROWS = _load(FIXTURES / "uses_utf8.csv")


def _mutate(header: list[str], row: list[str], column: str, value: str) -> list[str]:
    cells = list(row)
    cells[header.index(column)] = value
    return cells


def test_find_files_matches_exact_archive_names(tmp_path):
    # The UTF-8 archive also ships `produits_classe_et_mention_danger_utf8.csv`, which sorts
    # before `produits_utf8.csv`; a glob over `produits_*.csv` would pick it.
    (tmp_path / "produits_classe_et_mention_danger_utf8.csv").write_text("x", encoding="utf-8")
    (tmp_path / "produits_utf8.csv").write_text("a", encoding="utf-8")
    (tmp_path / "produits_usages_utf8.csv").write_text("b", encoding="utf-8")

    products, uses = sources.find_files(tmp_path)

    assert products.name == "produits_utf8.csv"
    assert uses.name == "produits_usages_utf8.csv"


def test_find_files_matches_windows_1252_names(tmp_path):
    (tmp_path / "produits_Windows-1252.csv").write_text("a", encoding="utf-8")
    (tmp_path / "produits_usages_Windows-1252.csv").write_text("b", encoding="utf-8")

    products, uses = sources.find_files(tmp_path)

    assert products.name == "produits_Windows-1252.csv"
    assert uses.name == "produits_usages_Windows-1252.csv"


def test_windows_1252_parses_the_same_rows_as_utf8():
    utf8_products = sources.parse_products((FIXTURES / "products_utf8.csv").read_bytes())
    cp1252_products = sources.parse_products((FIXTURES / "products_cp1252.csv").read_bytes())
    assert utf8_products.skipped == cp1252_products.skipped == 0
    assert {(r.amm, r.name, r.withdrawn) for r in utf8_products.rows} == {
        (r.amm, r.name, r.withdrawn) for r in cp1252_products.rows
    }

    utf8_uses = sources.parse_uses((FIXTURES / "uses_utf8.csv").read_bytes())
    cp1252_uses = sources.parse_uses((FIXTURES / "uses_cp1252.csv").read_bytes())
    assert utf8_uses.skipped == cp1252_uses.skipped == 0
    assert {(u.amm, u.identifiant) for u in utf8_uses.rows} == {
        (u.amm, u.identifiant) for u in cp1252_uses.rows
    }


MALFORMED = [
    (
        sources.parse_products,
        _bytes(PRODUCT_HEADER, [_mutate(PRODUCT_HEADER, PRODUCT_ROWS[0], "numero AMM", "")]),
        "missing or invalid AMM",
    ),
    (
        sources.parse_products,
        _bytes(PRODUCT_HEADER, [PRODUCT_ROWS[0][:-1]]),
        "columns",
    ),
    (
        sources.parse_products,
        _bytes(
            PRODUCT_HEADER,
            [_mutate(PRODUCT_HEADER, PRODUCT_ROWS[0], "Etat d’autorisation", "SUSPENDU")],
        ),
        "unknown authorisation state",
    ),
    (
        sources.parse_uses,
        _bytes(USE_HEADER, [_mutate(USE_HEADER, USE_ROWS[0], "dose retenue", "1,5")]),
        "malformed decimal",
    ),
    (
        sources.parse_uses,
        _bytes(USE_HEADER, [_mutate(USE_HEADER, USE_ROWS[0], "etat usage", "SUSPENDU")]),
        "unknown use state",
    ),
]


@pytest.mark.parametrize("parse,payload,reason", MALFORMED)
def test_malformed_row_is_reported_and_skipped(parse, payload, reason):
    result = parse(payload)

    assert result.skipped == 1
    assert len(result.rows) == 0
    assert reason in result.reasons[0]

"""E-Phy import: the recorded fixture imports idempotently, withdrawn products stay flagged, both
encodings are read, and a malformed row is skipped whole."""

from __future__ import annotations

import csv

from sqlalchemy import func, select

from farmcore.apps.catalog_fr_ephy import service
from farmcore.apps.catalog_fr_ephy.models import CatalogImport, CatalogUse
from farmcore.core.models.catalog import CatalogAuthorisation, CatalogItem

WITHDRAWN_AMM = "8800006"  # DIMATE BF 400
AUTHORISED_AMM = "2180347"  # APPLAUD STAR


def _item(session, amm: str) -> CatalogItem | None:
    return session.scalar(
        select(CatalogItem)
        .join(CatalogAuthorisation, CatalogAuthorisation.catalog_item_id == CatalogItem.id)
        .where(CatalogAuthorisation.country == "FR", CatalogAuthorisation.number == amm)
    )


def _use_count(session, item: CatalogItem) -> int:
    return session.scalar(
        select(func.count()).select_from(CatalogUse).where(CatalogUse.catalog_item_id == item.id)
    )


def test_import_maps_products_authorisations_and_uses(session, ephy):
    report = service.import_ephy(session, products_path=ephy["products"], uses_path=ephy["uses"])

    assert report.changed
    assert report.products == 200
    assert report.uses == 1477
    assert report.skipped == 0
    assert session.scalar(select(func.count()).select_from(CatalogItem)) == 200
    assert (
        session.scalar(
            select(func.count())
            .select_from(CatalogAuthorisation)
            .where(CatalogAuthorisation.country == "FR")
        )
        == 200
    )

    withdrawn = _item(session, WITHDRAWN_AMM)
    assert withdrawn is not None
    assert withdrawn.name == "DIMATE BF 400"
    assert withdrawn.props["withdrawn"] is True
    assert _use_count(session, withdrawn) == 18

    authorised = _item(session, AUTHORISED_AMM)
    assert authorised is not None
    assert authorised.props["withdrawn"] is False
    assert authorised.props["substances"] == [
        {"name": "buprofézine", "ref": "Buprofezin", "concentration": "250.0", "unit": "g/L"}
    ]
    assert _use_count(session, authorised) == 3


def test_unchanged_second_run_writes_nothing(session, ephy):
    service.import_ephy(session, products_path=ephy["products"], uses_path=ephy["uses"])
    items = session.scalar(select(func.count()).select_from(CatalogItem))

    again = service.import_ephy(session, products_path=ephy["products"], uses_path=ephy["uses"])

    assert again.changed is False
    assert again.products == 0
    assert session.scalar(select(func.count()).select_from(CatalogItem)) == items
    assert session.scalar(select(func.count()).select_from(CatalogImport)) == 1


def test_withdrawn_product_flagged_and_kept_across_updates(session, ephy, tmp_path):
    service.import_ephy(session, products_path=ephy["products"], uses_path=ephy["uses"])

    changed = tmp_path / "products.csv"
    changed.write_bytes(ephy["products"].read_bytes().replace(b"HELIXER", b"HELIXER RENAMED"))
    report = service.import_ephy(session, products_path=changed, uses_path=ephy["uses"])

    assert report.changed
    withdrawn = _item(session, WITHDRAWN_AMM)
    assert withdrawn is not None
    assert withdrawn.props["withdrawn"] is True
    assert _use_count(session, withdrawn) == 18
    renamed = session.scalar(select(CatalogItem).where(CatalogItem.name == "HELIXER RENAMED"))
    assert renamed is not None


def test_windows_1252_encoding_imports_the_same_catalog(session, ephy):
    report = service.import_ephy(
        session, products_path=ephy["products_cp1252"], uses_path=ephy["uses_cp1252"]
    )

    assert report.changed
    assert report.products == 200
    assert report.uses == 1477
    authorised = _item(session, AUTHORISED_AMM)
    assert authorised is not None
    assert authorised.props["substances"][0]["name"] == "buprofézine"


def test_malformed_row_is_reported_and_skipped(session, ephy, tmp_path):
    products = ephy["products"].read_text(encoding="utf-8").splitlines(keepends=True)
    header = products[0]
    rows = [next(csv.reader([line], delimiter=";")) for line in products[1:]]
    rows[0][1] = ""  # blank the AMM of the first product: the row is skipped whole
    malformed = tmp_path / "products.csv"
    with malformed.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter=";", lineterminator="\n")
        writer.writerow(next(csv.reader([header], delimiter=";")))
        writer.writerows(rows)

    report = service.import_ephy(session, products_path=malformed, uses_path=ephy["uses"])

    assert report.changed
    assert report.skipped >= 1
    assert report.products == 199
    assert _item(session, AUTHORISED_AMM) is None  # the blanked row, never imported half-way

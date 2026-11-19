"""Italian product import on real Postgres: products map to neutral catalog items keyed on the IT
registration number, with per-country authorisations and substance links to the EU register; an
unchanged re-run writes nothing and a changed product reconciles its links."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select

from farmcore.apps.catalog_eu_pesticides.models import CatalogItemSubstance
from farmcore.apps.catalog_fr_ephy.models import CatalogImport
from farmcore.apps.catalog_it_salute import service
from farmcore.core.models.catalog import CatalogAuthorisation, CatalogItem

FIXTURES = Path(__file__).parent / "fixtures"


def _item(session, number: str) -> CatalogItem | None:
    return session.scalar(
        select(CatalogItem)
        .join(CatalogAuthorisation, CatalogAuthorisation.catalog_item_id == CatalogItem.id)
        .where(CatalogAuthorisation.country == "IT", CatalogAuthorisation.number == number)
    )


def _links(session, item: CatalogItem) -> list[tuple[str, str]]:
    return session.execute(
        select(CatalogItemSubstance.substance_key, CatalogItemSubstance.name)
        .where(CatalogItemSubstance.catalog_item_id == item.id)
        .order_by(CatalogItemSubstance.substance_key)
    ).all()


def test_import_maps_products_authorisations_and_substance_links(session, salute):
    report = service.import_salute(session, path=salute)

    assert report.changed
    assert report.skipped == 0
    assert report.products == 6
    assert session.scalar(select(func.count()).select_from(CatalogItem)) == 6
    assert (
        session.scalar(
            select(func.count())
            .select_from(CatalogAuthorisation)
            .where(CatalogAuthorisation.country == "IT")
        )
        == 6
    )

    appl = _item(session, "015230")
    assert appl is not None
    assert appl.name == "APPLAUD 25 WP"
    assert appl.props["withdrawn"] is False
    assert appl.props["state"] == "authorised"
    assert appl.props["expiry"] == "2026-12-15"
    assert appl.props["holder"] == "NICHINO EUROPE CO LTD"
    assert _links(session, appl) == [("buprofezin", "BUPROFEZIN")]

    mix = _item(session, "000017")
    assert _links(session, mix) == [("2,4-d", "2,4-D"), ("mcpa", "MCPA")]

    withdrawn = _item(session, "000001")
    assert withdrawn.props["withdrawn"] is True
    assert withdrawn.props["state"] == "withdrawn"

    imported = session.scalar(select(CatalogImport).where(CatalogImport.source == "it-salute"))
    assert imported is not None
    assert imported.licence_id == "IODL-2.0"
    assert imported.attribution == "Ministero della Salute — Prodotti fitosanitari"


def test_unchanged_rerun_writes_nothing(session, salute):
    service.import_salute(session, path=salute)
    items = session.scalar(select(func.count()).select_from(CatalogItem))

    again = service.import_salute(session, path=salute)

    assert again.changed is False
    assert session.scalar(select(func.count()).select_from(CatalogItem)) == items
    assert session.scalar(select(func.count()).select_from(CatalogImport)) == 1


def test_changed_product_reconciles_substance_links(session, salute, tmp_path):
    service.import_salute(session, path=salute)
    appl = _item(session, "015230")
    assert _links(session, appl) == [("buprofezin", "BUPROFEZIN")]

    records = json.loads(salute.read_text(encoding="utf-8"))
    for record in records:
        if record["num_registrazione"] == "015230":
            record["sostanze_attive"] = "BUPROFEZIN|SULPHUR"
    changed = tmp_path / "products.json"
    changed.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")

    report = service.import_salute(session, path=changed)

    assert report.changed
    assert report.products == 1  # only APPLAUD moved
    appl = _item(session, "015230")
    assert _links(session, appl) == [("buprofezin", "BUPROFEZIN"), ("sulphur", "SULPHUR")]

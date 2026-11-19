"""Italian product import on real Postgres: products map to neutral catalog items keyed on the IT
registration number, with per-country authorisations and substance links that join to the EU
register through the alias table; an unchanged re-run writes nothing and a changed product
reconciles its links."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from farmcore.apps.catalog_eu_pesticides import service as eu_service
from farmcore.apps.catalog_eu_pesticides.models import (
    CatalogItemSubstance,
    CatalogSubstance,
    CatalogSubstanceAlias,
)
from farmcore.apps.catalog_it_salute import service
from farmcore.core.models.catalog import CatalogAuthorisation, CatalogImport, CatalogItem

FIXTURES = Path(__file__).parent / "fixtures"
EU_FIXTURE = (
    Path(__file__).parents[2]
    / "apps"
    / "catalog_eu_pesticides"
    / "fixtures"
    / "active_substances.ndjson"
)


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
    assert report.products == 7
    assert session.scalar(select(func.count()).select_from(CatalogItem)) == 7
    assert (
        session.scalar(
            select(func.count())
            .select_from(CatalogAuthorisation)
            .where(CatalogAuthorisation.country == "IT")
        )
        == 7
    )

    appl = _item(session, "015230")
    assert appl is not None
    assert appl.name == "APPLAUD 25 WP"
    assert appl.props["withdrawn"] is False
    assert _links(session, appl) == [("buprofezin", "BUPROFEZIN")]

    mix = _item(session, "000017")
    assert _links(session, mix) == [("2,4-d", "2,4-D"), ("mcpa", "MCPA")]

    withdrawn = _item(session, "000001")
    assert withdrawn.props["withdrawn"] is True

    imported = session.scalar(select(CatalogImport).where(CatalogImport.source == "it-salute"))
    assert imported.licence_id == "IODL-2.0"
    assert imported.attribution == "Ministero della Salute — Prodotti fitosanitari"


def test_products_join_to_eu_substances_via_alias(session, salute):
    eu_service.import_substances(session, path=EU_FIXTURE)
    service.import_salute(session, path=salute)

    resolved = session.execute(
        select(CatalogItem.name, CatalogSubstance.name)
        .select_from(CatalogItemSubstance)
        .join(
            CatalogSubstanceAlias,
            CatalogSubstanceAlias.alias_key == CatalogItemSubstance.substance_key,
        )
        .join(
            CatalogSubstance, CatalogSubstance.substance_key == CatalogSubstanceAlias.substance_key
        )
        .join(CatalogItem, CatalogItem.id == CatalogItemSubstance.catalog_item_id)
        .where(CatalogItem.name == "CANTUS")
    ).all()

    assert resolved == [("CANTUS", "Boscalid (formerly nicobifen)")]
    assert _links(session, _item(session, "012862")) == [
        ("boscalid (formerly nicobifen)", "BOSCALID (FORMERLY NICOBIFEN)")
    ]


def test_unchanged_rerun_writes_nothing_and_changed_rerun_reconciles_links(
    session, salute, tmp_path
):
    service.import_salute(session, path=salute)
    items = session.scalar(select(func.count()).select_from(CatalogItem))
    appl = _item(session, "015230")
    assert _links(session, appl) == [("buprofezin", "BUPROFEZIN")]

    again = service.import_salute(session, path=salute)
    assert again.changed is False
    assert session.scalar(select(func.count()).select_from(CatalogItem)) == items
    assert session.scalar(select(func.count()).select_from(CatalogImport)) == 1

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


def test_missing_product_is_flagged_and_cleared_on_return(session, salute, tmp_path):
    service.import_salute(session, path=salute)
    records = json.loads(salute.read_text())
    path = tmp_path / "changed.json"
    path.write_text(json.dumps([r for r in records if r["num_registrazione"] != "015230"]))
    report = service.import_salute(
        session, path=path, retrieved_at=datetime(2026, 11, 19, tzinfo=UTC)
    )
    item = _item(session, "015230")
    assert item.props["missing_upstream"] == "2026-11-19"
    assert item.props["state"] == "authorised"
    assert "1 products missing upstream" in report.reasons
    receipt = session.scalar(
        select(CatalogImport).where(CatalogImport.content_hash == report.content_hash)
    )
    assert "1 products missing upstream" in receipt.reasons
    path.write_text(json.dumps(records, indent=1))
    service.import_salute(session, path=path)
    assert "missing_upstream" not in item.props

"""E-Phy import on real Postgres: products/authorisations/uses map to the catalog, an unchanged
re-run writes nothing, a changed re-run touches only what changed and keeps withdrawn products
linked to history, and a malformed product's uses are never imported."""

from __future__ import annotations

import csv
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from farmcore.apps.catalog_fr_ephy import service
from farmcore.apps.catalog_fr_ephy.models import CatalogImport, CatalogUse
from farmcore.core.models.canonical import Farm, Log
from farmcore.core.models.catalog import CatalogAuthorisation, CatalogItem, enter_quantity

WITHDRAWN_AMM = "8800006"  # DIMATE BF 400
AUTHORISED_AMM = "2180347"  # APPLAUD STAR (the fixture's first product)
COMPOSITE_AMM = "2190686/2161101"  # REVYSTAR XL + PRIAXOR EC


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


def _blank_first_amm(src: Path, dst: Path) -> None:
    with src.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader)
        rows = list(reader)
    rows[0][header.index("numero AMM")] = ""
    with dst.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter=";", lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def _mutate_use(src: Path, dst: Path, amm: str, identifiant: str, **changes: str) -> None:
    with src.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader)
        rows = list(reader)
    amm_at = header.index("numero AMM")
    ident_at = header.index("identifiant usage")
    for row in rows:
        if row[amm_at].strip() == amm and row[ident_at].strip() == identifiant:
            for column, value in changes.items():
                row[header.index(column)] = value
            break
    with dst.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter=";", lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def test_import_maps_products_authorisations_and_uses(session, ephy):
    report = service.import_ephy(session, products_path=ephy["products"], uses_path=ephy["uses"])

    assert report.changed
    assert report.skipped == 0
    assert report.products == 202
    assert report.uses == 1460  # distinct uses (exact source duplicates collapse)
    assert session.scalar(select(func.count()).select_from(CatalogItem)) == 202
    assert session.scalar(select(func.count()).select_from(CatalogUse)) == 1460
    assert (
        session.scalar(
            select(func.count())
            .select_from(CatalogAuthorisation)
            .where(CatalogAuthorisation.country == "FR")
        )
        == 202
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

    composite = _item(session, COMPOSITE_AMM)
    assert composite is not None
    assert composite.name == "REVYSTAR XL + PRIAXOR EC"

    provisional = session.scalar(select(CatalogUse).where(CatalogUse.state == "provisional"))
    assert provisional is not None


def test_unchanged_rerun_writes_nothing_and_changed_rerun_keeps_withdrawn(session, ephy, tmp_path):
    service.import_ephy(session, products_path=ephy["products"], uses_path=ephy["uses"])
    withdrawn = _item(session, WITHDRAWN_AMM)
    assert withdrawn is not None
    item_id = withdrawn.id
    items = session.scalar(select(func.count()).select_from(CatalogItem))
    uses = session.scalar(select(func.count()).select_from(CatalogUse))

    # A historical record links the withdrawn item: AC2 "stay linked to historical records".
    farm = Farm(name="test")
    session.add(farm)
    session.flush()
    log = Log(
        farm_id=farm.id,
        type="spray",
        status="done",
        name="Historical spray",
        ts_start=datetime(2024, 4, 20, 8, 0, tzinfo=UTC),
    )
    session.add(log)
    session.flush()
    quantity = enter_quantity(
        session, log_id=log.id, value=Decimal("2"), unit="l/ha", catalog_item_id=withdrawn.id
    )
    session.flush()

    again = service.import_ephy(session, products_path=ephy["products"], uses_path=ephy["uses"])
    assert again.changed is False
    assert session.scalar(select(func.count()).select_from(CatalogItem)) == items
    assert session.scalar(select(func.count()).select_from(CatalogUse)) == uses
    assert session.scalar(select(func.count()).select_from(CatalogImport)) == 1

    changed = tmp_path / "products.csv"
    changed.write_bytes(ephy["products"].read_bytes().replace(b"HELIXER", b"HELIXER RENAMED"))
    report = service.import_ephy(session, products_path=changed, uses_path=ephy["uses"])

    assert report.changed
    assert report.products == 1  # only HELIXER moved
    assert session.get(CatalogItem, item_id) is not None  # the withdrawn item keeps its id
    assert session.get(CatalogItem, item_id).props["withdrawn"] is True
    assert quantity.catalog_item_id == item_id  # the historical record still links
    assert _item(session, "2180253").name == "HELIXER RENAMED"


def test_skipped_product_uses_are_not_imported(session, ephy, tmp_path):
    products = tmp_path / "products.csv"
    _blank_first_amm(ephy["products"], products)

    report = service.import_ephy(session, products_path=products, uses_path=ephy["uses"])

    assert report.changed
    assert report.skipped == 4  # the blanked product + its 3 uses
    assert report.products == 201
    assert report.uses == 1457  # the blanked product's 3 uses were not written
    assert _item(session, AUTHORISED_AMM) is None


def test_changed_use_replaces_old_row(session, ephy, tmp_path):
    service.import_ephy(session, products_path=ephy["products"], uses_path=ephy["uses"])
    item = _item(session, AUTHORISED_AMM)
    assert _use_count(session, item) == 3

    uses = tmp_path / "uses.csv"
    _mutate_use(
        ephy["uses"],
        uses,
        AUTHORISED_AMM,
        "Arbres et arbustes*Trt Part.Aer.*Aleurodes",
        **{"dose retenue": "2.0", "etat usage": "Retrait"},
    )
    report = service.import_ephy(session, products_path=ephy["products"], uses_path=uses)

    assert report.changed
    assert report.products == 1  # only APPLAUD STAR moved
    item = _item(session, AUTHORISED_AMM)
    rows = session.scalars(select(CatalogUse).where(CatalogUse.catalog_item_id == item.id)).all()
    assert len(rows) == 3  # replaced, not duplicated
    matching = [r for r in rows if r.identifiant == "Arbres et arbustes*Trt Part.Aer.*Aleurodes"]
    assert len(matching) == 1
    assert matching[0].max_dose == Decimal("2.0")
    assert matching[0].state == "withdrawn"


def test_use_upsert_chunks_across_batch_boundary(session, ephy, monkeypatch):
    monkeypatch.setattr(service, "USE_BATCH", 100)

    report = service.import_ephy(session, products_path=ephy["products"], uses_path=ephy["uses"])

    assert report.changed
    assert report.products == 202
    assert report.uses == 1460
    assert session.scalar(select(func.count()).select_from(CatalogUse)) == 1460

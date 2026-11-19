"""EU active-substance import on real Postgres: substances map to the catalog with approval data
and provenance, an unchanged re-run writes nothing, and a changed status updates in place."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from farmcore.apps.catalog_eu_pesticides import service
from farmcore.apps.catalog_eu_pesticides.models import CatalogSubstance
from farmcore.apps.catalog_fr_ephy.models import CatalogImport

FIXTURES = Path(__file__).parent / "fixtures"


def _substance(session, key: str) -> CatalogSubstance | None:
    return session.scalar(select(CatalogSubstance).where(CatalogSubstance.substance_key == key))


def test_import_maps_substances_and_records_provenance(session, eu):
    report = service.import_substances(
        session, path=eu, retrieved_at=datetime(2026, 11, 19, 8, 0, tzinfo=UTC)
    )

    assert report.changed
    assert report.skipped == 0
    assert report.substances == 5
    assert session.scalar(select(func.count()).select_from(CatalogSubstance)) == 5

    glyphosate = _substance(session, "glyphosate")
    assert glyphosate is not None
    assert glyphosate.name == "Glyphosate"
    assert glyphosate.status == "approved"
    assert glyphosate.cas_number == "1071-83-6"
    assert glyphosate.approval_date.isoformat() == "2023-12-16"
    assert glyphosate.expiry_date.isoformat() == "2033-12-15"
    assert glyphosate.props["category"] == "HB - Herbicide"

    imported = session.scalar(select(CatalogImport).where(CatalogImport.source == "eu-pesticides"))
    assert imported is not None
    assert imported.licence_id == "CC-BY-4.0"
    assert imported.attribution == "European Commission, EU Pesticides Database (DG SANTE)"
    assert imported.product_count == 5
    assert imported.use_count == 0
    assert imported.skipped == 0


def test_unchanged_rerun_writes_nothing(session, eu):
    service.import_substances(session, path=eu)
    count = session.scalar(select(func.count()).select_from(CatalogSubstance))

    again = service.import_substances(session, path=eu)

    assert again.changed is False
    assert session.scalar(select(func.count()).select_from(CatalogSubstance)) == count
    assert session.scalar(select(func.count()).select_from(CatalogImport)) == 1


def test_changed_status_updates_in_place(session, eu, tmp_path):
    service.import_substances(session, path=eu)
    original = _substance(session, "glyphosate")
    assert original.status == "approved"

    changed = tmp_path / "changed.ndjson"
    lines = eu.read_text(encoding="utf-8").splitlines()
    updated = []
    for line in lines:
        if '"substance_name":"Glyphosate"' in line:
            line = line.replace(
                '"substance_status":"Approved"', '"substance_status":"Not approved"'
            )
        updated.append(line)
    changed.write_text("\n".join(updated) + "\n", encoding="utf-8")

    report = service.import_substances(session, path=changed)

    assert report.changed
    assert report.substances == 5
    session.expire_all()  # the upsert is Core SQL, so the earlier ORM object is stale
    assert session.scalar(select(func.count()).select_from(CatalogSubstance)) == 5
    glyphosate = _substance(session, "glyphosate")
    assert glyphosate.id == original.id  # upserted in place, not replaced
    assert glyphosate.status == "not_approved"

"""EU active-substance import on real Postgres: substances and aliases map to the catalog with
provenance, an unchanged re-run writes nothing and a changed status updates in place, and a failed
import leaves existing rows."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from farmcore.apps.catalog_eu_pesticides import service, sources
from farmcore.apps.catalog_eu_pesticides.models import CatalogSubstance, CatalogSubstanceAlias
from farmcore.core.models.catalog import CatalogImport

FIXTURES = Path(__file__).parent / "fixtures"


def _substance(session, key: str) -> CatalogSubstance | None:
    return session.scalar(select(CatalogSubstance).where(CatalogSubstance.substance_key == key))


def test_import_maps_substances_and_records_provenance(session, eu):
    report = service.import_substances(
        session, path=eu, retrieved_at=datetime(2026, 11, 19, 8, 0, tzinfo=UTC)
    )

    assert report.changed
    assert report.skipped == 0
    assert report.substances == 6
    assert session.scalar(select(func.count()).select_from(CatalogSubstance)) == 6

    imported = session.scalar(select(CatalogImport).where(CatalogImport.source == "eu-pesticides"))
    assert imported.licence_id == "CC-BY-4.0"
    assert imported.attribution == "European Commission, EU Pesticides Database (DG SANTE)"
    assert imported.product_count == 6
    assert imported.use_count == 0


def test_import_writes_aliases(session, eu):
    service.import_substances(session, path=eu)

    alias_map = {
        a.alias_key: a.substance_key for a in session.scalars(select(CatalogSubstanceAlias))
    }
    assert alias_map["boscalid"] == "boscalid (formerly nicobifen)"
    assert alias_map["nicobifen"] == "boscalid (formerly nicobifen)"
    assert alias_map["sulphur"] == "sulphur"


def test_unchanged_rerun_writes_nothing_and_changed_rerun_updates_in_place(session, eu, tmp_path):
    service.import_substances(session, path=eu)
    count = session.scalar(select(func.count()).select_from(CatalogSubstance))
    original = _substance(session, "glyphosate")
    assert original.status == "approved"

    again = service.import_substances(session, path=eu)
    assert again.changed is False
    assert session.scalar(select(func.count()).select_from(CatalogSubstance)) == count
    assert session.scalar(select(func.count()).select_from(CatalogImport)) == 1

    changed = tmp_path / "changed.ndjson"
    lines = eu.read_text(encoding="utf-8").splitlines()
    updated = [
        line.replace('"substance_status":"Approved"', '"substance_status":"Not approved"')
        if '"substance_name":"Glyphosate"' in line
        else line
        for line in lines
    ]
    changed.write_text("\n".join(updated) + "\n", encoding="utf-8")

    report = service.import_substances(session, path=changed)

    assert report.changed
    assert report.substances == 6
    session.expire_all()  # the upsert is Core SQL, so the earlier ORM object is stale
    assert session.scalar(select(func.count()).select_from(CatalogSubstance)) == 6
    glyphosate = _substance(session, "glyphosate")
    assert glyphosate.id == original.id  # upserted in place, not replaced
    assert glyphosate.status == "not_approved"


def test_failed_import_leaves_existing_rows(session, eu, tmp_path, monkeypatch):
    service.import_substances(session, path=eu)
    count = session.scalar(select(func.count()).select_from(CatalogSubstance))

    empty = tmp_path / "empty.ndjson"
    empty.write_bytes(b"")
    with pytest.raises(sources.SourceError):
        service.import_substances(session, path=empty)

    assert session.scalar(select(func.count()).select_from(CatalogSubstance)) == count

    def unavailable(url, **kwargs):
        return httpx.Response(404, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", unavailable)
    with pytest.raises(httpx.HTTPStatusError):
        path, meta = sources.download(tmp_path)
        service.import_substances(session, path=path, meta=meta)
    assert session.scalar(select(func.count()).select_from(CatalogSubstance)) == count
    assert session.scalar(select(func.count()).select_from(CatalogImport)) == 1


def test_missing_substance_is_flagged_without_changing_approval(session, eu, tmp_path):
    service.import_substances(session, path=eu)
    path = tmp_path / "changed.ndjson"
    path.write_text(
        "\n".join(
            line
            for line in eu.read_text().splitlines()
            if '"substance_name":"Glyphosate"' not in line
        )
    )
    report = service.import_substances(
        session, path=path, retrieved_at=datetime(2026, 11, 19, tzinfo=UTC)
    )
    item = _substance(session, "glyphosate")
    assert item.props["missing_upstream"] == "2026-11-19"
    assert item.status == "approved"
    assert "1 substances missing upstream" in report.reasons

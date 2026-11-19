"""Idempotent import of the EU active-substance register, and the shared helper that links a
country catalog's products to substances by the normalised key.

An unchanged source (same whole-file content hash) writes nothing. On a change the importer
upserts every substance by key and never deletes one, so products from every country keep
referencing it; it also rebuilds the `catalog_substance_alias` lookup. `upsert_substance_links`
is the one write path `catalog_it_salute` and `catalog_fr_ephy` use to attach a product's
substances, batched so a full import runs in one delete plus a few chunked inserts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from farmcore.apps.catalog_eu_pesticides import sources
from farmcore.apps.catalog_eu_pesticides.models import (
    CatalogItemSubstance,
    CatalogSubstance,
    CatalogSubstanceAlias,
)
from farmcore.apps.catalog_eu_pesticides.substances import build_alias_map
from farmcore.core.models.catalog import CatalogImport

REASON_CAP = 100
# Rows per substance-link insert: 4000 rows x 3 columns stays under PostgreSQL's 65,535 bind limit.
LINK_BATCH = 4000


@dataclass(frozen=True)
class ImportReport:
    changed: bool
    substances: int
    skipped: int
    reasons: tuple[str, ...]
    content_hash: str


def upsert_substance_links(
    session: Session, links: list[tuple[int, list[tuple[str, str]]]]
) -> None:
    """Write each changed product's `catalog_item_substance` links and clear the links of a
    changed product whose substance list is now empty. All deletes and inserts run as one
    statement each (chunked), so a full import stays fast."""
    item_ids = [item_id for item_id, _ in links]
    if item_ids:
        session.execute(
            delete(CatalogItemSubstance).where(CatalogItemSubstance.catalog_item_id.in_(item_ids))
        )
    rows: list[dict[str, object]] = []
    seen: set[tuple[int, str]] = set()
    for item_id, substances in links:
        for key, name in substances:
            if (item_id, key) in seen:
                continue
            seen.add((item_id, key))
            rows.append({"catalog_item_id": item_id, "substance_key": key, "name": name})
    for start in range(0, len(rows), LINK_BATCH):
        session.execute(
            pg_insert(CatalogItemSubstance)
            .values(rows[start : start + LINK_BATCH])
            .on_conflict_do_nothing(index_elements=["catalog_item_id", "substance_key"])
        )

    if links:
        _refresh_aliases(session)


def _refresh_aliases(session: Session) -> None:
    alias_map = build_alias_map(
        list(session.scalars(select(CatalogSubstance.name))),
        list(session.scalars(select(CatalogItemSubstance.name).distinct())),
    )
    session.execute(delete(CatalogSubstanceAlias))
    rows = [{"alias_key": key, "substance_key": value} for key, value in alias_map.items()]
    for start in range(0, len(rows), LINK_BATCH):
        session.execute(pg_insert(CatalogSubstanceAlias).values(rows[start : start + LINK_BATCH]))


def import_substances(
    session: Session,
    *,
    path: Path | str,
    retrieved_at: datetime | None = None,
    meta: sources.SourceMeta | None = None,
) -> ImportReport:
    """Import the EU active-substance file, keyed on the normalised substance key, idempotent on
    the content hash. A substance is upserted in place and never deleted; a substance absent from
    the new file is flagged, never dropped."""
    raw = Path(path).read_bytes()
    fingerprint = sources.content_hash(raw)
    if (
        session.scalar(
            select(CatalogImport.id).where(
                CatalogImport.source == sources.SOURCE,
                CatalogImport.content_hash == fingerprint,
            )
        )
        is not None
    ):
        return ImportReport(
            changed=False, substances=0, skipped=0, reasons=(), content_hash=fingerprint
        )

    at = retrieved_at or sources.now()
    meta = meta or sources.SourceMeta()
    parsed = sources.parse(raw)
    reasons = list(parsed.reasons)

    existing_keys = set(session.scalars(select(CatalogSubstance.substance_key)))
    for row in parsed.rows:
        session.execute(
            pg_insert(CatalogSubstance)
            .values(
                {
                    "substance_key": row.key,
                    "name": row.name,
                    "cas_number": row.cas_number,
                    "status": row.status,
                    "approval_date": row.approval_date,
                    "expiry_date": row.expiry_date,
                    "props": row.props,
                }
            )
            .on_conflict_do_update(
                index_elements=["substance_key"],
                set_={
                    "name": row.name,
                    "cas_number": row.cas_number,
                    "status": row.status,
                    "approval_date": row.approval_date,
                    "expiry_date": row.expiry_date,
                    "props": row.props,
                },
            )
        )

    missing = existing_keys - {row.key for row in parsed.rows}
    if missing:
        for substance in session.scalars(
            select(CatalogSubstance).where(CatalogSubstance.substance_key.in_(missing))
        ):
            substance.props = {**substance.props, "missing_upstream": at.date().isoformat()}
        reasons.append(f"{len(missing)} substances missing upstream")

    _refresh_aliases(session)

    session.add(
        CatalogImport(
            source=sources.SOURCE,
            licence=meta.licence,
            licence_id=meta.licence_id,
            attribution=meta.attribution,
            retrieved_at=at,
            content_hash=fingerprint,
            resource_url=meta.resource_url,
            archive_name=meta.archive_name,
            archive_date=meta.archive_date,
            product_count=len(parsed.rows),
            use_count=0,
            skipped=parsed.skipped,
            reasons=list(reasons[:REASON_CAP]),
        )
    )
    return ImportReport(
        changed=True,
        substances=len(parsed.rows),
        skipped=parsed.skipped,
        reasons=tuple(reasons[:REASON_CAP]),
        content_hash=fingerprint,
    )

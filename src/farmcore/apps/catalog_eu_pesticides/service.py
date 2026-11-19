"""Idempotent import of the EU active-substance register, and the shared helper that links a
country catalog's products to substances by the normalised key.

An unchanged source (same whole-file content hash) writes nothing. On a change the importer
upserts every substance by key and never deletes one, so products from every country keep
referencing it. `upsert_substance_links` is the one write path `catalog_it_salute` and
`catalog_fr_ephy` use to attach a product's substances.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from farmcore.apps.catalog_eu_pesticides import sources
from farmcore.apps.catalog_eu_pesticides.models import CatalogItemSubstance, CatalogSubstance
from farmcore.apps.catalog_fr_ephy.models import CatalogImport

REASON_CAP = 100


@dataclass(frozen=True)
class ImportReport:
    changed: bool
    substances: int
    skipped: int
    reasons: tuple[str, ...]
    content_hash: str


def upsert_substance_links(
    session: Session, item_id: int, substances: list[tuple[str, str]]
) -> None:
    """Write one `catalog_item_substance` link per (key, name), and remove the links of a changed
    product whose substance no longer appears. A product with no substances clears its links."""
    deduped: dict[str, str] = {}
    for key, name in substances:
        deduped.setdefault(key, name)
    keys = set(deduped)
    if keys:
        session.execute(
            delete(CatalogItemSubstance).where(
                CatalogItemSubstance.catalog_item_id == item_id,
                CatalogItemSubstance.substance_key.not_in(keys),
            )
        )
    else:
        session.execute(
            delete(CatalogItemSubstance).where(CatalogItemSubstance.catalog_item_id == item_id)
        )
    if not deduped:
        return
    session.execute(
        pg_insert(CatalogItemSubstance)
        .values(
            [
                {"catalog_item_id": item_id, "substance_key": key, "name": name}
                for key, name in deduped.items()
            ]
        )
        .on_conflict_do_nothing(index_elements=["catalog_item_id", "substance_key"])
    )


def import_substances(
    session: Session,
    *,
    path: Path | str,
    retrieved_at: datetime | None = None,
    meta: sources.SourceMeta | None = None,
) -> ImportReport:
    """Import the EU active-substance file, keyed on the normalised substance key, idempotent on
    the content hash. A substance is upserted in place and never deleted."""
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

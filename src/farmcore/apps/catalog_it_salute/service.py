"""Idempotent import of Italy's product register: neutral catalog items keyed on the IT
registration number, per-country authorisations, and substance links to the EU register.

An unchanged source (same whole-file content hash) writes nothing. A changed source reconciles
only what changed: each product carries a content hash of its own row, so a product whose source
did not move is left alone. Withdrawn products are flagged on the item and never deleted, so
historical records stay linked.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from farmcore.apps.catalog_eu_pesticides.service import upsert_substance_links
from farmcore.apps.catalog_fr_ephy.models import CatalogImport
from farmcore.apps.catalog_it_salute import sources
from farmcore.core.models.catalog import CatalogAuthorisation, CatalogItem

COUNTRY = "IT"
REASON_CAP = 100


@dataclass(frozen=True)
class ImportReport:
    changed: bool
    products: int
    skipped: int
    reasons: tuple[str, ...]
    content_hash: str


def _fingerprint(row: sources.ProductRow) -> str:
    payload = {
        "name": row.name,
        "holder": row.holder,
        "state": row.state,
        "raw_state": row.raw_state,
        "expiry": row.expiry.isoformat() if row.expiry else None,
        "substances": list(row.substances),
        "props": row.props,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _props(row: sources.ProductRow, fingerprint: str) -> dict[str, object]:
    props: dict[str, object] = {
        "source": sources.SOURCE,
        "withdrawn": row.state == "withdrawn",
        "state": row.state,
        "raw_state": row.raw_state,
        "content_hash": fingerprint,
    }
    if row.holder:
        props["holder"] = row.holder
    if row.expiry:
        props["expiry"] = row.expiry.isoformat()
    if row.substances:
        props["substances"] = [name for _, name in row.substances]
    for key, value in row.props.items():
        if value:
            props[key] = value
    return props


def _existing_items(session: Session, numbers: list[str]) -> dict[str, CatalogItem]:
    if not numbers:
        return {}
    rows = session.execute(
        select(CatalogAuthorisation.number, CatalogItem)
        .join(CatalogItem, CatalogItem.id == CatalogAuthorisation.catalog_item_id)
        .where(CatalogAuthorisation.country == COUNTRY, CatalogAuthorisation.number.in_(numbers))
    ).all()
    return {number: item for number, item in rows}


def import_salute(
    session: Session,
    *,
    path: Path | str,
    retrieved_at: datetime | None = None,
    meta: sources.SourceMeta | None = None,
) -> ImportReport:
    """Import the Fitosanitari JSON file, keyed on the IT registration number, idempotent on the
    content hash."""
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
            changed=False, products=0, skipped=0, reasons=(), content_hash=fingerprint
        )

    at = retrieved_at or sources.now()
    meta = meta or sources.SourceMeta()
    parsed = sources.parse(raw)
    reasons = list(parsed.reasons)

    existing = _existing_items(session, [row.number for row in parsed.rows])
    new_items: list[tuple[str, CatalogItem]] = []
    changed: list[tuple[CatalogItem, sources.ProductRow]] = []
    for row in parsed.rows:
        product_hash = _fingerprint(row)
        item = existing.get(row.number)
        if item is not None and item.props.get("content_hash") == product_hash:
            continue
        if item is None:
            item = CatalogItem(name=row.name)
            session.add(item)
            new_items.append((row.number, item))
        else:
            item.name = row.name
        item.props = _props(row, product_hash)
        changed.append((item, row))
    session.flush()  # new items get their ids

    for number, item in new_items:
        session.add(CatalogAuthorisation(catalog_item_id=item.id, country=COUNTRY, number=number))
    session.flush()

    for item, row in changed:
        upsert_substance_links(session, item.id, list(row.substances))

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
        products=len(changed),
        skipped=parsed.skipped,
        reasons=tuple(reasons[:REASON_CAP]),
        content_hash=fingerprint,
    )

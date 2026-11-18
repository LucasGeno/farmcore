"""Idempotent E-Phy import: neutral catalog items keyed on the FR AMM, per-country
authorisations, registered uses and the import fingerprint.

An unchanged source (same whole-file content hash) writes nothing. A changed source reconciles
only what changed: each product carries a content hash of its own row and uses, so a product whose
source did not move is left alone (its id, props and uses are untouched). Uses are keyed on
`(catalog_item_id, use_key)` — a content hash of their mapped fields, since the source repeats an
identifiant per product — and upserted in place, so a weekly re-import never churns use ids or
silently cascades dependants. Withdrawn products are flagged on the item and never deleted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from farmcore.apps.catalog_fr_ephy import sources
from farmcore.apps.catalog_fr_ephy.models import CatalogImport, CatalogUse
from farmcore.core.models.catalog import CatalogAuthorisation, CatalogItem

COUNTRY = "FR"
REASON_CAP = 100
# Rows per use-upsert statement: 4000 rows x ~16 columns stays under PostgreSQL's 65,535 bind
# limit. A full first import (about 80k uses) runs in under a minute; an unchanged re-run is a
# no-op.
USE_BATCH = 4000


@dataclass(frozen=True)
class ImportReport:
    changed: bool
    products: int
    uses: int
    skipped: int
    reasons: tuple[str, ...]
    content_hash: str


def _use_payload(use: sources.UseRow) -> dict[str, object]:
    return {
        "identifiant": use.identifiant,
        "crop": use.crop,
        "target": use.target,
        "max_dose": str(use.max_dose) if use.max_dose is not None else None,
        "max_dose_unit": use.max_dose_unit,
        "interval_days": str(use.interval_days) if use.interval_days is not None else None,
        "max_applications": use.max_applications,
        "phi_days": use.phi_days,
        "distribution_end": use.distribution_end.isoformat() if use.distribution_end else None,
        "use_end": use.use_end.isoformat() if use.use_end else None,
        "znt_aquatic": str(use.znt_aquatic) if use.znt_aquatic is not None else None,
        "znt_arthropods": str(use.znt_arthropods) if use.znt_arthropods is not None else None,
        "znt_plants": str(use.znt_plants) if use.znt_plants is not None else None,
        "state": use.state,
    }


def _use_key(use: sources.UseRow) -> str:
    return hashlib.sha256(
        json.dumps(_use_payload(use), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _product_fingerprint(row: sources.ProductRow, uses: list[sources.UseRow]) -> str:
    payload = {
        "name": row.name,
        "withdrawn": row.withdrawn,
        "holder": row.holder,
        "gamme": row.gamme,
        "fonctions": list(row.fonctions),
        "formulation": row.formulation,
        "substances": [dict(substance) for substance in row.substances],
        "trade_names": list(row.trade_names),
        "withdrawal_date": row.withdrawal_date,
        "uses": [_use_payload(use) for use in uses],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def _props(row: sources.ProductRow, fingerprint: str) -> dict[str, object]:
    props: dict[str, object] = {
        "source": sources.SOURCE,
        "withdrawn": row.withdrawn,
        "content_hash": fingerprint,
    }
    if row.holder:
        props["holder"] = row.holder
    if row.gamme:
        props["gamme"] = row.gamme
    if row.fonctions:
        props["fonctions"] = list(row.fonctions)
    if row.formulation:
        props["formulation"] = row.formulation
    if row.substances:
        props["substances"] = [dict(substance) for substance in row.substances]
    if row.trade_names:
        props["trade_names"] = list(row.trade_names)
    if row.withdrawal_date:
        props["withdrawal_date"] = row.withdrawal_date
    return props


def _upsert_uses(session: Session, rows: list[tuple[int, sources.UseRow]]) -> int:
    """Insert the changed products' uses, chunked under the bind limit. Exact source duplicates
    (the same content hash per item) are collapsed — a single `ON CONFLICT` statement may not
    touch one row twice — and an existing identical use is left alone (`ON CONFLICT DO NOTHING`:
    every column is part of the hash). Returns the distinct use count written."""
    seen: set[tuple[int, str]] = set()
    deduped: list[tuple[int, sources.UseRow]] = []
    for item_id, use in rows:
        key = (item_id, _use_key(use))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((item_id, use))
    for start in range(0, len(deduped), USE_BATCH):
        batch = deduped[start : start + USE_BATCH]
        statement = (
            pg_insert(CatalogUse)
            .values(
                [
                    {
                        "catalog_item_id": item_id,
                        "use_key": _use_key(use),
                        "identifiant": use.identifiant,
                        "crop": use.crop,
                        "target": use.target,
                        "max_dose": use.max_dose,
                        "max_dose_unit": use.max_dose_unit,
                        "interval_days": use.interval_days,
                        "max_applications": use.max_applications,
                        "phi_days": use.phi_days,
                        "distribution_end": use.distribution_end,
                        "use_end": use.use_end,
                        "znt_aquatic": use.znt_aquatic,
                        "znt_arthropods": use.znt_arthropods,
                        "znt_plants": use.znt_plants,
                        "state": use.state,
                    }
                    for item_id, use in batch
                ]
            )
            .on_conflict_do_nothing(index_elements=["catalog_item_id", "use_key"])
        )
        session.execute(statement)
    return len(deduped)


def _delete_stale_uses(session: Session, item_id: int, keep_keys: set[str]) -> None:
    """Remove the uses of a changed product whose key no longer exists upstream, so a changed
    label never leaves a ghost row behind."""
    if keep_keys:
        session.execute(
            delete(CatalogUse).where(
                CatalogUse.catalog_item_id == item_id,
                CatalogUse.use_key.not_in(keep_keys),
            )
        )
    else:
        session.execute(delete(CatalogUse).where(CatalogUse.catalog_item_id == item_id))


def _existing_items(session: Session, amms: list[str]) -> dict[str, CatalogItem]:
    if not amms:
        return {}
    rows = session.execute(
        select(CatalogAuthorisation.number, CatalogItem)
        .join(CatalogItem, CatalogItem.id == CatalogAuthorisation.catalog_item_id)
        .where(CatalogAuthorisation.country == COUNTRY, CatalogAuthorisation.number.in_(amms))
    ).all()
    return {number: item for number, item in rows}


def import_ephy(
    session: Session,
    *,
    products_path: Path | str,
    uses_path: Path | str,
    retrieved_at: datetime | None = None,
    meta: sources.SourceMeta | None = None,
) -> ImportReport:
    """Import the two E-Phy CSV files, keyed on the FR AMM, idempotent on the content hash."""
    products_raw = Path(products_path).read_bytes()
    uses_raw = Path(uses_path).read_bytes()
    fingerprint = sources.content_hash(products_raw, uses_raw)
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
            changed=False, products=0, uses=0, skipped=0, reasons=(), content_hash=fingerprint
        )

    at = retrieved_at or sources.now()
    meta = meta or sources.SourceMeta()
    products = sources.parse_products(products_raw)
    uses = sources.parse_uses(uses_raw)
    known = {row.amm for row in products.rows}
    uses_by_amm: dict[str, list[sources.UseRow]] = {}
    unknown_uses = 0
    for use in uses.rows:
        if use.amm in known:
            uses_by_amm.setdefault(use.amm, []).append(use)
        else:
            unknown_uses += 1

    reasons = [*products.reasons, *uses.reasons]
    if unknown_uses:
        reasons.append(f"{unknown_uses} uses reference an unknown AMM")
    skipped = products.skipped + uses.skipped + unknown_uses

    existing = _existing_items(session, [row.amm for row in products.rows])
    new_items: list[tuple[str, CatalogItem]] = []
    changed: list[tuple[CatalogItem, list[sources.UseRow]]] = []
    products_written = 0
    for row in products.rows:
        uses_for = uses_by_amm.get(row.amm, [])
        product_hash = _product_fingerprint(row, uses_for)
        item = existing.get(row.amm)
        if item is not None and item.props.get("content_hash") == product_hash:
            continue
        if item is None:
            item = CatalogItem(name=row.name)
            session.add(item)
            new_items.append((row.amm, item))
        else:
            item.name = row.name
        item.props = _props(row, product_hash)
        changed.append((item, uses_for))
        products_written += 1
    session.flush()  # new items get their ids

    for amm, item in new_items:
        session.add(CatalogAuthorisation(catalog_item_id=item.id, country=COUNTRY, number=amm))
    session.flush()

    new_item_ids = {item.id for _, item in new_items}
    use_rows: list[tuple[int, sources.UseRow]] = []
    for item, uses_for in changed:
        if item.id not in new_item_ids:
            _delete_stale_uses(session, item.id, {_use_key(use) for use in uses_for})
        use_rows.extend((item.id, use) for use in uses_for)
    uses_written = _upsert_uses(session, use_rows)

    source_use_count = sum(
        len({_use_key(use) for use in uses_for}) for uses_for in uses_by_amm.values()
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
            product_count=len(products.rows),
            use_count=source_use_count,
            skipped=skipped,
            reasons=list(reasons[:REASON_CAP]),
        )
    )
    return ImportReport(
        changed=True,
        products=products_written,
        uses=uses_written,
        skipped=skipped,
        reasons=tuple(reasons[:REASON_CAP]),
        content_hash=fingerprint,
    )

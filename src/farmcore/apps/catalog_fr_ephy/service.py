"""Idempotent E-Phy import: neutral catalog items keyed on the FR AMM, per-country
authorisations, registered uses and the import fingerprint.

A run whose source content hash matches the last import writes nothing. A changed source
reconciles each product in place (the source is authoritative): its name and properties are
refreshed and its uses rebuilt. Withdrawn products are flagged on the item and never deleted, so
historical records keep their link.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from farmcore.apps.catalog_fr_ephy import sources
from farmcore.apps.catalog_fr_ephy.models import CatalogImport, CatalogUse
from farmcore.core.models.catalog import CatalogAuthorisation, CatalogItem

COUNTRY = "FR"


@dataclass(frozen=True)
class ImportReport:
    changed: bool
    products: int
    uses: int
    skipped: int
    content_hash: str


def _props(row: sources.ProductRow, retrieved_at: datetime) -> dict[str, object]:
    props: dict[str, object] = {
        "source": sources.SOURCE,
        "retrieved_at": retrieved_at.isoformat(),
        "withdrawn": row.withdrawn,
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
    if row.withdrawal_date:
        props["withdrawal_date"] = row.withdrawal_date
    return props


def _find_item(session: Session, amm: str) -> CatalogItem | None:
    return session.scalar(
        select(CatalogItem)
        .join(CatalogAuthorisation, CatalogAuthorisation.catalog_item_id == CatalogItem.id)
        .where(CatalogAuthorisation.country == COUNTRY, CatalogAuthorisation.number == amm)
    )


def import_ephy(
    session: Session,
    *,
    products_path: Path | str,
    uses_path: Path | str,
    retrieved_at: datetime | None = None,
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
        return ImportReport(changed=False, products=0, uses=0, skipped=0, content_hash=fingerprint)

    at = retrieved_at or sources.now()
    products = sources.parse_products(products_raw)
    uses = sources.parse_uses(uses_raw)
    known = {row.amm for row in products.rows}
    uses_by_amm: dict[str, list[sources.UseRow]] = {}
    for use in uses.rows:
        if use.amm in known:
            uses_by_amm.setdefault(use.amm, []).append(use)
    skipped = products.skipped + uses.skipped + sum(1 for use in uses.rows if use.amm not in known)

    item_ids: dict[str, int] = {}
    for row in products.rows:
        item = _find_item(session, row.amm)
        if item is None:
            item = CatalogItem(name=row.name)
            session.add(item)
            session.flush()
            session.add(
                CatalogAuthorisation(catalog_item_id=item.id, country=COUNTRY, number=row.amm)
            )
        else:
            item.name = row.name
        item.props = _props(row, at)
        item_ids[row.amm] = item.id
    session.flush()

    if item_ids:
        session.execute(
            delete(CatalogUse).where(CatalogUse.catalog_item_id.in_(list(item_ids.values())))
        )
    use_count = 0
    for row in products.rows:
        for use in uses_by_amm.get(row.amm, ()):
            session.add(
                CatalogUse(
                    catalog_item_id=item_ids[row.amm],
                    identifiant=use.identifiant,
                    crop=use.crop,
                    target=use.target,
                    max_dose=use.max_dose,
                    max_dose_unit=use.max_dose_unit,
                    interval_days=use.interval_days,
                    max_applications=use.max_applications,
                    state=use.state,
                )
            )
            use_count += 1

    session.add(
        CatalogImport(
            source=sources.SOURCE,
            licence=sources.LICENCE,
            retrieved_at=at,
            content_hash=fingerprint,
            product_count=len(products.rows),
            use_count=use_count,
            skipped=skipped,
        )
    )
    return ImportReport(
        changed=True,
        products=len(products.rows),
        uses=use_count,
        skipped=skipped,
        content_hash=fingerprint,
    )

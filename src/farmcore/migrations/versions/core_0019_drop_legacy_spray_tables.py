"""core: drop the legacy spray tables core_0018 froze (contract)

core_0018 moved every `product` and `quantity` row onto the catalog tables, recorded where each
went in `legacy_product_map` and `legacy_quantity_map`, and froze both tables for its rollback.
Nothing reads them since; this revision drops them. The maps stay: ids kept in historic JSON
and published NGSI-LD v1 ids resolve through them.

Downgrade rebuilds both tables from the maps and the catalog rows only: each mapped item becomes
its legacy product again (name from `catalog_item`, label facts from `crop_protection_item`,
type and targets from `advisor_product`) and each mapped typed quantity its legacy quantity
(as entered: measure, value, unit, label), under the legacy ids. It refuses, naming ids, while
anything has no legacy row to return to: a plant-protection item or a measured (spray-shaped)
typed quantity without a map entry, or a mapped item without crop protection's attributes.
core_0018's own downgrade rules then apply. The rebuild holds the rows as they are now, so a
moved dose a correction changed or removed returns changed or not at all: core_0018's refusals
over the frozen originals only hold while the tables exist.

Revision ID: core_0019
Revises: core_0018
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from farmcore.core.migration_refusal import MigrationRefused

revision: str = "core_0019"
down_revision: str | None = "core_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEASURES = ("rate", "volume", "area")
PRODUCT_TYPES = ("contact", "penetrant")
TARGETS = ("downy", "powdery")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    op.drop_table("quantity")
    op.drop_table("product")


def _ids(bind: sa.Connection, sql: str) -> list[int]:
    return list(bind.execute(sa.text(sql)).scalars())


def _named(what: str, ids: list[int]) -> str:
    shown = ", ".join(str(i) for i in ids[:20]) + (" …" if len(ids) > 20 else "")
    return f"{len(ids)} {what} ({shown})"


def _refusals(bind: sa.Connection) -> list[str]:
    checks = (
        (
            "plant-protection item(s) without a legacy product id",
            "SELECT c.catalog_item_id FROM crop_protection_item c WHERE NOT EXISTS (SELECT 1 "
            "FROM legacy_product_map m WHERE m.catalog_item_id = c.catalog_item_id) ORDER BY 1",
        ),
        (
            "item(s) with a legacy product id but no crop protection type",
            "SELECT m.catalog_item_id FROM legacy_product_map m WHERE NOT EXISTS (SELECT 1 "
            "FROM advisor_product a WHERE a.catalog_item_id = m.catalog_item_id) ORDER BY 1",
        ),
        (
            "measured typed quantit(y/ies) without a legacy quantity id",
            "SELECT t.id FROM typed_quantity t WHERE t.entered_measure IS NOT NULL AND NOT "
            "EXISTS (SELECT 1 FROM legacy_quantity_map m WHERE m.typed_quantity_id = t.id) "
            "ORDER BY 1",
        ),
    )
    return [_named(what, ids) for what, sql in checks if (ids := _ids(bind, sql))]


def downgrade() -> None:
    bind = op.get_bind()
    refusals = _refusals(bind)
    if refusals:
        raise MigrationRefused(
            "core_0019 downgrade refused, nothing was changed: "
            + "; ".join(refusals)
            + "; they have no legacy row to return to: back them up and remove them"
        )
    op.create_table(
        "product",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("authorisation_no", sa.Text(), nullable=False),
        sa.Column("active_substance", sa.Text(), nullable=False),
        sa.Column("product_type", sa.Text(), nullable=False),
        sa.Column(
            "targets", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
        ),
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _in("product_type", PRODUCT_TYPES), name=op.f("ck_product_product_type")
        ),
        sa.CheckConstraint(
            "targets <@ ARRAY[" + ", ".join(repr(t) for t in TARGETS) + "]::text[]",
            name=op.f("ck_product_targets"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product")),
    )
    op.create_table(
        "quantity",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("log_id", sa.BigInteger(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=True),
        sa.Column("measure", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.CheckConstraint(_in("measure", MEASURES), name=op.f("ck_quantity_measure")),
        sa.ForeignKeyConstraint(
            ["log_id"], ["log.id"], name=op.f("fk_quantity_log_id_log"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["product_id"], ["product.id"], name=op.f("fk_quantity_product_id_product")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_quantity")),
    )
    op.execute(
        "INSERT INTO product (id, name, authorisation_no, active_substance, product_type, "
        "targets, props) SELECT m.product_id, i.name, c.authorisation_no, c.active_substance, "
        "a.product_type, a.targets, c.props FROM legacy_product_map m "
        "JOIN catalog_item i ON i.id = m.catalog_item_id "
        "JOIN crop_protection_item c ON c.catalog_item_id = m.catalog_item_id "
        "JOIN advisor_product a ON a.catalog_item_id = m.catalog_item_id ORDER BY m.product_id"
    )
    op.execute(
        "INSERT INTO quantity (id, log_id, product_id, measure, value, unit, label) "
        "SELECT m.quantity_id, t.log_id, pm.product_id, t.entered_measure, t.entered_value, "
        "t.entered_unit, t.label FROM legacy_quantity_map m "
        "JOIN typed_quantity t ON t.id = m.typed_quantity_id "
        "LEFT JOIN legacy_product_map pm ON pm.catalog_item_id = t.catalog_item_id "
        "ORDER BY m.quantity_id"
    )
    for table in ("product", "quantity"):
        op.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
            f"coalesce((SELECT max(id) FROM {table}), 0) + 1, false)"
        )

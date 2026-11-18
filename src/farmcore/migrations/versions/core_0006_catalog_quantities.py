"""core: neutral catalog items and typed quantities beside the spray data (expand)

Adds `catalog_item` (optionally mirroring a legacy `product`), `catalog_authorisation` (one
number per country), the `crop_protection_item` extension (contact/penetrant, downy/powdery),
`typed_quantity` (standard measure/value/unit plus the entered value and unit, label and
provenance) and the nullable `log.crop_eppo` (Reg. (EU) 2023/564 record content).

Purely additive. Every existing product is mirrored with its extension, and every existing
quantity as a `legacy_quantity` typed quantity keeping its entered value and unit. The standard
value comes from the frozen unit table below (the legacy writers' units, SI factors); any other
unit keeps a null standard value instead of a guess. No authorisation country is inferred from
the legacy `authorisation_no`, and no crop code is set on existing logs.

Downgrade drops the mirror (it is derivable from `product` and `quantity`) but refuses while
catalog-only facts exist: items without a product, authorisations, entered typed quantities or
a log crop code.

Revision ID: core_0006
Revises: core_0005
Create Date: 2026-11-18
"""

from collections.abc import Sequence
from decimal import Decimal

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "core_0006"
down_revision: str | None = "core_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of the registry subset legacy rows use: entered unit -> (measure, unit, factor).
_MASS = {"kg": Decimal(1), "g": Decimal("0.001"), "mg": Decimal("0.000001"), "t": Decimal(1000)}
_VOLUME = {
    "l": Decimal(1),
    "L": Decimal(1),
    "ml": Decimal("0.001"),
    "mL": Decimal("0.001"),
    "hl": Decimal(100),
    "hL": Decimal(100),
}
UNITS: dict[str, tuple[str, str, Decimal]] = {
    **{u: ("mass", "kg", f) for u, f in _MASS.items()},
    **{u: ("volume", "l", f) for u, f in _VOLUME.items()},
    **{f"{u}/ha": ("mass_rate", "kg/ha", f) for u, f in _MASS.items()},
    **{f"{u}/ha": ("volume_rate", "l/ha", f) for u, f in _VOLUME.items()},
    "ha": ("area", "ha", Decimal(1)),
    "m2": ("area", "ha", Decimal("0.0001")),
    "count": ("count", "count", Decimal(1)),
    "count/ha": ("count_rate", "count/ha", Decimal(1)),
}
STANDARD = (
    ("mass", "kg"),
    ("volume", "l"),
    ("area", "ha"),
    ("count", "count"),
    ("mass_rate", "kg/ha"),
    ("volume_rate", "l/ha"),
    ("count_rate", "count/ha"),
)
STANDARD_CHECK = "(measure IS NULL AND value IS NULL AND unit IS NULL) OR " + " OR ".join(
    f"(measure = '{m}' AND unit = '{u}')" for m, u in STANDARD
)


def upgrade() -> None:
    op.add_column("log", sa.Column("crop_eppo", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_log_crop_eppo"), "log", "crop_eppo IS NULL OR crop_eppo ~ '^[0-9A-Z]{5,6}$'"
    )
    op.create_table(
        "catalog_item",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("product_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(btrim(name)) BETWEEN 1 AND 200", name=op.f("ck_catalog_item_name")
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["product.id"],
            name=op.f("fk_catalog_item_product_id_product"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_item")),
        sa.UniqueConstraint("product_id", name=op.f("uq_catalog_item_product_id")),
    )
    op.create_table(
        "catalog_authorisation",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("catalog_item_id", sa.BigInteger(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("number", sa.Text(), nullable=False),
        sa.CheckConstraint("country ~ '^[A-Z]{2}$'", name=op.f("ck_catalog_authorisation_country")),
        sa.CheckConstraint(
            "length(btrim(number)) BETWEEN 1 AND 64", name=op.f("ck_catalog_authorisation_number")
        ),
        sa.ForeignKeyConstraint(
            ["catalog_item_id"],
            ["catalog_item.id"],
            name=op.f("fk_catalog_authorisation_catalog_item_id_catalog_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_authorisation")),
        sa.UniqueConstraint(
            "catalog_item_id",
            "country",
            name=op.f("uq_catalog_authorisation_catalog_item_id_country"),
        ),
    )
    op.create_table(
        "crop_protection_item",
        sa.Column("catalog_item_id", sa.BigInteger(), nullable=False),
        sa.Column("product_type", sa.Text(), nullable=False),
        sa.Column("targets", sa.ARRAY(sa.Text()), nullable=False),
        sa.CheckConstraint(
            "product_type IN ('contact', 'penetrant')",
            name=op.f("ck_crop_protection_item_product_type"),
        ),
        sa.CheckConstraint(
            "targets <@ ARRAY['downy', 'powdery']::text[]",
            name=op.f("ck_crop_protection_item_targets"),
        ),
        sa.ForeignKeyConstraint(
            ["catalog_item_id"],
            ["catalog_item.id"],
            name=op.f("fk_crop_protection_item_catalog_item_id_catalog_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("catalog_item_id", name=op.f("pk_crop_protection_item")),
    )
    op.create_table(
        "typed_quantity",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("log_id", sa.BigInteger(), nullable=False),
        sa.Column("catalog_item_id", sa.BigInteger(), nullable=True),
        sa.Column("legacy_quantity_id", sa.BigInteger(), nullable=True),
        sa.Column("measure", sa.Text(), nullable=True),
        sa.Column("value", sa.Numeric(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("entered_value", sa.Numeric(), nullable=False),
        sa.Column("entered_unit", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.CheckConstraint(STANDARD_CHECK, name=op.f("ck_typed_quantity_standard")),
        sa.CheckConstraint(
            "value IS NULL OR value::text <> 'NaN'", name=op.f("ck_typed_quantity_value")
        ),
        sa.CheckConstraint(
            "entered_value::text <> 'NaN'", name=op.f("ck_typed_quantity_entered_value")
        ),
        sa.CheckConstraint(
            "length(btrim(entered_unit)) > 0", name=op.f("ck_typed_quantity_entered_unit")
        ),
        sa.CheckConstraint(
            "provenance IN ('legacy_quantity', 'entered')",
            name=op.f("ck_typed_quantity_provenance"),
        ),
        sa.ForeignKeyConstraint(
            ["catalog_item_id"],
            ["catalog_item.id"],
            name=op.f("fk_typed_quantity_catalog_item_id_catalog_item"),
        ),
        sa.ForeignKeyConstraint(
            ["legacy_quantity_id"],
            ["quantity.id"],
            name=op.f("fk_typed_quantity_legacy_quantity_id_quantity"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["log_id"], ["log.id"], name=op.f("fk_typed_quantity_log_id_log"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_typed_quantity")),
        sa.UniqueConstraint(
            "legacy_quantity_id", name=op.f("uq_typed_quantity_legacy_quantity_id")
        ),
    )
    op.create_index("ix_typed_quantity_log_id", "typed_quantity", ["log_id"])

    # Mirror the legacy rows.
    op.execute(
        "INSERT INTO catalog_item (name, product_id) SELECT name, id FROM product ORDER BY id"
    )
    op.execute(
        "INSERT INTO crop_protection_item (catalog_item_id, product_type, targets) "
        "SELECT c.id, p.product_type, p.targets FROM catalog_item c JOIN product p "
        "ON p.id = c.product_id"
    )
    op.get_bind().execute(
        sa.text(
            "INSERT INTO typed_quantity (log_id, catalog_item_id, legacy_quantity_id, measure, "
            "value, unit, entered_value, entered_unit, label, provenance) "
            "SELECT q.log_id, c.id, q.id, u.measure, q.value * u.factor, u.unit, q.value, "
            "q.unit, q.label, 'legacy_quantity' FROM quantity q "
            "LEFT JOIN catalog_item c ON c.product_id = q.product_id "
            "LEFT JOIN (SELECT * FROM unnest(CAST(:entered AS text[]), CAST(:measure AS text[]), "
            "CAST(:unit AS text[]), CAST(:factor AS numeric[])) "
            "AS m(entered, measure, unit, factor)) u "
            "ON u.entered = regexp_replace(q.unit, '\\s+', '', 'g') "
            "WHERE q.value::text <> 'NaN' ORDER BY q.id"
        ),
        {
            "entered": list(UNITS),
            "measure": [row[0] for row in UNITS.values()],
            "unit": [row[1] for row in UNITS.values()],
            "factor": [row[2] for row in UNITS.values()],
        },
    )


def downgrade() -> None:
    bind = op.get_bind()
    facts = bind.execute(
        sa.text(
            "SELECT (SELECT count(*) FROM catalog_item WHERE product_id IS NULL) "
            "+ (SELECT count(*) FROM catalog_authorisation) "
            "+ (SELECT count(*) FROM typed_quantity WHERE provenance <> 'legacy_quantity') "
            "+ (SELECT count(*) FROM log WHERE crop_eppo IS NOT NULL)"
        )
    ).scalar_one()
    if facts:
        raise RuntimeError(
            f"core_0006 downgrade refused: {facts} catalog-only fact(s) exist (catalog items, "
            "authorisations, entered quantities or log crop codes); back them up and remove "
            "them first"
        )
    op.drop_table("typed_quantity")
    op.drop_table("crop_protection_item")
    op.drop_table("catalog_authorisation")
    op.drop_table("catalog_item")
    op.drop_constraint(op.f("ck_log_crop_eppo"), "log", type_="check")
    op.drop_column("log", "crop_eppo")

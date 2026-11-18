"""core: neutral catalog items and typed quantities beside the spray data (expand)

Adds the frozen `unit_definition` table (every unit string FarmCore accepts, its measure and its
factor into the standard unit; a parity test keeps it equal to `farmcore.core.units`), the
catalog tables for entered data (`catalog_item`, `catalog_authorisation` with one number per
country, the `crop_protection_item` extension, `typed_quantity`) and the nullable
`log.crop_eppo` (Reg. (EU) 2023/564 record content).

Legacy rows are not copied. Two views expose them in the neutral shape, so they cannot drift:
`legacy_catalog_item` over `product`, and `legacy_typed_quantity` over `quantity` joined to
`unit_definition` by the exact entered unit. An unknown unit or a NaN value has a null standard
value. Nothing is inferred: no authorisation country from `authorisation_no`, no crop code on
existing logs.

Downgrade refuses while entered catalog facts exist (catalog items, typed quantities or a log
crop code); everything else is the views and the unit table.

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

# Frozen unit names: name -> (base measure, factor into its standard unit). SI factors.
NAMES: dict[str, tuple[str, Decimal]] = {
    **{n: ("mass", Decimal(1)) for n in ("kg", "kilogram", "KG", "Kg")},
    **{n: ("mass", Decimal("0.001")) for n in ("g", "gram")},
    **{n: ("mass", Decimal("0.000001")) for n in ("mg", "milligram")},
    **{n: ("mass", Decimal(1000)) for n in ("t", "tonne")},
    **{n: ("volume", Decimal(1)) for n in ("l", "litre", "L", "lt")},
    **{n: ("volume", Decimal("0.001")) for n in ("ml", "millilitre", "mL")},
    **{n: ("volume", Decimal(100)) for n in ("hl", "hectolitre", "hL")},
    **{n: ("volume", Decimal(1000)) for n in ("m3", "cubic_metre")},
    **{n: ("area", Decimal(1)) for n in ("ha", "hectare", "Ha")},
    "are": ("area", Decimal("0.01")),
    **{n: ("area", Decimal("0.0001")) for n in ("m2", "square_metre")},
    **{n: ("count", Decimal(1)) for n in ("count", "item")},
}
STANDARD = {
    "mass": "kg",
    "volume": "l",
    "area": "ha",
    "count": "count",
    "mass_rate": "kg/ha",
    "volume_rate": "l/ha",
    "count_rate": "count/ha",
}
STANDARD_CHECK = " OR ".join(f"(measure = '{m}' AND unit = '{u}')" for m, u in STANDARD.items())


def unit_rows() -> list[dict[str, object]]:
    """Every accepted string: a name, or an amount name per area name (a rate per hectare)."""
    rows = [
        {"entered": n, "measure": m, "unit": STANDARD[m], "factor": f}
        for n, (m, f) in NAMES.items()
    ]
    for top, (measure, top_factor) in NAMES.items():
        if measure == "area":
            continue
        for bottom, (bottom_measure, bottom_factor) in NAMES.items():
            if bottom_measure == "area":
                rate = f"{measure}_rate"
                rows.append(
                    {
                        "entered": f"{top}/{bottom}",
                        "measure": rate,
                        "unit": STANDARD[rate],
                        "factor": top_factor / bottom_factor,
                    }
                )
    return rows


def upgrade() -> None:
    op.add_column("log", sa.Column("crop_eppo", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_log_crop_eppo"), "log", "crop_eppo IS NULL OR crop_eppo ~ '^[0-9A-Z]{5,6}$'"
    )
    unit_definition = op.create_table(
        "unit_definition",
        sa.Column("entered", sa.Text(), nullable=False),
        sa.Column("measure", sa.Text(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("factor", sa.Numeric(), nullable=False),
        sa.CheckConstraint(STANDARD_CHECK, name=op.f("ck_unit_definition_standard")),
        sa.CheckConstraint("factor > 0", name=op.f("ck_unit_definition_factor")),
        sa.PrimaryKeyConstraint("entered", name=op.f("pk_unit_definition")),
    )
    op.bulk_insert(unit_definition, unit_rows())
    op.create_table(
        "catalog_item",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalog_item")),
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
        sa.Column("measure", sa.Text(), nullable=True),
        sa.Column("value", sa.Numeric(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=True),
        sa.Column("entered_value", sa.Numeric(), nullable=False),
        sa.Column("entered_unit", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("provenance", sa.Text(), server_default="entered", nullable=False),
        sa.CheckConstraint(
            "(measure IS NULL AND value IS NULL AND unit IS NULL) OR " + STANDARD_CHECK,
            name=op.f("ck_typed_quantity_standard"),
        ),
        sa.CheckConstraint(
            "value IS NULL OR value <> 'NaN'::numeric", name=op.f("ck_typed_quantity_value")
        ),
        sa.CheckConstraint(
            "entered_value <> 'NaN'::numeric", name=op.f("ck_typed_quantity_entered_value")
        ),
        sa.CheckConstraint(
            "length(btrim(entered_unit)) > 0", name=op.f("ck_typed_quantity_entered_unit")
        ),
        sa.CheckConstraint("provenance = 'entered'", name=op.f("ck_typed_quantity_provenance")),
        sa.ForeignKeyConstraint(
            ["catalog_item_id"],
            ["catalog_item.id"],
            name=op.f("fk_typed_quantity_catalog_item_id_catalog_item"),
        ),
        sa.ForeignKeyConstraint(
            ["log_id"], ["log.id"], name=op.f("fk_typed_quantity_log_id_log"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_typed_quantity")),
    )
    op.create_index("ix_typed_quantity_log_id", "typed_quantity", ["log_id"])
    op.execute(
        "CREATE VIEW legacy_catalog_item AS "
        "SELECT p.id AS product_id, p.name, p.product_type, p.targets, "
        "'legacy_product'::text AS provenance FROM product p"
    )
    op.execute(
        "CREATE VIEW legacy_typed_quantity AS "
        "SELECT q.id AS legacy_quantity_id, q.log_id, q.product_id, u.measure, "
        "q.value * u.factor AS value, u.unit, q.value AS entered_value, q.unit AS entered_unit, "
        "q.label, 'legacy_quantity'::text AS provenance FROM quantity q "
        "LEFT JOIN unit_definition u ON u.entered = q.unit AND q.value <> 'NaN'::numeric"
    )


def downgrade() -> None:
    facts = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT (SELECT count(*) FROM catalog_item) "
                "+ (SELECT count(*) FROM typed_quantity) "
                "+ (SELECT count(*) FROM log WHERE crop_eppo IS NOT NULL)"
            )
        )
        .scalar_one()
    )
    if facts:
        raise RuntimeError(
            f"core_0006 downgrade refused: {facts} entered catalog fact(s) exist (catalog items, "
            "typed quantities or log crop codes); back them up and remove them first"
        )
    op.execute("DROP VIEW legacy_typed_quantity")
    op.execute("DROP VIEW legacy_catalog_item")
    op.drop_table("typed_quantity")
    op.drop_table("crop_protection_item")
    op.drop_table("catalog_authorisation")
    op.drop_table("catalog_item")
    op.drop_table("unit_definition")
    op.drop_constraint(op.f("ck_log_crop_eppo"), "log", type_="check")
    op.drop_column("log", "crop_eppo")

"""core: move spray products and doses onto the catalog tables, and freeze the legacy tables

Every `product` becomes a `catalog_item` (its name) with operations' `crop_protection_item`
extension (authorisation number as printed, active substance, props) and crop protection's
`advisor_product` (contact/penetrant, targets); every `quantity` becomes a `typed_quantity` that
keeps what was entered (value, unit, label, and the measure in the new `entered_measure`), with
the standard measure/value/unit from the frozen `unit_definition` table (the same join the
`legacy_typed_quantity` view made, so stock reads the same numbers). Both are inserted in legacy
id order. `legacy_product_map` and `legacy_quantity_map` record where each row went: ids kept in
historic JSON (events, revisions, corrections, evidence) and published NGSI-LD v1 ids resolve
through them, and are never rewritten.

Ids that live outside the two tables move with them: stock rows and planning jobs name the
catalog item (`product_id` is dropped, so an old reader fails loudly), the farm shortlist key
becomes `catalog_item_ids`, `ext_id` rows of model `product` point at the catalog item, and open
spray drafts name `catalog_item_id`. The views over the legacy tables are dropped, so no reader
can see a dose twice; `product` and `quantity` stay, unchanged, for this revision's rollback
(`farmcore migrate` revokes writes on them and on the maps from the runtime role).

One transaction. A read-only preflight first lists every row the new checks would reject
(names, NaN values, blank units, unknown measures, attributes outside crop protection's checks,
malformed shortlists, existing extension rows, an irrigation that would end with more than one
amount) and refuses with all of them: legacy data is never coerced. A post-check compares
counts and per-record dose totals before the transaction commits.

Downgrade refuses while anything written since the upgrade has no legacy row to return to (a
spray dose or plant-protection item without a map entry, a stock row, planning job or shortlist
entry naming one) or a moved dose was changed or removed, naming the counts. Otherwise it maps
everything back and deletes the moved rows: the frozen tables were never changed.

Revision ID: core_0018
Revises: core_0017
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "core_0018"
down_revision: str | None = "core_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEASURES = ("rate", "volume", "area")  # legacy `quantity.measure`, kept as entered
PRODUCT_TYPES = ("contact", "penetrant")
TARGETS = ("downy", "powdery")
LEGACY_VIEWS = ("legacy_typed_quantity", "legacy_catalog_item")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ids(bind: sa.Connection, sql: str) -> list[int]:
    return list(bind.execute(sa.text(sql)).scalars())


def _named(what: str, ids: list[int], fix: str) -> str:
    shown = ", ".join(str(i) for i in ids[:20]) + (" …" if len(ids) > 20 else "")
    return f"{len(ids)} {what} ({shown}): {fix}"


def _preflight(bind: sa.Connection) -> None:
    """Every blocker at once; nothing is written before this passes."""
    checks = [
        (
            "product(s) with a blank name or one over 200 characters",
            "SELECT id FROM product WHERE length(btrim(name)) NOT BETWEEN 1 AND 200 ORDER BY id",
            "rename them",
        ),
        (
            "product(s) whose type or targets crop protection does not accept",
            "SELECT id FROM product WHERE product_type IS NULL OR NOT "
            + _in("product_type", PRODUCT_TYPES)
            + " OR targets IS NULL OR NOT targets <@ ARRAY["
            + ", ".join(repr(t) for t in TARGETS)
            + "]::text[] ORDER BY id",
            "correct them",
        ),
        (
            "quantit(y/ies) with a NaN value",
            "SELECT id FROM quantity WHERE value = 'NaN'::numeric ORDER BY id",
            "enter the dose again or remove it",
        ),
        (
            "quantit(y/ies) with a blank unit",
            "SELECT id FROM quantity WHERE unit IS NULL OR length(btrim(unit)) = 0 ORDER BY id",
            "enter the unit",
        ),
        (
            "quantit(y/ies) with an unknown measure",
            "SELECT id FROM quantity WHERE measure IS NULL OR NOT "
            + _in("measure", MEASURES)
            + " ORDER BY id",
            "correct the measure",
        ),
        (
            "crop-protection extension row(s) already present (nothing writes them before this "
            "revision)",
            "SELECT catalog_item_id FROM crop_protection_item ORDER BY catalog_item_id",
            "remove them",
        ),
        (
            "farm(s) whose product shortlist is not a list of ids",
            "SELECT id FROM farm WHERE props ? 'product_ids' AND (jsonb_typeof(props -> "
            "'product_ids') <> 'array' OR EXISTS (SELECT 1 FROM jsonb_array_elements(props -> "
            "'product_ids') e WHERE jsonb_typeof(e) <> 'number')) ORDER BY id",
            "correct the farm's product_ids",
        ),
        (
            "irrigation(s) that would hold more than one amount (a legacy quantity beside the "
            "entered one)",
            "SELECT l.id FROM log l WHERE l.type = 'irrigate' AND "
            "(SELECT count(*) FROM quantity q WHERE q.log_id = l.id) "
            "+ (SELECT count(*) FROM typed_quantity t WHERE t.log_id = l.id) > 1 ORDER BY l.id",
            "keep one amount per irrigation (remove the extra quantity rows)",
        ),
    ]
    blockers = [_named(what, ids, fix) for what, sql, fix in checks if (ids := _ids(bind, sql))]
    if blockers:
        raise RuntimeError(
            "core_0018 refused, nothing was changed: " + "; ".join(blockers) + ". Fix these "
            "rows (back up first) and run migrate again."
        )


def _schema() -> None:
    op.add_column("typed_quantity", sa.Column("entered_measure", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_typed_quantity_entered_measure"),
        "typed_quantity",
        _in("entered_measure", MEASURES),
    )
    for name in ("product_type", "targets"):
        op.drop_constraint(
            op.f(f"ck_crop_protection_item_{name}"), "crop_protection_item", type_="check"
        )
        op.drop_column("crop_protection_item", name)
    op.add_column("crop_protection_item", sa.Column("authorisation_no", sa.Text(), nullable=False))
    op.add_column("crop_protection_item", sa.Column("active_substance", sa.Text(), nullable=False))
    op.add_column(
        "crop_protection_item",
        sa.Column(
            "props",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_table(
        "advisor_product",
        sa.Column("catalog_item_id", sa.BigInteger(), nullable=False),
        sa.Column("product_type", sa.Text(), nullable=False),
        sa.Column(
            "targets", sa.ARRAY(sa.Text()), server_default=sa.text("'{}'::text[]"), nullable=False
        ),
        sa.CheckConstraint(
            _in("product_type", PRODUCT_TYPES), name=op.f("ck_advisor_product_product_type")
        ),
        sa.CheckConstraint(
            "targets <@ ARRAY[" + ", ".join(repr(t) for t in TARGETS) + "]::text[]",
            name=op.f("ck_advisor_product_targets"),
        ),
        sa.ForeignKeyConstraint(
            ["catalog_item_id"],
            ["crop_protection_item.catalog_item_id"],
            name=op.f("fk_advisor_product_catalog_item_id_crop_protection_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("catalog_item_id", name=op.f("pk_advisor_product")),
    )
    op.create_table(
        "legacy_product_map",
        sa.Column("product_id", sa.BigInteger(), nullable=False),
        sa.Column("catalog_item_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["catalog_item_id"],
            ["catalog_item.id"],
            name=op.f("fk_legacy_product_map_catalog_item_id_catalog_item"),
        ),
        sa.PrimaryKeyConstraint("product_id", name=op.f("pk_legacy_product_map")),
        sa.UniqueConstraint("catalog_item_id", name=op.f("uq_legacy_product_map_catalog_item_id")),
    )
    op.create_table(
        "legacy_quantity_map",
        sa.Column("quantity_id", sa.BigInteger(), nullable=False),
        sa.Column("typed_quantity_id", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["typed_quantity_id"],
            ["typed_quantity.id"],
            name=op.f("fk_legacy_quantity_map_typed_quantity_id_typed_quantity"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("quantity_id", name=op.f("pk_legacy_quantity_map")),
        sa.UniqueConstraint(
            "typed_quantity_id", name=op.f("uq_legacy_quantity_map_typed_quantity_id")
        ),
    )


def _allocate(bind: sa.Connection, table: str, count: int) -> int:
    """Reserve `count` consecutive ids of `table`; returns the first."""
    seq = f"pg_get_serial_sequence('{table}', 'id')"
    first = bind.execute(sa.text(f"SELECT nextval({seq})")).scalar_one()
    bind.execute(sa.text(f"SELECT setval({seq}, :last)"), {"last": first + count - 1})
    return first


def _plan(bind: sa.Connection, name: str, legacy: str, table: str) -> int:
    """Temp table `name` (legacy id, new id): new ids reserved in one block, in legacy id
    order. Returns the row count."""
    count = bind.execute(sa.text(f"SELECT count(*) FROM {legacy}")).scalar_one()
    first = _allocate(bind, table, count) if count else 0
    bind.execute(
        sa.text(
            f"CREATE TEMP TABLE {name} ON COMMIT DROP AS SELECT id AS legacy_id, "
            f":first + row_number() OVER (ORDER BY id) - 1 AS id FROM {legacy}"
        ).bindparams(first=first)
    )
    return count


def _move(bind: sa.Connection) -> None:
    """Insert the moved rows in legacy id order, then the maps."""
    if _plan(bind, "core_0018_items", "product", "catalog_item"):
        for sql in (
            "INSERT INTO catalog_item (id, name) SELECT i.id, p.name FROM core_0018_items i "
            "JOIN product p ON p.id = i.legacy_id ORDER BY i.id",
            "INSERT INTO crop_protection_item "
            "(catalog_item_id, authorisation_no, active_substance, props) "
            "SELECT i.id, p.authorisation_no, p.active_substance, p.props "
            "FROM core_0018_items i JOIN product p ON p.id = i.legacy_id ORDER BY i.id",
            "INSERT INTO advisor_product (catalog_item_id, product_type, targets) "
            "SELECT i.id, p.product_type, p.targets "
            "FROM core_0018_items i JOIN product p ON p.id = i.legacy_id ORDER BY i.id",
            "INSERT INTO legacy_product_map (product_id, catalog_item_id) "
            "SELECT legacy_id, id FROM core_0018_items ORDER BY id",
        ):
            bind.execute(sa.text(sql))
    if _plan(bind, "core_0018_doses", "quantity", "typed_quantity"):
        for sql in (
            # The standard value as the dropped view computed it (the frozen unit table).
            "INSERT INTO typed_quantity (id, log_id, catalog_item_id, measure, value, unit, "
            "entered_value, entered_unit, label, provenance, entered_measure) "
            "SELECT d.id, q.log_id, pm.catalog_item_id, u.measure, q.value * u.factor, u.unit, "
            "q.value, q.unit, q.label, 'entered', q.measure "
            "FROM core_0018_doses d JOIN quantity q ON q.id = d.legacy_id "
            "LEFT JOIN legacy_product_map pm ON pm.product_id = q.product_id "
            "LEFT JOIN unit_definition u ON u.entered = q.unit ORDER BY d.id",
            "INSERT INTO legacy_quantity_map (quantity_id, typed_quantity_id) "
            "SELECT legacy_id, id FROM core_0018_doses ORDER BY id",
        ):
            bind.execute(sa.text(sql))
    bind.execute(sa.text("DROP TABLE core_0018_items, core_0018_doses"))


def _remap() -> None:
    """Every id kept outside the two tables, onto the catalog item."""
    op.drop_constraint(op.f("ck_inventory_stock_item"), "inventory_stock", "check")
    op.execute(
        "UPDATE inventory_stock s SET catalog_item_id = m.catalog_item_id "
        "FROM legacy_product_map m WHERE s.product_id = m.product_id"
    )
    op.drop_constraint(
        op.f("uq_inventory_stock_farm_id_product_id_season"), "inventory_stock", "unique"
    )
    op.drop_constraint(
        op.f("fk_inventory_stock_product_id_product"), "inventory_stock", "foreignkey"
    )
    op.drop_column("inventory_stock", "product_id")
    op.alter_column(
        "inventory_stock", "catalog_item_id", existing_type=sa.BigInteger(), nullable=False
    )

    op.add_column("planning_job", sa.Column("catalog_item_id", sa.BigInteger(), nullable=True))
    op.execute(
        "UPDATE planning_job j SET catalog_item_id = m.catalog_item_id "
        "FROM legacy_product_map m WHERE j.product_id = m.product_id"
    )
    op.alter_column(
        "planning_job", "catalog_item_id", existing_type=sa.BigInteger(), nullable=False
    )
    op.create_foreign_key(
        op.f("fk_planning_job_catalog_item_id_crop_protection_item"),
        "planning_job",
        "crop_protection_item",
        ["catalog_item_id"],
        ["catalog_item_id"],
    )
    op.drop_constraint(op.f("fk_planning_job_product_id_product"), "planning_job", "foreignkey")
    op.drop_column("planning_job", "product_id")

    # Shortlist: order kept; `[]` stays `[]` (offers nothing); an id no product had is dropped.
    op.execute(
        "UPDATE farm f SET props = (f.props - 'product_ids') || jsonb_build_object("
        "'catalog_item_ids', (SELECT coalesce(jsonb_agg(m.catalog_item_id ORDER BY e.n), "
        "'[]'::jsonb) FROM jsonb_array_elements(f.props -> 'product_ids') WITH ORDINALITY "
        "AS e(v, n) JOIN legacy_product_map m ON m.product_id = (e.v)::text::bigint)) "
        "WHERE f.props ? 'product_ids'"
    )
    op.execute(
        "UPDATE ext_id e SET model = 'catalog_item', res_id = m.catalog_item_id "
        "FROM legacy_product_map m WHERE e.model = 'product' AND e.res_id = m.product_id"
    )
    # Open spray drafts: each dose's `product_id` becomes `catalog_item_id` (an id the map does
    # not hold stays `product_id`, which confirm then refuses as unknown).
    op.execute(
        "UPDATE draft d SET payload = jsonb_set(d.payload, '{doses}', (SELECT "
        "jsonb_agg(CASE WHEN m.catalog_item_id IS NULL THEN e.v ELSE (e.v - 'product_id') || "
        "jsonb_build_object('catalog_item_id', m.catalog_item_id) END ORDER BY e.n) "
        "FROM jsonb_array_elements(d.payload -> 'doses') WITH ORDINALITY AS e(v, n) "
        "LEFT JOIN legacy_product_map m ON jsonb_typeof(e.v -> 'product_id') = 'number' "
        "AND m.product_id = (e.v ->> 'product_id')::bigint)) "
        "WHERE d.kind = 'spray' AND d.status = 'open' "
        "AND jsonb_typeof(d.payload -> 'doses') = 'array' "
        "AND jsonb_array_length(d.payload -> 'doses') > 0"
    )


def _post_check(bind: sa.Connection) -> None:
    """Counts and per-record sums equal, or the whole transaction rolls back."""
    problems = []
    for legacy, mapped in (("product", "legacy_product_map"), ("quantity", "legacy_quantity_map")):
        a = bind.execute(sa.text(f"SELECT count(*) FROM {legacy}")).scalar_one()
        b = bind.execute(sa.text(f"SELECT count(*) FROM {mapped}")).scalar_one()
        if a != b:
            problems.append(f"{a} {legacy} row(s) but {b} mapped")
    drift = bind.execute(
        sa.text(
            "(SELECT q.log_id, pm.catalog_item_id, q.measure, q.unit, sum(q.value) FROM quantity q "
            "LEFT JOIN legacy_product_map pm ON pm.product_id = q.product_id "
            "GROUP BY 1, 2, 3, 4 "
            "EXCEPT SELECT t.log_id, t.catalog_item_id, t.entered_measure, t.entered_unit, "
            "sum(t.entered_value) FROM typed_quantity t JOIN legacy_quantity_map m "
            "ON m.typed_quantity_id = t.id GROUP BY 1, 2, 3, 4) "
            "UNION ALL "
            "(SELECT t.log_id, t.catalog_item_id, t.entered_measure, t.entered_unit, "
            "sum(t.entered_value) FROM typed_quantity t JOIN legacy_quantity_map m "
            "ON m.typed_quantity_id = t.id GROUP BY 1, 2, 3, 4 "
            "EXCEPT SELECT q.log_id, pm.catalog_item_id, q.measure, q.unit, sum(q.value) "
            "FROM quantity q LEFT JOIN legacy_product_map pm ON pm.product_id = q.product_id "
            "GROUP BY 1, 2, 3, 4)"
        )
    ).all()
    if drift:
        problems.append(f"{len(drift)} record dose total(s) differ")
    if problems:
        raise RuntimeError(
            "core_0018 post-check failed, nothing was changed: " + "; ".join(problems)
        )


def upgrade() -> None:
    bind = op.get_bind()
    _preflight(bind)
    _schema()
    _move(bind)
    _remap()
    for view in LEGACY_VIEWS:
        op.execute(f"DROP VIEW {view}")
    _post_check(bind)


# --- downgrade -----------------------------------------------------------------------------

_UNMAPPED_ITEMS = (
    "SELECT c.catalog_item_id FROM crop_protection_item c WHERE NOT EXISTS "
    "(SELECT 1 FROM legacy_product_map m WHERE m.catalog_item_id = c.catalog_item_id)"
)


def _refusals(bind: sa.Connection) -> list[str]:
    counts = {
        "spray dose(s) or amount(s) of a plant-protection product recorded since the upgrade": (
            "SELECT count(*) FROM typed_quantity t WHERE NOT EXISTS "
            "(SELECT 1 FROM legacy_quantity_map m WHERE m.typed_quantity_id = t.id) AND "
            "(t.entered_measure IS NOT NULL OR t.catalog_item_id IN (SELECT catalog_item_id "
            "FROM crop_protection_item))"
        ),
        "plant-protection product(s) added since the upgrade": (
            f"SELECT count(*) FROM ({_UNMAPPED_ITEMS}) x"
        ),
        "stock row(s) of a product added since the upgrade": (
            f"SELECT count(*) FROM inventory_stock WHERE catalog_item_id IN ({_UNMAPPED_ITEMS})"
        ),
        "planning job(s) naming a product added since the upgrade": (
            f"SELECT count(*) FROM planning_job WHERE catalog_item_id IN ({_UNMAPPED_ITEMS})"
        ),
        "farm shortlist(s) naming a product added since the upgrade": (
            "SELECT count(*) FROM farm f WHERE jsonb_typeof(f.props -> 'catalog_item_ids') = "
            "'array' AND EXISTS (SELECT 1 FROM jsonb_array_elements(f.props -> "
            "'catalog_item_ids') e WHERE NOT EXISTS (SELECT 1 FROM legacy_product_map m "
            "WHERE m.catalog_item_id = (e.value)::text::bigint))"
        ),
        "moved dose(s) changed since the upgrade": (
            "SELECT count(*) FROM legacy_quantity_map m "
            "JOIN typed_quantity t ON t.id = m.typed_quantity_id "
            "JOIN quantity q ON q.id = m.quantity_id "
            "LEFT JOIN legacy_product_map pm ON pm.product_id = q.product_id "
            "WHERE (t.log_id, t.entered_value, t.entered_unit, t.entered_measure) IS DISTINCT "
            "FROM (q.log_id, q.value, q.unit, q.measure) OR t.label IS DISTINCT FROM q.label "
            "OR t.catalog_item_id IS DISTINCT FROM pm.catalog_item_id"
        ),
        "moved dose(s) removed since the upgrade": (
            "SELECT count(*) FROM quantity q WHERE NOT EXISTS "
            "(SELECT 1 FROM legacy_quantity_map m WHERE m.quantity_id = q.id)"
        ),
    }
    out = []
    for what, sql in counts.items():
        n = bind.execute(sa.text(sql)).scalar_one()
        if n:
            out.append(f"{n} {what}")
    return out


def downgrade() -> None:
    bind = op.get_bind()
    refusals = _refusals(bind)
    if refusals:
        raise RuntimeError(
            "core_0018 downgrade refused, nothing was changed: " + "; ".join(refusals) + ". "
            "They have no legacy row to return to; back them up and remove them first."
        )
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
    op.execute(
        "UPDATE draft d SET payload = jsonb_set(d.payload, '{doses}', (SELECT "
        "jsonb_agg(CASE WHEN m.product_id IS NULL THEN e.v ELSE (e.v - 'catalog_item_id') || "
        "jsonb_build_object('product_id', m.product_id) END ORDER BY e.n) "
        "FROM jsonb_array_elements(d.payload -> 'doses') WITH ORDINALITY AS e(v, n) "
        "LEFT JOIN legacy_product_map m ON jsonb_typeof(e.v -> 'catalog_item_id') = 'number' "
        "AND m.catalog_item_id = (e.v ->> 'catalog_item_id')::bigint)) "
        "WHERE d.kind = 'spray' AND d.status = 'open' "
        "AND jsonb_typeof(d.payload -> 'doses') = 'array' "
        "AND jsonb_array_length(d.payload -> 'doses') > 0"
    )
    op.execute(
        "UPDATE ext_id e SET model = 'product', res_id = m.product_id "
        "FROM legacy_product_map m WHERE e.model = 'catalog_item' "
        "AND e.res_id = m.catalog_item_id"
    )
    op.execute(
        "UPDATE farm f SET props = (f.props - 'catalog_item_ids') || jsonb_build_object("
        "'product_ids', (SELECT coalesce(jsonb_agg(m.product_id ORDER BY e.n), '[]'::jsonb) "
        "FROM jsonb_array_elements(f.props -> 'catalog_item_ids') WITH ORDINALITY AS e(v, n) "
        "JOIN legacy_product_map m ON m.catalog_item_id = (e.v)::text::bigint)) "
        "WHERE f.props ? 'catalog_item_ids'"
    )

    op.add_column("planning_job", sa.Column("product_id", sa.BigInteger(), nullable=True))
    op.execute(
        "UPDATE planning_job j SET product_id = m.product_id "
        "FROM legacy_product_map m WHERE j.catalog_item_id = m.catalog_item_id"
    )
    op.alter_column("planning_job", "product_id", existing_type=sa.BigInteger(), nullable=False)
    op.create_foreign_key(
        op.f("fk_planning_job_product_id_product"),
        "planning_job",
        "product",
        ["product_id"],
        ["id"],
    )
    op.drop_constraint(
        op.f("fk_planning_job_catalog_item_id_crop_protection_item"), "planning_job", "foreignkey"
    )
    op.drop_column("planning_job", "catalog_item_id")

    op.add_column("inventory_stock", sa.Column("product_id", sa.BigInteger(), nullable=True))
    op.alter_column(
        "inventory_stock", "catalog_item_id", existing_type=sa.BigInteger(), nullable=True
    )
    op.execute(
        "UPDATE inventory_stock s SET product_id = m.product_id, catalog_item_id = NULL "
        "FROM legacy_product_map m WHERE s.catalog_item_id = m.catalog_item_id"
    )
    op.create_foreign_key(
        op.f("fk_inventory_stock_product_id_product"),
        "inventory_stock",
        "product",
        ["product_id"],
        ["id"],
    )
    op.create_unique_constraint(
        op.f("uq_inventory_stock_farm_id_product_id_season"),
        "inventory_stock",
        ["farm_id", "product_id", "season"],
    )
    op.create_check_constraint(
        op.f("ck_inventory_stock_item"),
        "inventory_stock",
        "num_nonnulls(product_id, catalog_item_id) = 1",
    )

    op.execute(
        "DELETE FROM typed_quantity WHERE id IN (SELECT typed_quantity_id FROM legacy_quantity_map)"
    )
    op.execute(
        "CREATE TEMP TABLE core_0018_items ON COMMIT DROP AS "
        "SELECT catalog_item_id FROM legacy_product_map"
    )
    op.drop_table("legacy_quantity_map")
    op.drop_table("legacy_product_map")
    op.drop_table("advisor_product")
    op.execute("DELETE FROM catalog_item WHERE id IN (SELECT catalog_item_id FROM core_0018_items)")
    op.execute("DROP TABLE core_0018_items")
    for name in ("props", "active_substance", "authorisation_no"):
        op.drop_column("crop_protection_item", name)
    op.add_column("crop_protection_item", sa.Column("product_type", sa.Text(), nullable=False))
    op.add_column("crop_protection_item", sa.Column("targets", sa.ARRAY(sa.Text()), nullable=False))
    op.create_check_constraint(
        op.f("ck_crop_protection_item_product_type"),
        "crop_protection_item",
        _in("product_type", PRODUCT_TYPES),
    )
    op.create_check_constraint(
        op.f("ck_crop_protection_item_targets"),
        "crop_protection_item",
        "targets <@ ARRAY[" + ", ".join(repr(t) for t in TARGETS) + "]::text[]",
    )
    op.drop_constraint(op.f("ck_typed_quantity_entered_measure"), "typed_quantity", "check")
    op.drop_column("typed_quantity", "entered_measure")

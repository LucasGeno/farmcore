"""core: backfill irrigation amounts as entered quantities (one reader)

An irrigation's amount now has one reader, its entered `typed_quantity`. Capture has written
one since #92 (2026-11-18); a done irrigation confirmed before then holds its amount only in
`log.props["irrigation"]`. This revision gives each such log the row capture would have written:
the entered value and unit, the standard measure/value/unit from the frozen `unit_definition`
table (`mm` has none and stays null), no label and no catalog item.

Only props that validate as capture's fact are read (a positive finite decimal amount of at most
64 characters, unit `mm`, `m3` or `L`); others are left alone and still read as details
unavailable. A log that already has a typed quantity is skipped, so nothing is counted twice and
a re-run writes nothing. Pending logs are drafts being confirmed, never committed: skipped.

Downgrade changes nothing and loses nothing: core_0016 capture writes these same rows, so they
are valid there, and keeping them makes the next upgrade a no-op.

Revision ID: core_0017
Revises: core_0016
Create Date: 2026-11-19
"""

from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

import sqlalchemy as sa
from alembic import op

revision: str = "core_0017"
down_revision: str | None = "core_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNITS = ("mm", "m3", "L")  # capture's irrigation units (operations.facts, frozen here)
MAX_AMOUNT_CHARS = 64


def _amount(raw: object) -> Decimal | None:
    if not isinstance(raw, str) or not 1 <= len(raw) <= MAX_AMOUNT_CHARS:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    return value if value.is_finite() and value > 0 else None


def upgrade() -> None:
    bind = op.get_bind()
    standard = {
        row.entered: (row.measure, row.unit, row.factor)
        for row in bind.execute(
            sa.text("SELECT entered, measure, unit, factor FROM unit_definition")
        )
    }
    rows = []
    for log_id, fact in bind.execute(
        sa.text(
            "SELECT l.id, l.props -> 'irrigation' FROM log l "
            "WHERE l.type = 'irrigate' AND l.status = 'done' "
            "AND jsonb_typeof(l.props -> 'irrigation') = 'object' "
            "AND NOT EXISTS (SELECT 1 FROM typed_quantity t WHERE t.log_id = l.id) "
            "ORDER BY l.id"
        )
    ):
        value, unit = _amount(fact.get("amount")), fact.get("unit")
        if value is None or unit not in UNITS:
            continue
        measure, standard_unit, factor = standard.get(unit, (None, None, None))
        rows.append(
            {
                "log_id": log_id,
                "measure": measure,
                "value": value * factor if factor is not None else None,
                "unit": standard_unit,
                "entered_value": value,
                "entered_unit": unit,
            }
        )
    if rows:
        bind.execute(
            sa.text(
                "INSERT INTO typed_quantity "
                "(log_id, measure, value, unit, entered_value, entered_unit, provenance) "
                "VALUES (:log_id, :measure, :value, :unit, :entered_value, :entered_unit, "
                "'entered')"
            ),
            rows,
        )


def downgrade() -> None:
    pass  # the backfilled rows are valid at core_0016 (its capture writes them): nothing to undo

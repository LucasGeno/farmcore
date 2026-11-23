"""core: Procrastinate's queue schema from 3.10.0 to 3.12.0

Applies the library's own migration files for 3.11.0 and 3.12.0, in order, from vendored copies
(`vendor/procrastinate_3_12_0_migrations/`, unchanged, MIT like core_0009's schema) whose SHA-256
is checked first. Upstream sources, at their release tags:

- 3.11.0: https://github.com/procrastinate-org/procrastinate/blob/3.11.0/procrastinate/sql/migrations/03.11.00_01_pre_fetch_job_skip_null_lock.sql
- 3.12.0: https://github.com/procrastinate-org/procrastinate/tree/3.12.0/procrastinate/sql/migrations
  (03.12.00_01, _02, _03 pre; 03.12.00_50, _51 post)

Pre and post files run in one revision: `farmcore upgrade` stops the app, worker and bot first
(docs/self-hosting.md, "Upgrading"), so no 3.10 code is left to call what the post files drop.

Two upstream steps build or drop an index `CONCURRENTLY`, which PostgreSQL refuses inside a
transaction. `farmcore migrate` runs the whole chain in one transaction on the connection that
holds the migration lock, so a failed or stopped migrate rolls back as a whole; an alembic
`autocommit_block` would commit everything before it and break that. Those two statements run
without `CONCURRENTLY`: the queue holds only the installation's own pending and recent jobs, and
nothing writes to it while an upgrade runs, so the brief write lock is harmless.

Downgrade restores the 3.10.0 queue exactly (core_0009's vendored definitions), so core_0009's
own downgrade still finds what it drops. It refuses (a count only) while any job uses the 3.12
`mutex` lock mode, which 3.10 cannot represent; nothing is deleted to get past a refusal.

Revision ID: core_0023
Revises: core_0022
Create Date: 2026-11-23
"""

from collections.abc import Sequence
from hashlib import sha256
from pathlib import Path

from alembic import op

from farmcore.core.migration_refusal import MigrationRefused

revision: str = "core_0023"
down_revision: str | None = "core_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VENDOR = Path(__file__).resolve().parents[1] / "vendor"
MIGRATIONS = VENDOR / "procrastinate_3_12_0_migrations"
# Applied in this order, as Procrastinate's own `schema --migrate` would.
FILES = (
    (
        "03.11.00_01_pre_fetch_job_skip_null_lock.sql",
        "0d305a337820e3b2369b06e82df462094fdfbb417808fb982c9b91d0c42a0de6",
    ),
    (
        "03.12.00_01_pre_add_lock_mode.sql",
        "66d5117bfbf13c5d462b978cd1e58307a0c25265a067e10f154ddd6672674864",
    ),
    (
        "03.12.00_02_pre_retry_job_fail_on_queueing_lock_conflict.sql",
        "d85abd4a6c2b4bfbbbc55a19e15cbcf15fbb36d918412f67ec3740e53e090163",
    ),
    (
        "03.12.00_03_pre_add_todo_doing_lock_index.sql",
        "2c63597473aeaa1aa2f6a378a3c6b86d414d6c1d3ebe94bcb1009982bdeeb133",
    ),
    (
        "03.12.00_50_post_add_lock_mode.sql",
        "419b7181f707140a205f0df9afa5c1e3e20e0f276930d9dffa04a86ce6c61736",
    ),
    (
        "03.12.00_51_post_drop_id_lock_index.sql",
        "d4755202bd16ee87bbe2cb8cde828d8fe9b836deb2046c385837cd81f7d9d5dc",
    ),
)
# Not allowed in a transaction (see above); each appears once, in its own file.
IN_TRANSACTION = {
    "CREATE INDEX CONCURRENTLY ": "CREATE INDEX ",
    "DROP INDEX CONCURRENTLY ": "DROP INDEX ",
}
# The schema.sql of the library this revision brings the queue to: a lock-file bump that
# changes it needs a revision of its own (tests/core/test_schema.py).
LIBRARY_SCHEMA_SHA256 = "224a8d0fcaf5ff862aa7d9149169a1733687cf90a3b2494c630ae6d7a500e98b"

# core_0009's frozen 3.10.0 schema: the downgrade takes its definitions from it.
SCHEMA_3_10 = VENDOR / "procrastinate_3_10_0_schema.sql"
SCHEMA_3_10_SHA256 = "c70ec4b400a60ad9592787653aae5ae77ae41bf801d56b5bd2712751a07f2009"
ID_LOCK_INDEX = (
    "CREATE INDEX procrastinate_jobs_id_lock_idx_v1 ON procrastinate_jobs (id, lock) WHERE status "
    "= ANY (ARRAY['todo'::procrastinate_job_status, 'doing'::procrastinate_job_status]);"
)


def _read(path: Path, digest: str) -> str:
    body = path.read_bytes()
    if sha256(body).hexdigest() != digest:
        raise RuntimeError(f"{path.name} does not match the file this revision was written for")
    return body.decode()


def _execute(script: str) -> None:
    # The driver's own execute: several statements, `%` in RAISE messages, no bind parameters.
    op.get_bind().connection.driver_connection.execute(script)


def upgrade() -> None:
    for name, digest in FILES:
        script = _read(MIGRATIONS / name, digest)
        for concurrent, plain in IN_TRANSACTION.items():
            script = script.replace(concurrent, plain)
        _execute(script)


def _statement(schema: str, first_line: str) -> str:
    """One statement of the 3.10.0 schema, from its first line to its end: `$$;` for a
    function, `);` for a type, the line itself (an index) otherwise."""
    lines = schema.splitlines()
    start = lines.index(first_line)
    if first_line.startswith("CREATE FUNCTION"):
        stop = lines.index("$$;", start)
    elif first_line.startswith("CREATE TYPE"):
        stop = lines.index(");", start)
    else:
        stop = start
    return "\n".join(lines[start : stop + 1])


def downgrade() -> None:
    bind = op.get_bind()
    mutex = bind.exec_driver_sql(
        "SELECT count(*) FROM procrastinate_jobs WHERE lock_mode = 'mutex'"
    ).scalar_one()
    if mutex:
        raise MigrationRefused(
            f"core_0023 downgrade: {mutex} queued job(s) use the mutex lock mode, "
            "which Procrastinate 3.10 cannot represent"
        )
    schema = _read(SCHEMA_3_10, SCHEMA_3_10_SHA256)
    restored = [
        _statement(schema, "CREATE TYPE procrastinate_job_to_defer_v1 AS ("),
        _statement(schema, "CREATE FUNCTION procrastinate_defer_jobs_v1("),
        _statement(schema, "CREATE FUNCTION procrastinate_defer_periodic_job_v2("),
        # Replaced in place: other functions' plans and the library call them by name.
        *(
            _statement(schema, f"CREATE FUNCTION {name}(").replace(
                "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
            )
            for name in ("procrastinate_fetch_job_v2", "procrastinate_retry_job_v2")
        ),
        _statement(schema, ID_LOCK_INDEX),
    ]
    _execute("\n".join(restored))
    _execute(
        """
        DROP INDEX procrastinate_jobs_todo_doing_lock_idx_v1;
        DROP FUNCTION procrastinate_defer_periodic_job_v3(
            character varying, character varying, procrastinate_lock_mode, character varying,
            character varying, integer, character varying, bigint, jsonb
        );
        DROP FUNCTION procrastinate_defer_jobs_v2(procrastinate_job_to_defer_v2[]);
        DROP TYPE procrastinate_job_to_defer_v2;
        ALTER TABLE procrastinate_jobs DROP COLUMN lock_mode;
        DROP TYPE procrastinate_lock_mode;
        """
    )

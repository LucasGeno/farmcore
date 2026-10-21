"""Upgrade an installation: check compatibility, take a pre-upgrade bundle, migrate once, and say
honestly in `/health` which release and schema are in use.

Seams: the `farmcore` CLI (`upgrade`, `migrate`, `restore`), `GET /health` as the runtime role,
real `farmcore` processes (signals, concurrency) and, at the bottom, two real Compose projects.
A newer release is this checkout's migrations plus test-only revisions in a temporary copy
(never in the repository); the "old release" is this checkout as it is.

Failure model (what these tests are for):
- a release that migrates a database it does not understand (unknown or newer heads), or
  takes a backup or changes anything before refusing it;
- two migrators running at once (two upgrades, an upgrade and a compose `migrate`);
- a migration that fails or is stopped (SIGTERM) halfway and leaves a half-applied schema, a
  running statement on the server, a held lock, or an installation that says it is fine;
- an upgrade that migrates without a complete pre-upgrade bundle, or whose bundle does not
  restore what was there before;
- `/health` that hides a failed, running or incompatible upgrade, or says more than release,
  heads and state;
- a failed upgrade of A that disturbs B.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from importlib.metadata import version
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import farmcore.core.startup as startup
from farmcore.settings import Settings
from tests.instances import test_backup_restore as br
from tests.instances.test_backup_restore import (
    HEAD,
    SEMANTIC,
    Databases,
    decrypt,
    installation_id,
    members,
)

# The backup/restore fixtures, shared by name: the throwaway server's databases, the CLI, the
# owner's age key, installation A (seeded and replayed) and the pg_dump 17 tools on PATH.
pytestmark = br.pytestmark
caught_signals, cli, dbs, owner_key, source = (
    br.caught_signals,
    br.cli,
    br.dbs,
    br.owner_key,
    br.source,
)
pg_tools = br.pg_tools

RELEASE = version("farmcore")
NEXT_RELEASE = "99.0.0"  # what a newer image calls itself
ORIGINAL = startup.MIGRATIONS
GATE = 4242  # an advisory lock a test holds to stop a test revision mid-migration

REVISION = '''"""Test-only revision: what a newer release would ship."""

from alembic import op

revision = "{rev}"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade() -> None:
{body}


def downgrade() -> None:
    pass
'''
PROBE = (
    "    op.execute('CREATE TABLE upgrade_probe (n int)')\n"
    "    op.execute('INSERT INTO upgrade_probe VALUES (1)')"
)
FAILING = "    op.execute('CREATE TABLE upgrade_probe (n int)')\n    op.execute('SELECT 1/0')"
GATED = f"    op.execute('SELECT pg_advisory_xact_lock({GATE})')\n" + PROBE


def write_release(root: Path, *bodies: str) -> Path:
    """This checkout's migrations plus one revision per body after the current head."""
    shutil.copytree(ORIGINAL, root, ignore=shutil.ignore_patterns("__pycache__"))
    down = HEAD
    for i, body in enumerate(bodies, 1):
        rev = f"zz_test_{i:04d}"
        (root / "versions" / f"{rev}.py").write_text(REVISION.format(rev=rev, down=down, body=body))
        down = rev
    return root


@pytest.fixture
def release(tmp_path, monkeypatch):
    """`release(*bodies)` makes the code under test a newer release; `release()` with no body
    goes back to this checkout (reverting the image)."""

    def use(*bodies: str) -> Path:
        root = write_release(tmp_path / f"release-{uuid.uuid4().hex[:6]}", *bodies)
        monkeypatch.setattr(startup, "MIGRATIONS", root if bodies else ORIGINAL)
        return root

    return use


def health(url: str) -> tuple[int, dict]:
    from farmcore.main import create_app

    app = create_app(Settings(_env_file=None, database_url=url), manifests=[])
    with TestClient(app) as c:
        resp = c.get("/health")
    return resp.status_code, resp.json()


def heads(dbs: Databases, name: str) -> list[str]:
    return sorted(r for (r,) in dbs.query(name, "SELECT version_num FROM alembic_version"))


def settings_of(dbs: Databases, name: str) -> list[str]:
    rows = dbs.query(
        name,
        "SELECT unnest(setconfig) FROM pg_db_role_setting s JOIN pg_database d "
        "ON d.oid = s.setdatabase WHERE d.datname = current_database() AND s.setrole = 0",
    )
    return sorted(r for (r,) in rows)


def tables(dbs: Databases, name: str) -> list[str]:
    rows = dbs.query(
        name,
        "SELECT table_name || '.' || column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' ORDER BY 1",
    )
    return [r for (r,) in rows]


def semantic(dbs: Databases, name: str) -> dict[str, list[tuple]]:
    return {label: dbs.query(name, sql) for label, sql in SEMANTIC.items()}


def migration_lock_held(dbs: Databases, name: str) -> bool:
    key1, key2 = startup.MIGRATION_LOCK
    return bool(
        dbs.query(
            name,
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND granted "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
            "AND classid = :k1 AND objid = :k2 AND objsubid = 2",
            k1=key1,
            k2=key2,
        )[0][0]
    )


def waiting_on_gate(dbs: Databases, name: str) -> int:
    return dbs.query(
        name,
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
        "AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) "
        "AND objid = :g AND objsubid = 1",
        g=GATE,
    )[0][0]


def wait_for(predicate, what: str, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.1)


class Holder:
    """An advisory lock held by this test on its own connection to `name`, until released."""

    def __init__(self, dbs: Databases, name: str, *key: int) -> None:
        self.engine = create_engine(dbs.server.url(name), isolation_level="AUTOCOMMIT")
        self.conn = self.engine.connect()
        args = ", ".join(str(k) for k in key)
        self.conn.exec_driver_sql(f"SELECT pg_advisory_lock({args})")

    def release(self) -> None:
        self.conn.close()
        self.engine.dispose()


def upgrade(cli, env: dict[str, str], recipient: str, backups: Path, *extra: str):
    return cli(
        "upgrade", "--backup-dir", str(backups), "--recipient", recipient, *extra,
        env={k: v for k, v in env.items() if k != "name"},
    )  # fmt: skip


def bundles(backups: Path) -> list[Path]:
    return sorted(backups.glob("pre-upgrade-*.age"))


@pytest.fixture
def backups(tmp_path) -> Path:
    path = tmp_path / "backups"
    path.mkdir()
    return path


def restore_into_new(dbs: Databases, cli, bundle: Path, key: Path) -> tuple[str, dict[str, str]]:
    """The documented recovery: the pre-upgrade bundle into an empty installation database."""
    target = dbs.create()
    env = dbs.installation(target)
    result = cli("restore", str(bundle), "--identity", str(key), env=env)
    assert result.exit_code == 0, result.output
    return target, env


# --- /health ------------------------------------------------------------------------------------


def test_health_states_the_release_and_schema_in_use_and_nothing_more(source, dbs):
    status, body = health(dbs.runtime_url(source))

    assert status == 200
    assert body == expected(dbs, source["name"], "ok", [HEAD])
    assert uuid.UUID(body["installation_id"]).version == 4


def test_health_says_maintenance_while_a_migration_runs_or_is_pending(source, dbs, release):
    runtime = dbs.runtime_url(source)
    holder = Holder(dbs, source["name"], *startup.MIGRATION_LOCK)
    try:
        assert health(runtime) == (503, expected(dbs, source["name"], "maintenance", [HEAD]))
    finally:
        holder.release()
    assert health(runtime)[0] == 200

    release(PROBE)  # this release expects a revision the database does not have yet
    assert health(runtime) == (503, expected(dbs, source["name"], "maintenance", [HEAD]))


def expected(dbs: Databases, name: str, state: str, alembic_heads: list[str]) -> dict:
    return {
        "db": "ok",
        "installation_id": installation_id(dbs, name),
        "release": RELEASE,
        "alembic_heads": alembic_heads,
        "state": state,
    }


# --- farmcore upgrade ---------------------------------------------------------------------------


def test_upgrade_needs_a_backup_dir_and_a_migrated_database(
    pg_server, fresh_db, cli, owner_key, backups
):
    _, recipient = owner_key
    env = {"DATABASE_URL": pg_server.url(fresh_db)}

    no_dir = cli("upgrade", "--recipient", recipient, env=env)
    new = cli("upgrade", "--backup-dir", str(backups), "--recipient", recipient, env=env)

    assert no_dir.exit_code == 1 and "give --backup-dir" in no_dir.output
    assert new.exit_code == 1, new.output
    assert "never migrated" in new.output and "farmcore migrate" in new.output
    assert bundles(backups) == []
    assert "alembic_version" not in str(
        create_engine(pg_server.url(fresh_db))
        .connect()
        .exec_driver_sql("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        .all()
    )


def test_upgrade_backs_up_first_then_migrates_once_and_reports_health(
    source, dbs, cli, owner_key, release, backups
):
    key, recipient = owner_key
    before = semantic(dbs, source["name"])
    installation = health(dbs.runtime_url(source))[1]["installation_id"]
    release(PROBE)

    checked = cli("upgrade", "--check", env={"DATABASE_URL": source["DATABASE_URL"]})
    assert checked.exit_code == 0, checked.output
    assert f"alembic heads {HEAD}" in checked.output
    assert "zz_test_0001" in checked.output and "pending" in checked.output
    assert bundles(backups) == [] and heads(dbs, source["name"]) == [HEAD]

    result = upgrade(cli, source, recipient, backups)

    assert result.exit_code == 0, result.output
    [bundle] = bundles(backups)
    assert f"pre-upgrade backup {bundle}" in result.output
    assert f"release {RELEASE}, alembic heads zz_test_0001, state ok" in result.output
    manifest = json.loads(members(decrypt(bundle, key))["manifest.json"])
    assert manifest["alembic_heads"] == [HEAD]  # taken before the migration
    assert manifest["installation_id"] == installation
    assert heads(dbs, source["name"]) == ["zz_test_0001"]
    assert dbs.query(source["name"], "SELECT n FROM upgrade_probe") == [(1,)]
    assert semantic(dbs, source["name"]) == before
    assert health(dbs.runtime_url(source)) == (
        200,
        expected(dbs, source["name"], "ok", ["zz_test_0001"]),
    )
    assert installation_id(dbs, source["name"]) == installation

    time.sleep(1.1)  # bundle names have one-second resolution
    again = upgrade(cli, source, recipient, backups)  # and compose's `migrate` after it

    assert again.exit_code == 0, again.output
    assert "already at zz_test_0001" in again.output
    assert cli("migrate", env=source_env(source)).exit_code == 0
    assert dbs.query(source["name"], "SELECT n FROM upgrade_probe") == [(1,)]
    assert len(bundles(backups)) == 2


def source_env(source: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in source.items() if k != "name"}


@pytest.mark.parametrize(
    "stored", [["zz_future_0001"], [HEAD, "zz_future_0001"]], ids=["newer", "unknown-beside-ours"]
)
def test_an_incompatible_database_is_refused_before_any_backup_or_migration(
    source, dbs, cli, owner_key, backups, stored
):
    """A newer release migrated this database (or a fork did): this release must not touch it."""
    _, recipient = owner_key
    dbs.query(source["name"], "DELETE FROM alembic_version RETURNING 1")
    for rev in stored:
        dbs.query(source["name"], "INSERT INTO alembic_version VALUES (:r) RETURNING 1", r=rev)
    schema, settings = tables(dbs, source["name"]), settings_of(dbs, source["name"])

    for args in (
        ("upgrade", "--backup-dir", str(backups), "--recipient", recipient),
        ("upgrade", "--check"),
        ("migrate",),
    ):
        result = cli(*args, env=source_env(source))

        assert result.exit_code == 1, result.output
        assert "incompatible" in result.output and "zz_future_0001" in result.output
        assert "Traceback" not in result.output
        assert heads(dbs, source["name"]) == sorted(stored)
        assert tables(dbs, source["name"]) == schema
        assert settings_of(dbs, source["name"]) == settings  # not even marked failed
        assert bundles(backups) == []
        assert not migration_lock_held(dbs, source["name"])

    status, body = health(dbs.runtime_url(source))
    assert status == 503
    assert body["state"] == "failed" and body["alembic_heads"] == sorted(stored)


@pytest.mark.parametrize("fault", ["no recipient", "unwritable directory", "existing bundle"])
def test_a_failed_preflight_changes_nothing(
    source, dbs, cli, owner_key, release, backups, monkeypatch, fault
):
    _, recipient = owner_key
    release(PROBE)
    schema, settings = tables(dbs, source["name"]), settings_of(dbs, source["name"])
    args = ["upgrade", "--backup-dir", str(backups), "--recipient", recipient]
    if fault == "no recipient":
        args = args[:3]
        monkeypatch.delenv("BACKUP_RECIPIENT", raising=False)
    elif fault == "unwritable directory":
        args[2] = str(backups / "missing")
    else:  # the clock gives the bundle a name that is taken
        import farmcore.core.commands.upgrade as module

        monkeypatch.setattr(module, "_stamp", lambda: "20261021T000000Z")
        (backups / "pre-upgrade-20261021T000000Z.age").write_bytes(b"older")

    result = cli(*args, env=source_env(source))

    assert result.exit_code == 1, result.output
    assert "nothing was migrated" in result.output
    assert heads(dbs, source["name"]) == [HEAD]
    assert tables(dbs, source["name"]) == schema
    assert settings_of(dbs, source["name"]) == settings
    assert [p.name for p in bundles(backups)] == (
        ["pre-upgrade-20261021T000000Z.age"] if fault == "existing bundle" else []
    )
    assert not migration_lock_held(dbs, source["name"])
    release()
    assert health(dbs.runtime_url(source))[0] == 200


def test_a_failed_migration_rolls_back_says_failed_and_recovers_from_the_pre_upgrade_bundle(
    source, dbs, cli, owner_key, release, backups
):
    key, recipient = owner_key
    other = dbs.create()  # installation B, on the same server
    b_env = dbs.installation(other)
    assert cli("migrate", env=b_env).exit_code == 0
    before = semantic(dbs, source["name"])
    schema = tables(dbs, source["name"])
    name = source["name"]
    release(FAILING)

    result = upgrade(cli, source, recipient, backups)

    assert result.exit_code == 1, result.output
    [bundle] = bundles(backups)
    assert "migration failed: division by zero" in result.output
    assert f"the schema is still at {HEAD}" in result.output
    assert str(bundle) in result.output and "restore" in result.output
    assert "SELECT" not in result.output and "Traceback" not in result.output
    assert heads(dbs, source["name"]) == [HEAD]
    assert tables(dbs, source["name"]) == schema  # upgrade_probe rolled back with it
    assert not migration_lock_held(dbs, source["name"])
    release()  # the previous image again; B was migrated with it
    assert health(dbs.runtime_url(source)) == (503, expected(dbs, name, "failed", [HEAD]))
    assert health(dbs.runtime_url(b_env))[0] == 200  # B keeps serving

    # Recovery: the pre-upgrade bundle restores into a new installation, as it was.
    target, env = restore_into_new(dbs, cli, bundle, key)
    assert semantic(dbs, target) == before
    assert health(dbs.runtime_url(env))[1]["state"] == "ok"
    # Or, as the schema never changed, back on the previous release, `migrate` clears it.
    assert cli("migrate", env=source_env(source)).exit_code == 0
    assert health(dbs.runtime_url(source))[1]["state"] == "ok"
    assert health(dbs.runtime_url(b_env))[0] == 200


def test_after_a_failed_post_start_check_reverting_the_release_is_refused_and_restore_recovers(
    source, dbs, cli, owner_key, release, backups
):
    """The schema migrated, then the new release does not serve. Going back to the previous
    release does not take the schema back: that release refuses the database, and the
    pre-upgrade bundle is the way back."""
    key, recipient = owner_key
    before = semantic(dbs, source["name"])
    release(PROBE)
    import farmcore.core.commands.backup as backup_module

    with pytest.MonkeyPatch.context() as newer:  # a real release bump: the new version number
        newer.setattr(backup_module, "version", lambda _: NEXT_RELEASE)
        assert upgrade(cli, source, recipient, backups).exit_code == 0
    [bundle] = bundles(backups)
    assert json.loads(members(decrypt(bundle, key))["manifest.json"])["release"] == NEXT_RELEASE

    release()  # the previous image again
    reverted = cli("migrate", env=source_env(source))

    assert reverted.exit_code == 1
    assert "incompatible" in reverted.output and "zz_test_0001" in reverted.output
    assert health(dbs.runtime_url(source))[0] == 503
    assert heads(dbs, source["name"]) == ["zz_test_0001"]

    target, env = restore_into_new(dbs, cli, bundle, key)
    assert semantic(dbs, target) == before
    assert heads(dbs, target) == [HEAD]
    assert health(dbs.runtime_url(env)) == (200, expected(dbs, target, "ok", [HEAD]))


def test_a_second_upgrade_while_one_runs_exits_cleanly_and_touches_nothing(
    source, dbs, cli, owner_key, release, backups
):
    _, recipient = owner_key
    release(PROBE)
    holder = Holder(dbs, source["name"], *startup.MIGRATION_LOCK)  # another migrator
    try:
        result = upgrade(cli, source, recipient, backups)
    finally:
        holder.release()

    assert result.exit_code == 1, result.output
    assert "another upgrade or migrate is running" in result.output
    assert "nothing was done" in result.output
    assert bundles(backups) == [] and heads(dbs, source["name"]) == [HEAD]


def test_one_installations_migration_lock_never_holds_up_another(source, dbs, cli):
    """The lock is per installation: B migrates while A's migrator is busy (or stuck)."""
    other = dbs.create()
    env = dbs.installation(other)
    holder = Holder(dbs, source["name"], *startup.MIGRATION_LOCK)
    try:
        result = cli("migrate", env=env)
        a_state = health(dbs.runtime_url(source))[1]["state"]
    finally:
        holder.release()

    assert result.exit_code == 0, result.output
    assert health(dbs.runtime_url(env))[1]["state"] == "ok"
    assert a_state == "maintenance"


def test_a_busy_server_list_stops_the_upgrade_after_migrating_and_migrate_finishes_it(
    source, dbs, cli, owner_key, release, backups
):
    """Another installation's restore or migrate holds `pg_database` past the lock timeout:
    the schema step is done, the runtime role step is not. The installation says failed, and
    says how to finish; the failed mark itself never waits for that lock."""
    _, recipient = owner_key
    release(PROBE)
    engine = create_engine(dbs.server.url("postgres"))
    conn = engine.connect()
    conn.exec_driver_sql("LOCK TABLE pg_catalog.pg_database IN SHARE ROW EXCLUSIVE MODE")
    # Never hang the run: give the lock back after 20 s whatever happens.
    timer = threading.Timer(20, conn.rollback)
    timer.start()
    try:
        started = time.monotonic()
        result = upgrade(cli, source, recipient, backups)
        took = time.monotonic() - started
    finally:
        timer.cancel()
        conn.rollback()
        conn.close()
        engine.dispose()

    assert result.exit_code == 1, result.output
    assert "the server is busy" in result.output, result.output
    assert "after migrating to zz_test_0001" in result.output
    assert "farmcore migrate" in result.output
    assert took < 15  # the 5 s timeout, not the safety timer
    assert heads(dbs, source["name"]) == ["zz_test_0001"]
    assert health(dbs.runtime_url(source))[1]["state"] == "failed"

    assert cli("migrate", env=source_env(source)).exit_code == 0
    assert health(dbs.runtime_url(source)) == (
        200,
        expected(dbs, source["name"], "ok", ["zz_test_0001"]),
    )


def test_sigterm_mid_migration_cancels_it_and_leaves_the_installation_failed_not_half_done(
    source, dbs, cli, owner_key, release, backups, caught_signals
):
    """`docker compose stop`, a closed terminal: the running statement is cancelled on the
    server, the transaction rolls back, the lock goes, and the installation says failed."""
    key, recipient = owner_key
    release(GATED)
    gate = Holder(dbs, source["name"], GATE)
    handler = signal.getsignal(signal.SIGTERM)

    def stop() -> None:
        wait_for(lambda: waiting_on_gate(dbs, source["name"]) == 1, "the migration to block")
        os.kill(os.getpid(), signal.SIGTERM)

    killer = threading.Thread(target=stop)
    killer.start()
    try:
        result = upgrade(cli, source, recipient, backups)
        # Read while the gate is still held: a statement merely abandoned by the client would
        # still be waiting on the server (and would carry on once the gate opens).
        stuck = waiting_on_gate(dbs, source["name"])
        held = migration_lock_held(dbs, source["name"])
    finally:
        killer.join()
        gate.release()

    assert result.exit_code == 1, result.output
    assert "stopped by SIGTERM" in result.output
    assert stuck == 0 and not held  # cancelled on the server, lock gone
    assert heads(dbs, source["name"]) == [HEAD]
    assert "upgrade_probe" not in str(tables(dbs, source["name"]))
    assert health(dbs.runtime_url(source))[1]["state"] == "failed"
    assert signal.getsignal(signal.SIGTERM) == handler and caught_signals == []
    [bundle] = bundles(backups)  # complete, and taken before the migration
    assert json.loads(members(decrypt(bundle, key))["manifest.json"])["alembic_heads"] == [HEAD]
    time.sleep(1.1)  # bundle names have one-second resolution

    rerun = upgrade(cli, source, recipient, backups)
    assert rerun.exit_code == 0, rerun.output
    assert health(dbs.runtime_url(source))[1]["state"] == "ok"


# --- Real processes -----------------------------------------------------------------------------

LAUNCH = (
    "import sys; from pathlib import Path; import farmcore.core.startup as s; "
    "s.MIGRATIONS = Path(sys.argv[1]); from farmcore.cli import cli; "
    "cli(sys.argv[2:], prog_name='farmcore')"
)


def farmcore(root: Path, env: dict[str, str], *args: str) -> subprocess.Popen:
    """A real `farmcore` process running the release at `root`."""
    return subprocess.Popen(
        [sys.executable, "-c", LAUNCH, str(root), *args],
        env={**os.environ, **source_env(env)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_concurrent_migrators_run_the_migration_once(source, dbs, owner_key, tmp_path, backups):
    """Two upgrades and a compose `migrate` at once: one migrates, the other upgrade exits
    cleanly, `migrate` waits and then finds nothing to do."""
    _, recipient = owner_key
    root = write_release(tmp_path / "next", GATED)
    gate = Holder(dbs, source["name"], GATE)
    up = ("upgrade", "--backup-dir", str(backups), "--recipient", recipient)
    try:
        first = farmcore(root, source, *up)
        wait_for(lambda: waiting_on_gate(dbs, source["name"]) == 1, "the first upgrade")
        second = farmcore(root, source, *up)
        migrate = farmcore(root, source, "migrate")
        out, _ = second.communicate(timeout=60)
        assert second.returncode == 1, out
        assert "another upgrade or migrate is running" in out
        wait_for(lambda: lock_waiters(dbs, source["name"]) == 1, "migrate to wait for the lock")
        status, body = health(dbs.runtime_url(source))
        assert (status, body["state"]) == (503, "maintenance")
    finally:
        gate.release()
    out, _ = first.communicate(timeout=120)
    assert first.returncode == 0, out
    out, _ = migrate.communicate(timeout=120)
    assert migrate.returncode == 0, out
    assert dbs.query(source["name"], "SELECT n FROM upgrade_probe") == [(1,)]
    assert heads(dbs, source["name"]) == ["zz_test_0001"]
    assert len(bundles(backups)) == 1


def lock_waiters(dbs: Databases, name: str) -> int:
    key1, key2 = startup.MIGRATION_LOCK
    return dbs.query(
        name,
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
        "AND classid = :k1 AND objid = :k2 AND objsubid = 2",
        k1=key1,
        k2=key2,
    )[0][0]


def test_a_real_upgrade_stopped_by_sigterm_says_so_and_a_rerun_finishes(
    source, dbs, owner_key, tmp_path, backups
):
    _, recipient = owner_key
    root = write_release(tmp_path / "next", GATED)
    gate = Holder(dbs, source["name"], GATE)
    up = ("upgrade", "--backup-dir", str(backups), "--recipient", recipient)
    try:
        proc = farmcore(root, source, *up)
        wait_for(lambda: waiting_on_gate(dbs, source["name"]) == 1, "the migration to block")
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
        stuck = waiting_on_gate(dbs, source["name"])  # before the gate opens
    finally:
        gate.release()

    assert proc.returncode == 1, out
    assert "stopped by SIGTERM" in out and "Traceback" not in out
    assert stuck == 0
    assert heads(dbs, source["name"]) == [HEAD]
    rerun = farmcore(root, source, *up)
    out, _ = rerun.communicate(timeout=120)
    assert rerun.returncode == 0, out
    assert heads(dbs, source["name"]) == ["zz_test_0001"]


def test_a_migrate_stopped_while_waiting_for_the_lock_leaves_no_mark(
    source, dbs, tmp_path, caught_signals, cli
):
    holder = Holder(dbs, source["name"], *startup.MIGRATION_LOCK)
    settings = settings_of(dbs, source["name"])

    def stop() -> None:
        wait_for(lambda: lock_waiters(dbs, source["name"]) == 1, "migrate to wait")
        os.kill(os.getpid(), signal.SIGTERM)

    killer = threading.Thread(target=stop)
    killer.start()
    try:
        result = cli("migrate", env=source_env(source))
    finally:
        killer.join()
        holder.release()

    assert result.exit_code == 1
    assert "stopped by SIGTERM" in result.output
    assert settings_of(dbs, source["name"]) == settings
    assert caught_signals == []


# --- Docs ---------------------------------------------------------------------------------------


def test_the_upgrade_runbook_says_reverting_the_image_does_not_revert_the_schema():
    from tests.instances.conftest import REPO

    docs = (REPO / "docs" / "self-hosting.md").read_text()
    section = docs.split("\n## Upgrading", 1)[1].split("\n## ", 1)[0]
    assert "Reverting the image does not revert the schema" in section
    for step in (
        "farmcore upgrade --check",
        "farmcore upgrade --backup-dir /backups",
        "up -d --wait",
        "farmcore restore",
        "TEMPLATE template_postgis",
    ):
        assert step in section, step


# --- Two real Compose projects ------------------------------------------------------------------


def _image_with(base: str, tag: str, context: Path, revision: str, cmd: str | None) -> str:
    """A newer release: `base` plus a revision file (and, optionally, an app that will not
    start), built from a throwaway context."""
    package = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", base, "-c",
         "import farmcore, os; print(os.path.dirname(farmcore.__file__))"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    context.mkdir()
    (context / "zz_test_0001.py").write_text(
        REVISION.format(rev="zz_test_0001", down=HEAD, body=revision)
    )
    lines = [f"FROM {base}", f"COPY zz_test_0001.py {package}/migrations/versions/"]
    if cmd:
        lines.append(f"CMD {cmd}")
    (context / "Dockerfile").write_text("\n".join(lines) + "\n")
    subprocess.run(
        ["docker", "build", "-q", "-t", tag, str(context)], check=True, capture_output=True
    )
    return tag


def _psql(project, sql: str, database: str = "farmcore") -> list[str]:
    out = project.compose("exec", "-T", "db", "psql", "-U", "farmcore", "-d", database,
                          "-v", "ON_ERROR_STOP=1", "-Atc", sql).stdout  # fmt: skip
    return out.splitlines()


def _set_image(project, image: str) -> None:
    project.env["FARMCORE_IMAGE"] = image
    project.env_file.write_text("".join(f"{k}={v}\n" for k, v in project.env.items()))


@pytest.mark.slow
def test_a_failed_upgrade_of_project_a_recovers_by_restore_while_b_keeps_serving(
    new_project, image, owner_key, tmp_path
):
    key, recipient = owner_key
    a, b = new_project(BACKUP_RECIPIENT=recipient), new_project()
    a.up()
    b.up()
    for args in (("seed", "--scene", "B"), ("replay", "--to", "2024-04-10 06:00")):
        a.compose("exec", "-T", "app", "farmcore", *args)
    before = {label: _psql(a, sql) for label, sql in SEMANTIC.items()}
    assert len(before["records"]) == 10
    suffix = uuid.uuid4().hex[:8]
    broken_migration = _image_with(
        image, f"farmcore-iso-test-bad:{suffix}", tmp_path / "bad", FAILING, None
    )
    broken_app = _image_with(
        image, f"farmcore-iso-test-dead:{suffix}", tmp_path / "dead", PROBE,
        '["python", "-c", "raise SystemExit(\'this release cannot start\')"]',
    )  # fmt: skip
    b_id = b.health()["installation_id"]
    failures: list[str] = []
    done = threading.Event()

    def probe() -> None:
        while not done.is_set():
            try:
                body = b.health()
                assert body["installation_id"] == b_id and body["state"] == "ok"
            except Exception as exc:  # noqa: BLE001 - any failure to serve counts
                failures.append(type(exc).__name__)
            time.sleep(0.5)

    upgrade_args = ("run", "--rm", "--no-deps", "migrate", "farmcore", "upgrade")
    watcher = threading.Thread(target=probe)
    watcher.start()
    try:
        # 1. Preflight failure: no recipient for the pre-upgrade backup. Nothing changes.
        _set_image(a, broken_migration)
        checked = a.compose(*upgrade_args, "--check", check=False)
        assert checked.returncode == 0, checked.stdout + checked.stderr
        assert "pending" in checked.stdout
        a.compose("stop", "app")
        refused = a.compose(
            "run", "--rm", "--no-deps", "-e", "BACKUP_RECIPIENT=", "migrate",
            "farmcore", "upgrade", "--backup-dir", "/backups", check=False,
        )  # fmt: skip
        assert refused.returncode == 1 and "nothing was migrated" in refused.stderr

        # 2. Migration failure: rolled back, A says failed; the previous image recovers it.
        failed = a.compose(*upgrade_args, "--backup-dir", "/backups", check=False)
        assert failed.returncode == 1
        assert "migration failed: division by zero" in failed.stderr, failed.stderr
        assert f"the schema is still at {HEAD}" in failed.stderr
        assert _psql(a, "SELECT version_num FROM alembic_version") == [HEAD]
        _set_image(a, image)
        a.compose("up", "-d", "--wait", "--wait-timeout", "180")
        assert a.health()["state"] == "ok"

        # 3. Post-start failure: the schema migrates, then the new release does not serve.
        _set_image(a, broken_app)
        a.compose("stop", "app")
        migrated = a.compose(*upgrade_args, "--backup-dir", "/backups")
        assert "alembic heads zz_test_0001, state ok" in migrated.stdout
        started = a.compose("up", "-d", "--wait", "--wait-timeout", "60", check=False)
        assert started.returncode != 0
        # Reverting the image does not revert the schema: the previous release refuses it.
        _set_image(a, image)
        reverted = a.compose("up", "-d", "--wait", "--wait-timeout", "120", check=False)
        assert reverted.returncode != 0
        logs = a.compose("logs", "--no-color", "migrate").stdout
        assert "incompatible" in logs and "zz_test_0001" in logs

        # The documented recovery: keep the failed database aside, restore the bundle.
        listed = a.compose("run", "--rm", "--no-deps", "--entrypoint", "ls", "backup", "/backups")
        pre = sorted(n for n in listed.stdout.split() if n.startswith("pre-upgrade-"))
        assert len(pre) == 2, pre  # one per upgrade that ran; the refused preflight wrote none
        restore_dir = tmp_path / "restore"
        restore_dir.mkdir()
        a.compose("cp", f"backup:/backups/{pre[-1]}", str(restore_dir / "bundle.age"))
        shutil.copy(key, restore_dir / "owner.key")
        for path in restore_dir.iterdir():
            path.chmod(0o600)
        a.compose("stop", "app", "backup")
        _psql(a, "ALTER DATABASE farmcore RENAME TO farmcore_failed_upgrade", "postgres")
        _psql(a, "CREATE DATABASE farmcore TEMPLATE template_postgis", "postgres")
        restored = a.compose(
            "run", "--rm", "--no-deps", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{restore_dir}:/restore:ro", "migrate",
            "farmcore", "restore", "/restore/bundle.age", "--identity", "/restore/owner.key",
            check=False,
        )  # fmt: skip
        assert restored.returncode == 0, restored.stdout + restored.stderr
        a.compose("up", "-d", "--wait", "--wait-timeout", "180")
    finally:
        done.set()
        watcher.join()
        for tag in (broken_migration, broken_app):
            subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)
    assert failures == []

    body = a.health()
    assert (body["state"], body["alembic_heads"], body["release"]) == ("ok", [HEAD], RELEASE)
    for label, sql in SEMANTIC.items():
        assert _psql(a, sql) == before[label], label
    assert _psql(a, "SELECT count(*) FROM pg_database WHERE datname = 'farmcore_failed_upgrade'")
    effects = a.compose("run", "--rm", "--no-deps", "migrate", "farmcore", "effects")
    assert effects.stdout.strip() == "external effects: paused"
    assert b.health()["installation_id"] == b_id

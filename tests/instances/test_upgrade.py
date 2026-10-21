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
import stat
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
from tests.instances.support import (
    HEAD,
    OUTCOMES,
    SEMANTIC,
    Databases,
    decrypt,
    installation_from,
    installation_id,
    members,
    owner_env,
    refused,
    restore,
    run_cli,
    says,
    wait_for,
)

pytestmark = pytest.mark.usefixtures("pg_tools")

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
    goes back to this checkout (reverting the image). Its migrations' directory."""

    def use(*bodies: str) -> Path:
        root = write_release(tmp_path / f"release-{uuid.uuid4().hex[:6]}", *bodies)
        monkeypatch.setattr(startup, "MIGRATIONS", root if bodies else ORIGINAL)
        return root if bodies else ORIGINAL

    return use


@pytest.fixture
def installation(dbs) -> dict[str, str]:
    """A migrated installation without data, for checks that need no records."""
    return installation_from(dbs, dbs.templates.migrated)


def health(url: str) -> tuple[int, dict]:
    from farmcore.main import create_app

    app = create_app(Settings(_env_file=None, database_url=url), manifests=[])
    with TestClient(app) as c:
        resp = c.get("/health")
    return resp.status_code, resp.json()


def state(dbs: Databases, installation: dict[str, str]) -> str:
    return health(dbs.runtime_url(installation))[1]["state"]


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


def migration_lock_waiters(dbs: Databases, name: str) -> int:
    key1, key2 = startup.MIGRATION_LOCK
    return dbs.query(
        name,
        "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
        "AND classid = :k1 AND objid = :k2 AND objsubid = 2",
        k1=key1,
        k2=key2,
    )[0][0]


def advisory_waiters(dbs: Databases, name: str) -> int:
    return dbs.query(
        name,
        "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
        "AND wait_event_type = 'Lock' AND wait_event = 'advisory'",
    )[0][0]


# The advisory lock `ensure_runtime_role` takes before setting up the runtime role.
RUNTIME_LOCK = "hashtext('farmcore.runtime')"


class Holder:
    """An advisory lock held by this test on its own connection to `name`, until released."""

    def __init__(self, dbs: Databases, name: str, *key) -> None:
        self.engine = create_engine(dbs.server.url(name), isolation_level="AUTOCOMMIT")
        self.conn = self.engine.connect()
        args = ", ".join(str(k) for k in key)
        self.conn.exec_driver_sql(f"SELECT pg_advisory_lock({args})")

    def release(self) -> None:
        self.conn.close()
        self.engine.dispose()


def upgrade(env: dict[str, str], recipient: str, backups: Path, *extra: str):
    return run_cli(
        "upgrade", "--backup-dir", str(backups), "--recipient", recipient, *extra,
        env=owner_env(env),
    )  # fmt: skip


def bundles(backups: Path) -> list[Path]:
    return sorted(backups.glob("pre-upgrade-*.age"))


@pytest.fixture
def backups(tmp_path) -> Path:
    path = tmp_path / "backups"
    path.mkdir()
    return path


def restore_into_new(dbs: Databases, bundle: Path, key: Path) -> tuple[str, dict[str, str]]:
    """The documented recovery: the pre-upgrade bundle into an empty installation database."""
    target = dbs.create()
    env = dbs.installation(target)
    result = restore(bundle, key, env)
    assert result.exit_code == 0, result.output
    return target, env


def expected(dbs: Databases, name: str, state: str, alembic_heads: list[str]) -> dict:
    return {
        "db": "ok",
        "installation_id": installation_id(dbs, name),
        "release": RELEASE,
        "alembic_heads": alembic_heads,
        "state": state,
    }


# --- B5: /health --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "status", "says_state"),
    [("ok", 200, "ok"), ("migration running", 503, "maintenance"),
     ("revision pending", 503, "maintenance")],
)  # fmt: skip
def test_health_states_the_release_schema_and_state_and_nothing_more(
    shared_source, module_dbs, release, case, status, says_state
):
    holder = None
    if case == "migration running":
        holder = Holder(module_dbs, shared_source["name"], *startup.MIGRATION_LOCK)
    elif case == "revision pending":
        release(PROBE)  # this release expects a revision the database does not have yet
    try:
        found = health(module_dbs.runtime_url(shared_source))
    finally:
        if holder:
            holder.release()

    assert found == (status, expected(module_dbs, shared_source["name"], says_state, [HEAD]))
    assert uuid.UUID(found[1]["installation_id"]).version == 4


# --- K: farmcore upgrade --------------------------------------------------------------------------


def test_upgrade_backs_up_first_then_migrates_once_and_reports_health(
    source, dbs, owner_key, release, backups, tmp_path
):
    key, recipient = owner_key
    before = semantic(dbs, source["name"])
    installation = health(dbs.runtime_url(source))[1]["installation_id"]
    release(PROBE)

    checked = run_cli("upgrade", "--check", env={"DATABASE_URL": source["DATABASE_URL"]})
    assert checked.exit_code == 0, checked.output
    assert HEAD in checked.output and "zz_test_0001" in checked.output
    assert "pending" in checked.output
    assert bundles(backups) == [] and heads(dbs, source["name"]) == [HEAD]

    result = upgrade(source, recipient, backups)

    assert result.exit_code == 0, result.output
    [bundle] = bundles(backups)
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o600  # like the daily bundles
    assert str(bundle) in result.output
    assert "zz_test_0001" in result.output and "state ok" in result.output
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

    again_dir = tmp_path / "again"
    again_dir.mkdir()
    again = upgrade(source, recipient, again_dir)  # and compose's `migrate` after it

    assert again.exit_code == 0, again.output
    assert "already at zz_test_0001" in again.output
    assert run_cli("migrate", env=owner_env(source)).exit_code == 0
    assert dbs.query(source["name"], "SELECT n FROM upgrade_probe") == [(1,)]
    assert len(bundles(again_dir)) == 1


def test_an_incompatible_database_is_refused_before_any_backup_or_migration(
    installation, dbs, owner_key, backups
):
    """A newer release migrated this database: this release must not touch it."""
    _, recipient = owner_key
    name = installation["name"]
    dbs.query(name, "UPDATE alembic_version SET version_num = 'zz_future_0001' RETURNING 1")
    schema, settings = tables(dbs, name), settings_of(dbs, name)

    for args in (
        ("upgrade", "--backup-dir", str(backups), "--recipient", recipient),
        ("upgrade", "--check"),
        ("migrate",),
    ):
        result = run_cli(*args, env=owner_env(installation))

        refused(result, "incompatible")
        assert "zz_future_0001" in result.output and "Traceback" not in result.output
        assert heads(dbs, name) == ["zz_future_0001"]
        assert tables(dbs, name) == schema
        assert settings_of(dbs, name) == settings  # not even marked failed
        assert bundles(backups) == []
        assert not migration_lock_held(dbs, name)

    status, body = health(dbs.runtime_url(installation))
    assert status == 503
    assert body["state"] == "failed" and body["alembic_heads"] == ["zz_future_0001"]


@pytest.mark.parametrize(
    ("fault", "outcome"),
    [("no --backup-dir", "nothing done"), ("never migrated", "never migrated"),
     ("no recipient", "nothing migrated")],
)  # fmt: skip
def test_a_failed_preflight_changes_nothing_and_says_so(
    dbs, owner_key, release, backups, monkeypatch, fault, outcome
):
    _, recipient = owner_key
    name = dbs.create()
    env = dbs.installation(name)
    if fault != "never migrated":
        assert run_cli("migrate", env=env).exit_code == 0
    release(PROBE)
    schema, settings = tables(dbs, name), settings_of(dbs, name)
    args = ["upgrade", "--backup-dir", str(backups), "--recipient", recipient]
    if fault == "no --backup-dir":
        args = ["upgrade", "--recipient", recipient]
    elif fault == "no recipient":
        args = args[:3]
        monkeypatch.delenv("BACKUP_RECIPIENT", raising=False)

    result = run_cli(*args, env=env)

    refused(result, outcome)
    assert tables(dbs, name) == schema
    assert settings_of(dbs, name) == settings
    assert bundles(backups) == []
    assert not migration_lock_held(dbs, name)


def test_a_failed_migration_rolls_back_says_failed_and_recovers_from_the_pre_upgrade_bundle(
    source, dbs, owner_key, release, backups
):
    key, recipient = owner_key
    other = dbs.create()  # installation B, on the same server
    b_env = dbs.installation(other)
    assert run_cli("migrate", env=b_env).exit_code == 0
    before = semantic(dbs, source["name"])
    schema = tables(dbs, source["name"])
    release(FAILING)

    result = upgrade(source, recipient, backups)

    output = refused(result)
    [bundle] = bundles(backups)
    assert "division by zero" in output and HEAD in output  # the cause, and where it stands
    # The schema did not move: the way back is the previous image, not a restore.
    says(output, "revert image")
    assert str(bundle) in output and "restore" not in output.replace(str(bundle), "")
    assert "SELECT" not in output and "Traceback" not in output
    assert heads(dbs, source["name"]) == [HEAD]
    assert tables(dbs, source["name"]) == schema  # upgrade_probe rolled back with it
    assert not migration_lock_held(dbs, source["name"])
    release()  # the previous image again; B was migrated with it
    assert health(dbs.runtime_url(source)) == (503, expected(dbs, source["name"], "failed", [HEAD]))
    assert health(dbs.runtime_url(b_env))[0] == 200  # B keeps serving

    # Recovery: the pre-upgrade bundle restores into a new installation, as it was.
    target, env = restore_into_new(dbs, bundle, key)
    assert semantic(dbs, target) == before
    assert state(dbs, env) == "ok"
    # Or, as the schema never changed, back on the previous release, `migrate` clears it.
    assert run_cli("migrate", env=owner_env(source)).exit_code == 0
    assert state(dbs, source) == "ok"
    assert health(dbs.runtime_url(b_env))[0] == 200


def test_a_set_up_failure_after_the_migration_step_never_says_to_just_revert_the_image(
    installation, owner_key, backups
):
    """The runtime role step runs after the schema step and commits on its own: only a failed
    migration, rolled back as a whole, may say that nothing was applied."""
    _, recipient = owner_key
    as_owner = {**installation, "RUNTIME_DATABASE_ROLE": "farmcore"}  # refused at set-up

    output = refused(upgrade(as_owner, recipient, backups))

    [bundle] = bundles(backups)
    assert str(bundle) in output
    assert OUTCOMES["revert image"] not in output


def test_after_a_failed_post_start_check_reverting_the_release_is_refused_and_restore_recovers(
    source, dbs, owner_key, release, backups
):
    """The schema migrated, then the new release does not serve. Going back to the previous
    release does not take the schema back: that release refuses the database, and the
    pre-upgrade bundle, which carries the new release number, is the way back."""
    key, recipient = owner_key
    before = semantic(dbs, source["name"])
    release(PROBE)
    import farmcore.core.commands.backup as backup_module

    with pytest.MonkeyPatch.context() as newer:  # a real release bump: the new version number
        newer.setattr(backup_module, "version", lambda _: NEXT_RELEASE)
        assert upgrade(source, recipient, backups).exit_code == 0
    [bundle] = bundles(backups)
    assert json.loads(members(decrypt(bundle, key))["manifest.json"])["release"] == NEXT_RELEASE

    release()  # the previous image again
    reverted = run_cli("migrate", env=owner_env(source))

    refused(reverted, "incompatible")
    assert "zz_test_0001" in reverted.output
    assert health(dbs.runtime_url(source))[0] == 503
    assert heads(dbs, source["name"]) == ["zz_test_0001"]
    target, env = restore_into_new(dbs, bundle, key)
    assert semantic(dbs, target) == before
    assert health(dbs.runtime_url(env)) == (200, expected(dbs, target, "ok", [HEAD]))


def test_a_second_migrator_exits_at_once_while_another_installation_migrates_meanwhile(
    installation, dbs, owner_key, release, backups
):
    """One upgrade at a time per installation (AC iso 03 #1), and a stuck upgrade never holds
    up another installation on the server."""
    _, recipient = owner_key
    release(PROBE)
    other = dbs.create()
    b_env = dbs.installation(other)
    holder = Holder(dbs, installation["name"], *startup.MIGRATION_LOCK)  # another migrator
    try:
        second = upgrade(installation, recipient, backups)
        b_migrated = run_cli("migrate", env=b_env)
        a_state = state(dbs, installation)
    finally:
        holder.release()

    refused(second, "another migrator", "nothing done")
    assert bundles(backups) == [] and heads(dbs, installation["name"]) == [HEAD]
    assert b_migrated.exit_code == 0, b_migrated.output
    assert state(dbs, b_env) == "ok"
    assert a_state == "maintenance"


def test_a_busy_server_list_stops_the_upgrade_after_migrating_and_migrate_finishes_it(
    installation, dbs, owner_key, release, backups
):
    """Another installation's restore or migrate holds `pg_database` past the lock timeout:
    the schema step is done, the runtime role step is not. The installation says failed, and
    says how to finish; the failed mark itself never waits for that lock."""
    _, recipient = owner_key
    name = installation["name"]
    release(PROBE)
    engine = create_engine(dbs.server.url("postgres"))
    conn = engine.connect()
    conn.exec_driver_sql("LOCK TABLE pg_catalog.pg_database IN SHARE ROW EXCLUSIVE MODE")
    timer = threading.Timer(20, conn.rollback)  # never hang the run, whatever happens
    timer.start()
    try:
        started = time.monotonic()
        result = upgrade(installation, recipient, backups)
        took = time.monotonic() - started
    finally:
        timer.cancel()
        conn.rollback()
        conn.close()
        engine.dispose()

    refused(result, "busy", "farmcore migrate")
    assert "zz_test_0001" in result.output  # it says how far it got
    assert took < 15  # the lock timeout, not the safety timer
    assert heads(dbs, name) == ["zz_test_0001"]
    assert state(dbs, installation) == "failed"
    assert run_cli("migrate", env=owner_env(installation)).exit_code == 0
    assert health(dbs.runtime_url(installation)) == (
        200,
        expected(dbs, name, "ok", ["zz_test_0001"]),
    )


# --- K8, K9: real processes -----------------------------------------------------------------------

LAUNCH = (
    "import sys; from pathlib import Path; import farmcore.core.startup as s; "
    "s.MIGRATIONS = Path(sys.argv[1]); from farmcore.cli import cli; "
    "cli(sys.argv[2:], prog_name='farmcore')"
)


def farmcore(root: Path, env: dict[str, str], *args: str) -> subprocess.Popen:
    """A real `farmcore` process running the release at `root`."""
    return subprocess.Popen(
        [sys.executable, "-c", LAUNCH, str(root), *args],
        env={**os.environ, **owner_env(env)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


@pytest.mark.parametrize("moment", ["waiting for the lock", "mid-migration", "after the commit"])
def test_a_stop_signal_leaves_an_honest_installation_and_a_rerun_finishes(
    installation, dbs, owner_key, release, backups, tmp_path, moment
):
    """`docker compose stop` (SIGTERM) on a real process, once the server shows it waiting at
    that moment (a 30-run storm found each of these)."""
    _, recipient = owner_key
    name = installation["name"]
    settings = settings_of(dbs, name)
    up = ("upgrade", "--backup-dir", str(backups), "--recipient", recipient)
    if moment == "waiting for the lock":
        root, args, key = release(), ("migrate",), startup.MIGRATION_LOCK
        reached = lambda: migration_lock_waiters(dbs, name) == 1  # noqa: E731
    elif moment == "mid-migration":
        root, args, key = release(GATED), up, (GATE,)
        reached = lambda: waiting_on_gate(dbs, name) == 1  # noqa: E731
    else:
        root, args, key = release(PROBE), up, (RUNTIME_LOCK,)
        reached = lambda: advisory_waiters(dbs, name) == 1  # noqa: E731
    holder = Holder(dbs, name, *key)
    try:
        proc = farmcore(root, installation, *args)
        wait_for(lambda: reached() or proc.poll() is not None, moment)
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
        # Read while the lock is still held: a statement merely abandoned by the client would
        # still be waiting on the server (and would carry on once the lock is free).
        stuck = reached()
        held = key != startup.MIGRATION_LOCK and migration_lock_held(dbs, name)
    finally:
        holder.release()

    assert proc.returncode == 1, out
    says(out, "stopped")
    assert "Traceback" not in out
    assert not stuck and not held  # cancelled on the server, the lock gone with it
    if moment == "waiting for the lock":
        says(out, "nothing migrated")
        assert settings_of(dbs, name) == settings  # no failed mark
        return
    assert state(dbs, installation) == "failed"
    [bundle] = bundles(backups)  # complete, and taken before the migration
    assert json.loads(members(decrypt(bundle, owner_key[0]))["manifest.json"])["alembic_heads"] == [
        HEAD
    ]
    if moment == "mid-migration":
        assert heads(dbs, name) == [HEAD]
        assert "upgrade_probe" not in str(tables(dbs, name))
        rerun_dir = tmp_path / "rerun"
        rerun_dir.mkdir()
        rerun = upgrade(installation, recipient, rerun_dir)
    else:
        says(out, "farmcore migrate")
        assert "zz_test_0001" in out and "nothing was migrated" not in out
        assert heads(dbs, name) == ["zz_test_0001"]
        rerun = run_cli("migrate", env=owner_env(installation))
    assert rerun.exit_code == 0, rerun.output
    assert state(dbs, installation) == "ok"


@pytest.mark.parametrize(
    ("command", "stops"),
    [
        ("migrate", ("SIGTERM", "SIGHUP")),
        ("upgrade", ("SIGTERM", "SIGHUP")),
        ("upgrade", ("SIGINT",)),
    ],
    ids=["migrate finished", "upgrade stopped", "upgrade on Ctrl-C"],
)
def test_a_stop_signal_never_cuts_the_report_short_and_ctrl_c_is_reported_like_one(
    installation, dbs, owner_key, backups, monkeypatch, caught_signals, command, stops
):
    """Compose sends SIGTERM again, a closed terminal SIGHUP after it (the counterpart of I6):
    every line the command prints is sent `stops`. The first stop ends the command; until it
    has said what happened, nothing else does, and a finished command stays finished."""
    import typer

    echo = typer.echo

    def stopped_again(message, *args, **kwargs) -> None:  # the output boundary
        for name in stops:
            os.kill(os.getpid(), getattr(signal, name))
        echo(message, *args, **kwargs)

    monkeypatch.setattr(typer, "echo", stopped_again)
    _, recipient = owner_key

    if command == "migrate":
        result = run_cli("migrate", env=owner_env(installation))
        assert result.exit_code == 0, result.output
        assert f"installation {installation_id(dbs, installation['name'])}" in result.output
    else:  # the first line it prints is the pre-upgrade backup's: stopped before migrating
        result = upgrade(installation, recipient, backups)
        refused(result, "nothing migrated")
        assert stops[0] in result.output
    assert caught_signals == []


@pytest.mark.slow
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
        says(out, "another migrator")
        wait_for(lambda: migration_lock_waiters(dbs, source["name"]) == 1, "migrate to wait")
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


# --- L7: two real Compose projects ----------------------------------------------------------------


def _image_with(base: str, tag: str, context: Path, revision: str, cmd: str | None) -> str:
    """A newer release: `base` with the version number `NEXT_RELEASE`, plus a revision file
    (and, optionally, an app that will not start), built from a throwaway context."""
    package = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", base, "-c",
         "import farmcore, os; print(os.path.dirname(farmcore.__file__))"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    context.mkdir()
    (context / "zz_test_0001.py").write_text(
        REVISION.format(rev="zz_test_0001", down=HEAD, body=revision)
    )
    lines = [
        f"FROM {base}",
        "USER root",
        f"COPY zz_test_0001.py {package}/migrations/versions/",
        # What a release bump changes: the package's version and its installed metadata.
        f"RUN sed -i 's/^__version__ = .*/__version__ = \"{NEXT_RELEASE}\"/' {package}/__init__.py"
        f" && sed -i 's/^Version: .*/Version: {NEXT_RELEASE}/' {package}-*.dist-info/METADATA",
        "USER farmcore",
    ]
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
        refused_ = a.compose(
            "run", "--rm", "--no-deps", "-e", "BACKUP_RECIPIENT=", "migrate",
            "farmcore", "upgrade", "--backup-dir", "/backups", check=False,
        )  # fmt: skip
        assert refused_.returncode == 1 and "nothing was migrated" in refused_.stderr

        # 2. Migration failure: rolled back, A says failed; the previous image recovers it.
        failed = a.compose(*upgrade_args, "--backup-dir", "/backups", check=False)
        assert failed.returncode == 1
        assert "division by zero" in failed.stderr, failed.stderr
        assert _psql(a, "SELECT version_num FROM alembic_version") == [HEAD]
        _set_image(a, image)
        a.compose("up", "-d", "--wait", "--wait-timeout", "180")
        assert a.health()["state"] == "ok"

        # 3. Post-start failure: the schema migrates, then the new release does not serve.
        _set_image(a, broken_app)
        a.compose("stop", "app")
        migrated = a.compose(*upgrade_args, "--backup-dir", "/backups")
        assert f"release {NEXT_RELEASE}" in migrated.stdout and "zz_test_0001" in migrated.stdout
        started = a.compose("up", "-d", "--wait", "--wait-timeout", "60", check=False)
        assert started.returncode != 0
        # Reverting the image does not revert the schema: the previous release refuses it.
        _set_image(a, image)
        reverted = a.compose("up", "-d", "--wait", "--wait-timeout", "120", check=False)
        assert reverted.returncode != 0
        logs = a.compose("logs", "--no-color", "migrate").stdout
        assert "incompatible" in logs and "zz_test_0001" in logs

        # The documented recovery, step by step, with the previous image (set above): keep the
        # failed database aside, restore the bundle the new release wrote.
        a.compose("stop", "app", "bot", "backup")
        listed = a.compose(
            "run", "--rm", "--no-deps", "--entrypoint", "ls", "backup", "-l", "/backups"
        )  # fmt: skip
        pre = sorted(w for w in listed.stdout.split() if w.startswith("pre-upgrade-"))
        assert len(pre) == 2, pre  # one per upgrade that ran; the refused preflight wrote none
        assert all(line.startswith("-rw-------") for line in listed.stdout.splitlines()
                   if "pre-upgrade-" in line), listed.stdout  # fmt: skip
        restore_dir = tmp_path / "restore"
        restore_dir.mkdir(mode=0o700)
        a.compose("cp", f"backup:/backups/{pre[-1]}", str(restore_dir / "bundle.age"))
        shutil.copy(key, restore_dir / "owner.key")
        for path in restore_dir.iterdir():
            path.chmod(0o600)
        manifest = json.loads(members(decrypt(restore_dir / "bundle.age", key))["manifest.json"])
        assert (manifest["release"], manifest["alembic_heads"]) == (NEXT_RELEASE, [HEAD])
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
        paused = a.compose("run", "--rm", "--no-deps", "migrate", "farmcore", "effects")
        assert paused.stdout.strip() == "external effects: paused"
        a.compose("run", "--rm", "--no-deps", "migrate", "farmcore", "effects", "enable")
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
    assert effects.stdout.strip() == "external effects: enabled"
    assert b.health()["installation_id"] == b_id

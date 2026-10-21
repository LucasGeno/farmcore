"""The swap that puts a restored copy in place, and an honest outcome when it, or a step after
it, cannot finish.

Seams: the `farmcore restore` CLI in this process with its `on_step` stop points (a lock or a
failure at the moment just before a step), pg_restore stand-ins on PATH, a real `farmcore
restore` process (SIGKILL), and the catalog. Every test restores the module's one bundle of A,
which carries a member session.

Failure model (what these tests are for):
- a target that loses its name, or two renames that are not one transaction;
- a false "nothing was restored" after the rows are in (followed by a second restore), or a
  false "restore failed" when only a cosmetic step failed;
- a wait for `pg_database` that outlasts `docker compose stop`'s 10 s grace.
"""

from __future__ import annotations

import shutil
import time

import pytest
from sqlalchemy import create_engine

from tests.instances.support import (
    HeldLoad,
    as_superuser,
    effects,
    fake_tool,
    held_copy,
    installation_id,
    old_of,
    refused,
    restore,
    run_cli,
    same_records,
    says,
    sigterm,
    staging_of,
    wait_for,
)

pytestmark = pytest.mark.usefixtures("pg_tools")


# --- H: the swap, and an honest outcome after it -------------------------------------------------


def test_a_restore_that_stops_after_the_data_never_carries_sessions_or_opens_the_copy(
    shared_source, member_bundle, owner_key, module_dbs
):
    """The runtime role setup refuses (it names the owner) after the swap: the rows are in."""
    key, _ = owner_key
    target = module_dbs.create()
    env = {**module_dbs.installation(target), "RUNTIME_DATABASE_ROLE": module_dbs.server.superuser}

    result = restore(member_bundle, key, env)

    refused(result, "data is in", "run migrate")
    assert module_dbs.query(target, "SELECT count(*) FROM log") == [(10,)]
    assert module_dbs.query(target, "SELECT count(*) FROM local_session") == [(0,)]
    assert effects(env) == "external effects: paused"
    assert run_cli("migrate", env=module_dbs.installation(target)).exit_code == 0


def test_a_restore_kept_from_the_database_list_after_the_swap_says_the_data_is_in(
    restore_at, shared_source, module_dbs, server_list
):
    result, target, env, _ = restore_at(migrate=lambda _t: server_list.lock())
    server_list.unlock()

    refused(result, "busy", "data is in")
    assert "nothing was restored" not in result.output
    assert same_records(module_dbs, target, shared_source["name"])
    assert run_cli("migrate", env=env).exit_code == 0


def test_an_emptied_target_that_cannot_be_dropped_is_named_and_migrate_still_runs(
    restore_at, shared_source, module_dbs
):
    """A failed drop of the emptied target only leaves that database behind: restore names
    it, says how to drop it, and finishes the job."""
    olds: list[str] = []

    def undroppable(target: str) -> None:  # the server refuses to drop a template database
        olds.append(old_of(module_dbs, target))
        module_dbs.on_server(f'ALTER DATABASE "{olds[0]}" IS_TEMPLATE true')

    try:
        result, target, _, _ = restore_at(drop_old=undroppable)
    finally:
        if olds:
            module_dbs.on_server(f'ALTER DATABASE "{olds[0]}" IS_TEMPLATE false')
            module_dbs.on_server(f'DROP DATABASE "{olds[0]}"')

    assert result.exit_code == 0, result.output
    says(result.output, "data is in", "drop hint")
    assert olds[0] in result.output
    assert installation_id(module_dbs, target) in result.output  # migrated
    assert same_records(module_dbs, target, shared_source["name"])


def test_a_swap_that_fails_halfway_puts_the_target_back_and_reports_its_own_error(
    restore_at, module_dbs, tmp_path, monkeypatch, caught_signals
):
    """The second rename fails (someone holds the staging copy open) while a stop signal
    waits: the target keeps its name and OID, the swap's own error is reported, not the
    signal, and nothing is left."""
    real, psql = shutil.which("pg_restore"), shutil.which("psql")
    monkeypatch.setenv("PATH", fake_tool(tmp_path / "bin", "pg_restore", f"""case "$*" in
  *--single-transaction*)
    {as_superuser(module_dbs)} "{psql}" -X -q -c "SELECT pg_sleep(120)" >/dev/null 2>&1 &
    exec "{real}" "$@";;
  *) exec "{real}" "$@";;
esac
"""))  # fmt: skip

    def held_open_then_stopped(_target: str) -> None:
        wait_for(lambda: module_dbs.query("postgres", "SELECT count(*) FROM pg_stat_activity "
                                          "WHERE query LIKE 'SELECT pg_sleep(120)%'")[0][0],
                 "the staging copy to be held open")  # fmt: skip
        sigterm()

    result, target, _, untouched = restore_at(swap=held_open_then_stopped)

    refused(result, "database unusable")
    assert "stopped" not in result.output
    untouched.check()  # same OID under its name; no staging copy, no loader role
    assert caught_signals == []


def test_a_restore_killed_between_the_renames_leaves_the_target_under_its_name(
    shared_source, member_bundle, owner_key, module_dbs, tmp_path, private_tmp
):
    """The two renames are one transaction (review round 7): a restore killed (SIGKILL, a
    crash) while the second one waits for a lock leaves the target as it was."""
    key, _ = owner_key
    target = module_dbs.create()
    env = module_dbs.installation(target)
    oid = module_dbs.query("postgres", "SELECT oid FROM pg_database WHERE datname = :d", d=target)
    held = HeldLoad(tmp_path, member_bundle, key, env, private_tmp)
    staging, _ = held_copy(module_dbs, target)
    # A transaction that comments on the staging copy locks it: renaming it waits.
    engine = create_engine(module_dbs.server.url("postgres"))
    conn = engine.connect()
    try:
        conn.begin()
        conn.exec_driver_sql(f"COMMENT ON DATABASE \"{staging}\" IS 'held'")
        held.release()
        rename = (
            "SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
            "AND query LIKE 'ALTER DATABASE%RENAME%'"
        )
        deadline = time.monotonic() + 120
        while not (renaming := module_dbs.query("postgres", rename)):
            assert held.proc.poll() is None, held.result()
            assert time.monotonic() < deadline, "restore never reached the swap"
            time.sleep(0.02)
        held.proc.kill()  # and its session dies with it, as in a crash
        module_dbs.query("postgres", "SELECT pg_terminate_backend(:p)", p=renaming[0][0])
    finally:
        conn.rollback()
        conn.close()
        engine.dispose()
    held.result()

    assert module_dbs.query("postgres", "SELECT oid FROM pg_database WHERE datname = :d",
                            d=target) == oid  # fmt: skip
    again = restore(member_bundle, key, env)
    assert again.exit_code == 0, again.output
    assert staging_of(module_dbs, target) == []


def test_a_swap_kept_waiting_for_the_database_list_renames_nothing_and_says_so(
    restore_at, member_bundle, owner_key, server_list
):
    """The swap waits for `pg_database` at most the lock timeout, well within compose's 10 s,
    and a swap that timed out renamed nothing; the rerun works."""
    key, _ = owner_key

    def busy(_target: str) -> None:
        server_list.lock()
        server_list.unlock_once_waiting("DROP DATABASE")  # the cleanup waits for it too

    result, _, env, untouched = restore_at(swap=busy)

    refused(result, "busy", "nothing restored")
    untouched.check()
    again = restore(member_bundle, key, env)
    assert again.exit_code == 0, again.output


def test_a_restore_kept_from_creating_its_copy_says_nothing_was_restored(
    restore_at, member_bundle, owner_key, server_list
):
    """CREATE DATABASE cannot be stopped while it waits for `pg_database` (a signal waits for
    it, review round 6): it gives up after the lock timeout, so a stop never ends in SIGKILL
    and a left staging copy."""
    key, _ = owner_key

    result, _, env, untouched = restore_at(create=lambda _t: server_list.lock())
    server_list.unlock()

    refused(result, "busy", "nothing restored")
    untouched.check()
    again = restore(member_bundle, key, env)
    assert again.exit_code == 0, again.output

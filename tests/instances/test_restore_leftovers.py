"""What a restore leaves when its load fails, when another restore races it, or when it was
killed: the target as it was, no staging copy and no loader role, and nobody else's database.

Seams: the `farmcore restore` CLI, real `farmcore restore` processes held at the load
(`HeldLoad`) or killed, pg_restore stand-ins on PATH, and the catalog. The concurrency stress
runs are `slow`: they found real deadlocks, but they are for before merge, not every loop.

Failure model (what these tests are for):
- a failed load that leaves a half-loaded copy the app can open;
- two restores racing on one target, or sweeping each other's staging copies;
- leftovers piling up after SIGKILL, or a sweep that drops someone else's database;
- "tuple concurrently updated" or "deadlock detected" between restores and migrates.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from tests.instances.support import (
    FARMCORE,
    HeldLoad,
    Untouched,
    backup,
    fake_tool,
    held_copy,
    owner_env,
    refused,
    restore,
    role_exists,
    run_cli,
    same_records,
    staging_of,
)

pytestmark = pytest.mark.usefixtures("pg_tools")


# --- G4: the load ---------------------------------------------------------------------------------


def test_a_failed_load_leaves_the_target_as_it_was_and_a_rerun_restores(
    shared_source, member_bundle, dbs, owner_key, tmp_path, monkeypatch, private_tmp
):
    key, _ = owner_key
    target = dbs.create()
    env = dbs.installation(target)
    real = shutil.which("pg_restore")
    # pg_restore dies once it writes into the target (say the disk filled up).
    path = fake_tool(tmp_path / "bin", "pg_restore", f"""case "$*" in
  *--single-transaction*) echo "pg_restore: error: could not write" >&2; exit 1;;
  *) exec "{real}" "$@";;
esac
""")  # fmt: skip
    real_path = os.environ["PATH"]
    monkeypatch.setenv("PATH", path)
    untouched = Untouched(dbs, shared_source["name"], target, [member_bundle], private_tmp)

    failed = restore(member_bundle, key, env)

    refused(failed, "load failed")
    untouched.check()  # the staging copy and its loader role are gone, the target as it was
    shut_out = create_engine(dbs.runtime_url(env))
    with (
        pytest.raises(Exception, match="permission denied|does not exist|authentication"),
        shut_out.connect(),
    ):
        pass
    shut_out.dispose()
    monkeypatch.setenv("PATH", real_path)
    again = restore(member_bundle, key, env)
    assert again.exit_code == 0, again.output
    assert same_records(dbs, target, shared_source["name"])


# --- J: concurrency and leftovers -----------------------------------------------------------------


def test_a_second_restore_into_the_same_target_is_refused_and_the_target_is_rechecked(
    shared_source, member_bundle, dbs, owner_key, tmp_path, private_tmp
):
    key, _ = owner_key
    target = dbs.create()
    env = dbs.installation(target)
    held = HeldLoad(tmp_path, member_bundle, key, env, private_tmp)
    _, role = held_copy(dbs, target)
    try:
        second = restore(member_bundle, key, env)
        # Meanwhile someone starts using the target (say its app came up and wrote a table).
        dbs.query(target, "CREATE TABLE public.meanwhile (id int)")
    finally:
        held.release()
    code, out = held.result()

    refused(second, "another restore")
    assert code == 1 and "already holds data" in out, out
    assert dbs.query(target, "SELECT count(*) FROM public.meanwhile") == [(0,)]
    assert staging_of(dbs, target) == [] and not role_exists(dbs, role)


def test_restores_into_targets_with_a_common_prefix_never_meet(
    shared_source, member_bundle, dbs, owner_key, tmp_path, private_tmp
):
    """Two targets whose names share their first 40 characters: neither restore blocks the
    other or sweeps away the other's staging copy."""
    key, _ = owner_key
    common = f"br_{uuid.uuid4().hex[:10]}_" + "x" * 30
    first, second = dbs.create(name=common + "_one"), dbs.create(name=common + "_two")
    held = HeldLoad(tmp_path, member_bundle, key, dbs.installation(first), private_tmp)
    staging, role = held_copy(dbs, first)
    try:
        other = restore(member_bundle, key, dbs.installation(second))
        still_there = staging in staging_of(dbs, first) and role_exists(dbs, role)
    finally:
        held.release()
    code, out = held.result()

    assert other.exit_code == 0, other.output
    assert still_there
    assert code == 0, out
    assert same_records(dbs, first, shared_source["name"])
    assert same_records(dbs, second, shared_source["name"])


def test_the_next_restore_removes_what_a_killed_restore_left_and_never_a_strangers_lookalike(
    shared_source, member_bundle, dbs, owner_key, tmp_path, private_tmp
):
    """SIGKILL (or a power cut) skips every cleanup; the next restore into the target does it,
    but a database that only looks like a staging copy, owned by another role, stays."""
    key, _ = owner_key
    target = dbs.create()
    env = dbs.installation(target)
    held = HeldLoad(tmp_path, member_bundle, key, env, private_tmp)
    held.proc.kill()
    held.result()
    staging, role = held_copy(dbs, target)  # both left behind
    for d in os.listdir(private_tmp):  # a killed process leaves its own temp dir too
        shutil.rmtree(private_tmp / d)
    lookalike, stranger = staging[:-8] + "0badcafe", f"rt_{uuid.uuid4().hex[:10]}"
    dbs.roles.append(stranger)
    dbs.on_server(f'CREATE ROLE "{stranger}" NOLOGIN')
    dbs.create(name=lookalike, postgis=False)
    dbs.on_server(f'ALTER DATABASE "{lookalike}" OWNER TO "{stranger}"')

    result = restore(member_bundle, key, env)

    assert result.exit_code == 0, result.output
    assert staging_of(dbs, target) == [lookalike] and not role_exists(dbs, role)
    assert same_records(dbs, target, shared_source["name"])


def _start(*args: str, env: dict[str, str], extra_path: str = "", tmp: Path) -> subprocess.Popen:
    full = {**os.environ, **env, "TMPDIR": str(tmp)}
    if extra_path:
        full["PATH"] = extra_path
    return subprocess.Popen(
        [FARMCORE, *args], env=full, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )


@pytest.mark.slow
def test_restores_and_a_migrate_started_together_all_finish(
    source, dbs, owner_key, tmp_path, private_tmp
):
    """Each one revokes PUBLIC's access on every database of the server, and creates, renames
    and drops databases: run at the same moment, none may fail on another's catalog change
    ("tuple concurrently updated", review 8/8)."""
    key, recipient = owner_key
    bundle = backup(source, recipient, tmp_path / "a.age")
    common = f"br_{uuid.uuid4().hex[:10]}_" + "x" * 30  # 44 characters
    targets = [dbs.create(name=f"{common}_{n}") for n in ("one", "two", "three")]
    # Every restore waits at its last check of the bundle, then all go at once.
    gate = tmp_path / "gate"
    gate.mkdir()
    real = shutil.which("pg_restore")
    path = fake_tool(tmp_path / "bin", "pg_restore", f"""case "$*" in
  *--table=alembic_version*)
    touch "{gate}/ready.$$"
    i=0
    while [ ! -e "{gate}/go" ] && [ $i -lt 1800 ]; do sleep 0.05; i=$((i+1)); done
    exec "{real}" "$@";;
  *) exec "{real}" "$@";;
esac
""")  # fmt: skip
    restores = [
        _start("restore", str(bundle), "--identity", str(key), env=dbs.installation(t),
               extra_path=path, tmp=private_tmp)
        for t in targets
    ]  # fmt: skip
    deadline = time.monotonic() + 180
    while len(list(gate.glob("ready.*"))) < len(restores):
        assert time.monotonic() < deadline, "the restores never reached the gate"
        time.sleep(0.05)
    (gate / "go").touch()
    migrating = _start("migrate", env=owner_env(source), tmp=private_tmp)
    done = [p.communicate(timeout=300)[0] for p in [*restores, migrating]]

    assert [p.returncode for p in [*restores, migrating]] == [0] * 4, done
    assert all(same_records(dbs, t, source["name"]) for t in targets)
    assert staging_of(dbs, targets[0]) == []


@pytest.mark.slow
def test_migrates_of_running_installations_during_restores_never_deadlock(
    source, dbs, owner_key, tmp_path, private_tmp
):
    """A migrate of an installation that already has its runtime role first revokes that
    role's CONNECT, a change to `pg_database`, then waits for the server-wide catalog lock:
    unless the lock comes first, two of them (or one and a restore) wait on each other and
    one dies with "deadlock detected" (7/10, round 6). Real processes, repeated."""
    key, recipient = owner_key
    bundle = backup(source, recipient, tmp_path / "a.age")
    other = dbs.create()
    running = [owner_env(source), dbs.installation(other)]
    assert run_cli("migrate", env=running[1]).exit_code == 0

    rounds, failed = [], []
    for _ in range(3):  # before the fix, every one of these deadlocked at least once
        restoring = _start("restore", str(bundle), "--identity", str(key),
                           env=dbs.installation(dbs.create()), tmp=private_tmp)  # fmt: skip
        pairs = 0
        while restoring.poll() is None:  # two migrates at a time, for as long as it runs
            migrating = [_start("migrate", env=e, tmp=private_tmp) for e in running]
            for p in migrating:
                out = p.communicate(timeout=300)[0]
                if p.returncode:
                    failed.append(out)
            pairs += 1
        out = restoring.communicate(timeout=300)[0]
        if restoring.returncode:
            failed.append(out)
        rounds.append(pairs)

    assert failed == [], failed
    assert min(rounds) >= 2, rounds  # the migrates did overlap each restore

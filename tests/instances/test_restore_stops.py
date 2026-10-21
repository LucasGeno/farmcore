"""A restore stopped by a signal says honestly what it left, and leaves no plaintext copy.

Seams: the `farmcore restore` CLI in this process with its `on_step` stop points, signals sent
to this process, a pg_restore stand-in on PATH, and the catalog. Every test restores the
module's one bundle of A.

Failure model (what these tests are for):
- `docker compose stop` (SIGTERM) leaving a staging copy with every row in clear, or a loader
  role, or a create the server finished after the client gave up;
- a stop that drops a swapped-in copy, or outlasts compose's grace after the swap;
- a stop that is noted and lost, or a second stop that cuts the first one's message short.
"""

from __future__ import annotations

import os
import shutil
import signal
import sys
import threading
import time
import weakref

import pytest
from sqlalchemy import create_engine

from tests.instances.support import (
    OUTCOMES,
    as_superuser,
    effects,
    fake_tool,
    old_of,
    refused,
    run_cli,
    same_records,
    says,
    sigterm,
    staging_of,
    wait_for,
)

pytestmark = pytest.mark.usefixtures("pg_tools")


# --- I: stop signals ---------------------------------------------------------------------------


def test_a_stopped_restore_removes_its_staging_copy_and_loader_role(
    restore_at, module_dbs, tmp_path, monkeypatch, caught_signals
):
    """`docker compose stop` mid-load: the staging copy holds every row in clear. pg_restore
    notes what exists, then the restore process gets the signal."""
    seen = tmp_path / "seen.txt"
    real, psql = shutil.which("pg_restore"), shutil.which("psql")
    listing = "SELECT datname FROM pg_database UNION ALL SELECT rolname FROM pg_roles"
    monkeypatch.setenv("PATH", fake_tool(tmp_path / "bin", "pg_restore", f"""case "$*" in
  *--single-transaction*)
    {as_superuser(module_dbs)} "{psql}" -X -At -d postgres -c "{listing}" > "{seen}"
    kill -TERM $PPID
    exec sleep 60;;
  *) exec "{real}" "$@";;
esac
"""))  # fmt: skip
    handler = signal.getsignal(signal.SIGTERM)

    result, target, _, untouched = restore_at()

    refused(result, "stopped", "nothing restored")
    existed = seen.read_text().split()
    staging = [n for n in existed if n.startswith(f"{target}_restore_")]
    assert len(staging) == 1 and f"farmcore_restore_{staging[0].rsplit('_', 1)[1]}" in existed
    untouched.check()  # both were there at the signal, and both are gone
    assert signal.getsignal(signal.SIGTERM) == handler  # the caller's handler is back
    assert caught_signals == []  # restore took the signal itself


def _hold_the_template(dbs, target: str):
    """A session on the target (the template) makes CREATE DATABASE wait for it to leave."""
    engine = create_engine(dbs.server.url(target))
    conn = engine.connect()
    conn.exec_driver_sql("SELECT 1")
    return conn.invalidate, engine.dispose


def _hold_the_role_catalog(dbs, target: str):
    """A lock on the role catalog makes CREATE ROLE wait until it is released."""
    engine = create_engine(dbs.server.url("postgres"))
    conn = engine.connect()
    conn.begin()
    conn.exec_driver_sql("LOCK TABLE pg_catalog.pg_authid IN EXCLUSIVE MODE")

    def release() -> None:
        conn.rollback()
        conn.close()

    return release, engine.dispose


@pytest.mark.parametrize(
    ("step", "statement", "hold"),
    [("create", "CREATE DATABASE", _hold_the_template),
     ("create_loader", "CREATE ROLE", _hold_the_role_catalog)],
    ids=["CREATE DATABASE", "CREATE ROLE"],
)  # fmt: skip
def test_a_stop_while_a_server_statement_runs_waits_for_it_then_removes_what_it_made(
    restore_at, module_dbs, caught_signals, step, statement, hold
):
    """The server finishes a create its client gave up on: restore must wait for it and then
    drop what it made, not leave it behind."""
    holding: list = []

    def stop_while_waiting(target: str) -> None:
        release, dispose = hold(module_dbs, target)
        holding.append(dispose)
        waiting = (
            "SELECT count(*) FROM pg_stat_activity WHERE state = 'active' "
            "AND query LIKE :p AND pid <> pg_backend_pid()"
        )

        def stop() -> None:
            try:
                wait_for(lambda: module_dbs.query("postgres", waiting, p=statement + "%")[0][0],
                         f"{statement} to wait")  # fmt: skip
                sigterm()
                # The handler's first act is to ignore further stops: the signal was taken.
                wait_for(lambda: signal.getsignal(signal.SIGTERM) is signal.SIG_IGN, "the stop")
            finally:
                release()

        threading.Thread(target=stop, daemon=True).start()

    try:
        result, _, _, untouched = restore_at(**{step: stop_while_waiting})
    finally:
        for dispose in holding:
            dispose()

    refused(result, "stopped", "nothing restored")
    untouched.check()
    assert caught_signals == []


def test_a_signal_during_the_swap_waits_for_it_and_says_the_copy_is_in(
    restore_at, shared_source, module_dbs, caught_signals
):
    """Stopped between the two renames, restore must neither drop the copy that now holds the
    rows nor say nothing was restored: the signal waits until the swap is done."""
    # Another thread in the process (libraries start them), which may receive the signal.
    idle = threading.Event()
    helper = threading.Thread(target=idle.wait, daemon=True)
    helper.start()
    try:
        result, target, env, _ = restore_at(swap=sigterm)
    finally:
        idle.set()
        helper.join()

    refused(result, "stopped", "data is in")
    assert "nothing was restored" not in result.output
    assert same_records(module_dbs, target, shared_source["name"])
    assert module_dbs.query(target, "SELECT count(*) FROM local_session") == [(0,)]
    assert effects(env) == "external effects: paused"
    assert staging_of(module_dbs, target) == []
    assert caught_signals == []
    assert run_cli("migrate", env=env).exit_code == 0  # as the message says


def test_a_stop_while_the_emptied_target_waits_to_be_dropped_ends_after_the_lock_timeout(
    restore_at, shared_source, module_dbs, server_list, caught_signals
):
    """The drop runs where stop signals wait for it, so it waits for `pg_database` only up to
    the lock timeout: a stop never outlasts `docker compose stop`'s grace."""
    stopped_at: list[float] = []

    def busy_and_stopped(_target: str) -> None:
        server_list.lock()
        stopped_at.append(time.monotonic())
        sigterm()

    result, target, env, _ = restore_at(drop_old=busy_and_stopped)

    took = time.monotonic() - stopped_at[0]
    server_list.unlock()
    old = old_of(module_dbs, target)
    module_dbs.on_server(f'DROP DATABASE "{old}"')
    refused(result, "stopped", "data is in", "drop hint")
    assert old in result.output
    assert took < 5, f"the stop took {took:.1f} s"  # the safety unlock is at 10 s
    assert same_records(module_dbs, target, shared_source["name"])
    assert caught_signals == []
    assert run_cli("migrate", env=env).exit_code == 0


def test_a_stop_the_handler_could_not_raise_still_stops_the_restore_before_the_swap(
    restore_at, monkeypatch, caught_signals
):
    """Python drops what a signal handler raises inside a weakref callback ("Exception ignored
    in ..."), and the handler ignores further stops by then (about 1 in 96 SIGTERMs in the
    storm): the noted stop must still end the restore before the swap."""
    dropped: list[str] = []
    monkeypatch.setattr(sys, "unraisablehook", lambda u: dropped.append(type(u.exc_value).__name__))

    def stop_inside_a_weakref_callback(_target: str) -> None:
        handler = signal.getsignal(signal.SIGTERM)

        class Collected:
            pass

        gone = Collected()
        ref = weakref.ref(gone, lambda _ref: handler(signal.SIGTERM, None))
        del gone  # the callback runs here
        assert ref() is None

    result, _, _, untouched = restore_at(drop_loader=stop_inside_a_weakref_callback)

    assert dropped == ["Interrupted"]  # the handler's raise was lost
    refused(result, "stopped", "nothing restored")
    untouched.check()
    assert caught_signals == []


@pytest.mark.parametrize("first", ["stopped before the copy", "finished"])
def test_a_stop_signal_never_cuts_the_report_short_or_turns_a_finished_restore_into_a_failure(
    restore_at, monkeypatch, caught_signals, first
):
    """Compose sends SIGTERM again, a closed terminal SIGHUP after it (round 8): until the
    command has said what happened, stop signals are ignored. A restore that has finished,
    migrate included, only waits for its report."""
    import typer

    echo = typer.echo

    def stopped_again(message, *args, **kwargs) -> None:  # the output boundary
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGHUP)
        echo(message, *args, **kwargs)

    monkeypatch.setattr(typer, "echo", stopped_again)

    if first == "finished":
        result, _, _, _ = restore_at()
        assert result.exit_code == 0, result.output
        says(result.output, "restored")
        assert OUTCOMES["nothing restored"] not in result.output
    else:
        result, _, _, untouched = restore_at(create=sigterm)
        refused(result, "stopped", "nothing restored")
        untouched.check()
    assert caught_signals == []

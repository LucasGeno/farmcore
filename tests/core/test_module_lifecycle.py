"""Module lifecycle: switching an app off takes effect in every process at once (web routes,
assistant tools, a running `farmcore worker`), fences its in-flight work, holds its outbox intents,
and switching it on again resumes without losing or reseeding anything."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from farmcore.core import clock, jobs, outbox, registry, tasks
from farmcore.core.models.canonical import Farm
from farmcore.core.models.kernel import AppModule
from farmcore.core.tools import collect_tools
from farmcore.db import session_scope
from farmcore.main import create_app
from tests.core.fake_lifecycle.beacon.app import SWEEPS

PACKAGES = ("tests.core.fake_lifecycle",)
WAIT = 20.0  # seconds; every wait ends on an observable state, this only bounds a hang


@pytest.fixture
def lifecycle_dir(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("LIFECYCLE_DIR", str(tmp_path))
    return tmp_path


def _live_farm(factory, settings, *, beacon: str = registry.INSTALLED) -> int:
    with session_scope(factory) as session:
        farm = Farm(name="Lifecycle")
        session.add(farm)
        session.add(AppModule(name="beacon", version="1.0.0", state=beacon))
        clock.set_live(session, settings)
        session.flush()
        return farm.id


def _queued(factory, task_name: str) -> int:
    with factory() as session:
        return session.execute(
            text("SELECT count(*) FROM procrastinate_jobs WHERE task_name = :n"), {"n": task_name}
        ).scalar_one()


def test_a_switched_off_app_is_neither_planned_nor_run_by_the_worker(
    session_factory, settings, lifecycle_dir
) -> None:
    ordered = registry.discover(packages=PACKAGES)
    farm_id = _live_farm(session_factory, settings, beacon=registry.UNINSTALLED)

    tasks.plan(session_factory, settings, ordered)
    assert _queued(session_factory, "beacon.ping") == 0
    assert tasks.begin_attempt(session_factory, "beacon.ping", farm_id, timedelta(1)) is None
    SWEEPS.clear()
    jobs.run_due_live(session_factory, ordered, settings)  # first seen
    jobs.run_due_live(session_factory, ordered, settings)
    assert SWEEPS == ["lamp"]


def _publishing(factory, farm_id: int):
    """An open transaction that has just passed the task fence for beacon."""
    attempt = tasks.begin_attempt(factory, "beacon.ping", farm_id, timedelta(minutes=1))
    session = factory()
    assert tasks.publish_if_current(session, attempt)
    return session


def _claiming(factory, farm_id: int):
    """An open transaction that has just admitted one of beacon's outbox intents."""
    session = factory()
    intent = outbox.Intent(0, farm_id, "beacon", "note", {}, 0)
    assert outbox.may_dispatch(session, intent)
    return session


@pytest.mark.parametrize("holder", [_publishing, _claiming], ids=["publication", "claim"])
def test_a_switch_off_waits_for_work_admitted_before_it(
    committed_session_factory, settings, holder
) -> None:
    """Without the row lock a disable could commit between admission and the write, and the
    attempt would publish (or the intent be sent) after the app is off."""
    factory = committed_session_factory
    farm_id = _live_farm(factory, settings)
    admitted = holder(factory, farm_id)
    switch = factory()
    try:
        switch.execute(text("SET LOCAL lock_timeout = '200ms'"))
        with pytest.raises(OperationalError, match="lock timeout"):
            switch.execute(
                text("UPDATE app_module SET state = 'uninstalled' WHERE name = 'beacon'")
            )
    finally:
        switch.rollback()
        switch.close()
        admitted.rollback()
        admitted.close()


@pytest.fixture
def start_worker(settings, tmp_path) -> Iterator[Callable[[], subprocess.Popen]]:
    started: list[subprocess.Popen] = []

    def start() -> subprocess.Popen:
        env = {**os.environ, "DATABASE_URL": settings.database_url}
        command = [sys.executable, "-c", "from farmcore.cli import main; main()", "worker"]
        flags = ["--tick", "0.1", "--app-package", PACKAGES[0]]
        log = (tmp_path / "worker.log").open("w")
        process = subprocess.Popen([*command, *flags], env=env, stdout=log, stderr=log)
        started.append(process)
        return process

    yield start
    for process in started:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _until(check: Callable[[], object], what: str) -> object:
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _read(path: Path) -> str | None:
    return path.read_text() if path.exists() else None


def test_web_tools_and_a_running_worker_agree_on_switching_an_app_off_and_on(
    committed_session_factory, settings, lifecycle_dir, start_worker
) -> None:
    factory = committed_session_factory
    farm_id = _live_farm(factory, settings)
    app = create_app(settings, app_packages=PACKAGES, session_factory=factory)
    admin = {"X-Admin-Token": settings.admin_token.get_secret_value()}

    def note(source_app: str, key: str) -> int:
        with session_scope(factory) as session:
            outbox.add(
                session,
                farm_id=farm_id,
                source_app=source_app,
                kind="note",
                dedupe_key=key,
                payload={},
                ttl=timedelta(hours=1),
            )
            return session.execute(
                text("SELECT id FROM outbox WHERE dedupe_key = :k"), {"k": key}
            ).scalar_one()

    def tool_names() -> list[str]:  # what the chat assistant offers (its live rule)
        with factory() as session:
            return [
                t.name for t in collect_tools(registry.live_active(session, app.state.manifests))
            ]

    start_worker()
    with TestClient(app, headers=admin) as web:
        # On: the worker begins beacon's task, which waits for `release` before publishing.
        _until(lambda: (lifecycle_dir / "began-1").exists(), "beacon's first attempt")
        assert tool_names() == ["beacon_status"]

        assert web.post("/apps/beacon/uninstall").json()["applies"] == "now"
        held = note("beacon", "beacon-1")
        witness = note("lamp", "lamp-1")  # sent after `held` was offered and held
        (lifecycle_dir / "release").touch()

        assert _until(lambda: _read(lifecycle_dir / "decided-1"), "the fence") == "fenced"
        _until(lambda: (lifecycle_dir / f"sent-{witness}").exists(), "lamp's note")
        assert not (lifecycle_dir / f"sent-{held}").exists()
        assert web.get("/beacon/ping").status_code == 404
        assert tool_names() == []

        assert web.post("/apps/beacon/install").json()["applies"] == "now"
        assert web.get("/beacon/ping").status_code == 200
        _until(lambda: (lifecycle_dir / f"sent-{held}").exists(), "the held note, once on")
        with session_scope(factory) as session:  # as `run_now` would, without waiting a deadline
            tasks.enqueue(session, "beacon.ping", farm_id)
        assert _until(lambda: _read(lifecycle_dir / "decided-2"), "a new attempt") == "published"
        assert tool_names() == ["beacon_status"]

    with factory() as session:  # nothing was deleted or redone: the history continues
        assert session.execute(text("SELECT state, count(*) FROM outbox GROUP BY state")).all() == [
            ("sent", 2)
        ]
        assert (
            session.execute(
                text("SELECT attempt FROM task_attempt WHERE task_name = 'beacon.ping'")
            ).scalar_one()
            == 2
        )

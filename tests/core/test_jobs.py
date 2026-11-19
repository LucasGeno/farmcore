"""Manifest jobs: replay `step_to`, `run_now`, and the live jobs `farmcore worker` runs."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from farmcore.core import jobs
from farmcore.core.manifest import JobSpec, Manifest
from tests.core import fake_jobs

T0 = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)


def _manifest(name: str, func: str, interval: timedelta = timedelta(hours=1)) -> Manifest:
    return Manifest(
        name=name,
        version="0.1.0",
        jobs=(JobSpec(name=f"{name}.job", func=f"tests.core.fake_jobs:{func}", interval=interval),),
    )


@pytest.fixture(autouse=True)
def _clear_calls() -> None:
    fake_jobs.CALLS.clear()
    fake_jobs.STARTED.clear()
    fake_jobs.RELEASE.clear()


def test_step_to_runs_each_manifest_job_once_in_load_order(session_factory, settings) -> None:
    manifests = [_manifest("a", "record"), _manifest("b", "record_b")]

    runs = jobs.step_to(session_factory, manifests, T0, settings)

    assert [(r.name, r.ok) for r in runs] == [("a.job", True), ("b.job", True)]
    assert [c[0] for c in fake_jobs.CALLS] == ["record", "record_b"]
    # Each job gets now=t, and the replay clock is already at t inside its session.
    assert all(c[1] == T0 and c[2] == T0 for c in fake_jobs.CALLS)


def test_step_to_runs_a_job_again_only_after_its_interval_or_on_step_back(
    session_factory, settings
) -> None:
    manifests = [_manifest("a", "record", interval=timedelta(hours=1))]

    def ran(t: datetime) -> list[str]:
        return [r.name for r in jobs.step_to(session_factory, manifests, t, settings)]

    assert ran(T0) == ["a.job"]
    assert ran(T0 + timedelta(minutes=59)) == []  # interval not elapsed
    assert ran(T0 + timedelta(hours=1)) == ["a.job"]  # last_run + interval <= t
    assert ran(T0 + timedelta(hours=1)) == []  # same instant again
    assert ran(T0 - timedelta(days=1)) == ["a.job"]  # stepping back resets
    assert ran(T0 - timedelta(days=1) + timedelta(minutes=30)) == []


def test_a_failing_job_is_reported_and_later_jobs_still_run(
    session_factory, settings, caplog
) -> None:
    manifests = [_manifest("a", "fail"), _manifest("b", "record_b")]

    with caplog.at_level("ERROR", logger="farmcore.core.jobs"):
        runs = jobs.step_to(session_factory, manifests, T0, settings)

    failed, ok = runs
    assert (failed.name, failed.ok) == ("a.job", False)
    assert (ok.name, ok.ok, ok.error) == ("b.job", True, None)
    assert [c[0] for c in fake_jobs.CALLS] == ["fail", "record_b"]
    assert "a.job" in caplog.text


def test_a_failure_keeps_the_exception_message_out_of_runs_and_logs(
    session_factory, settings, caplog
) -> None:
    """Messages may quote data rows: only the type is recorded; the log gets code frames."""
    with caplog.at_level("DEBUG", logger="farmcore.core.jobs"):
        (failed,) = jobs.step_to(session_factory, [_manifest("a", "fail")], T0, settings)

    assert failed.error == "RuntimeError"
    assert "fake job failure" not in caplog.text
    assert "fake_jobs.py" in caplog.text and "in fail" in caplog.text  # traceback frames


def test_overlapping_steps_run_a_due_job_once(committed_session_factory, settings) -> None:
    """A double-click or a CLI step during a web step must not run due jobs twice."""
    manifests = [_manifest("a", "slow")]
    results: dict[str, list[jobs.JobRun]] = {}

    def step(key: str) -> None:
        results[key] = jobs.step_to(committed_session_factory, manifests, T0, settings)

    first = threading.Thread(target=step, args=("first",))
    first.start()
    assert fake_jobs.STARTED.wait(timeout=5)
    second = threading.Thread(target=step, args=("second",))
    second.start()
    second.join(timeout=0.5)  # gives the second step time to (wrongly) run the job
    fake_jobs.RELEASE.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert [c[0] for c in fake_jobs.CALLS] == ["slow"]
    assert [r.name for r in results["first"]] == ["a.job"]
    assert results["second"] == []


def test_run_now_runs_one_named_job_at_the_current_clock(session_factory, settings) -> None:
    manifests = [_manifest("a", "record"), _manifest("b", "record_b")]
    jobs.step_to(session_factory, manifests, T0, settings)
    fake_jobs.CALLS.clear()

    run = jobs.run_now(session_factory, manifests, "b.job", settings)

    assert run is not None and (run.name, run.ok) == ("b.job", True)
    assert fake_jobs.CALLS == [("record_b", T0, T0)]
    # A job that is not installed is not an error for the caller.
    assert jobs.run_now(session_factory, manifests, "chat.push_advice", settings) is None


def _fake_app_package(tmp_path, monkeypatch) -> str:
    """Importable package with one app `orchard` whose job records (`fake_jobs.record`)."""
    pkg = f"fake_job_apps_{uuid.uuid4().hex[:8]}"
    app_dir = tmp_path / pkg / "orchard"
    app_dir.mkdir(parents=True)
    (tmp_path / pkg / "__init__.py").write_text("")
    (app_dir / "__init__.py").write_text("")
    (app_dir / "manifest.py").write_text(
        "from datetime import timedelta\n"
        "from farmcore.core.manifest import JobSpec, Manifest\n"
        "MANIFEST = Manifest(name='orchard', version='0.1.0', jobs=(JobSpec("
        "'orchard.job', 'tests.core.fake_jobs:record', timedelta(hours=1)),))\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    return pkg


def test_replay_command_steps_to_local_time_and_prints_runs(
    committed_session_factory, settings, tmp_path, monkeypatch
) -> None:
    from typer.testing import CliRunner

    from farmcore.cli import cli
    from farmcore.core.clock import get_clock
    from farmcore.core.models.kernel import ClockState
    from farmcore.settings import get_settings

    pkg = _fake_app_package(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)  # no .env: the command reads DATABASE_URL from the env
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        result = runner.invoke(cli, ["replay", "--to", "2024-04-10 06:00", "--app-package", pkg])
        assert result.exit_code == 0, result.output
        assert "10 Apr 2024 06:00" in result.output
        assert "orchard.job" in result.output and "ok" in result.output
        assert [c[1] for c in fake_jobs.CALLS] == [T0]
        with committed_session_factory() as s:
            assert get_clock(s, settings).now() == T0

        live = runner.invoke(cli, ["replay", "--live"])
        assert live.exit_code == 0, live.output
        with committed_session_factory() as s:
            assert s.get(ClockState, 1).mode == "live"
    finally:
        get_settings.cache_clear()


def test_replay_command_skips_jobs_of_uninstalled_apps(
    committed_session_factory, settings, tmp_path, monkeypatch
) -> None:
    """`farmcore replay` runs the same jobs as the navbar step: installed apps only."""
    from typer.testing import CliRunner

    from farmcore.cli import cli
    from farmcore.core.models.kernel import AppModule
    from farmcore.settings import get_settings

    pkg = _fake_app_package(tmp_path, monkeypatch)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    with committed_session_factory() as s:
        s.merge(AppModule(name="orchard", version="0.1.0", state="uninstalled"))
        s.commit()
    try:
        result = CliRunner().invoke(
            cli, ["replay", "--to", "2024-04-10 06:00", "--app-package", pkg]
        )
        assert result.exit_code == 0, result.output
        assert "orchard.job" not in result.output
        assert "1 job(s) ran" in result.output  # only core's drafts.expire
        assert fake_jobs.CALLS == []
    finally:
        get_settings.cache_clear()
        with committed_session_factory() as s:
            s.query(AppModule).filter_by(name="orchard").delete()
            s.commit()


def test_run_now_waits_for_clock_coordinator_before_reading_time(
    committed_session_factory, settings
):
    from sqlalchemy import text

    from farmcore.core.clock import set_replay
    from farmcore.db import session_scope

    factory = committed_session_factory
    manifest = _manifest("a", "record")
    with session_scope(factory) as session:
        set_replay(session, T0, settings)
    started = threading.Event()
    errors = []

    def immediate():
        started.set()
        try:
            jobs.run_now(factory, [manifest], "a.job", settings)
        except Exception as exc:
            errors.append(type(exc).__name__)

    thread = threading.Thread(target=immediate)
    try:
        with session_scope(factory) as coordinator:
            coordinator.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": jobs.STEP_LOCK_KEY}
            )
            thread.start()
            assert started.wait(timeout=5)
            thread.join(timeout=0.5)
            premature = list(fake_jobs.CALLS)
            with session_scope(factory) as session:
                set_replay(session, T0 + timedelta(days=1), settings)
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert premature == []
        assert errors == []
        assert [c[1] for c in fake_jobs.CALLS] == [T0 + timedelta(days=1)]
    finally:
        thread.join(timeout=5)


def test_run_now_in_live_queues_the_farm_and_answers_within_its_wait(
    committed_session_factory, settings
) -> None:
    """The chat confirm path must answer even when no worker runs: queued, not run in-process.
    With a worker it reports the evaluation that began after the call."""
    from sqlalchemy import text

    from farmcore.core import clock
    from farmcore.core.models.canonical import Farm
    from farmcore.db import session_scope
    from tests.task_drain import drain

    spec = JobSpec(
        "a.job", "tests.core.fake_jobs:record", timedelta(hours=1), "tests.core.fake_jobs:publish"
    )
    manifests = [Manifest(name="a", version="0.1.0", jobs=(spec,))]
    with session_scope(committed_session_factory) as session:
        farm = Farm(name="Live")
        session.add(farm)
        clock.set_live(session, settings)

    pending = jobs.run_now(
        committed_session_factory, manifests, "a.job", settings, farm_ids=(farm.id,), wait=0.2
    )
    assert pending is not None and not pending.ok and fake_jobs.CALLS == []
    with committed_session_factory() as session:
        queued = session.execute(text("SELECT count(*) FROM procrastinate_jobs")).scalar_one()
    assert queued == 1
    # An evaluation that began before the call (it may not see the new log) does not count.
    assert drain(committed_session_factory, settings, manifests) == 1
    stale = jobs.run_now(
        committed_session_factory, manifests, "a.job", settings, farm_ids=(farm.id,), wait=0.2
    )
    assert stale is not None and not stale.ok

    # A worker drains the queue while run_now waits: the answer is the evaluation it began after.
    stop = threading.Event()

    def worker() -> None:
        while not stop.wait(0.02):
            drain(committed_session_factory, settings, manifests)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        done = jobs.run_now(
            committed_session_factory, manifests, "a.job", settings, farm_ids=(farm.id,), wait=10
        )
    finally:
        stop.set()
        thread.join(timeout=5)
    assert done is not None and done.ok


def test_the_worker_runs_each_live_job_once_per_interval_and_never_while_replaying(
    session_factory, settings, monkeypatch
) -> None:
    """Installation-wide jobs (no `task`) run on the worker. Their schedule is persisted, so a
    restarted or second worker neither repeats nor forgets one; a job is first due one interval
    after the worker first sees it, as under the old in-process scheduler."""
    from farmcore.core.clock import LiveClock, set_live, set_replay
    from farmcore.db import session_scope

    manifests = [_manifest("a", "record")]
    wall = [datetime(2026, 11, 19, 8, 0, tzinfo=UTC)]
    monkeypatch.setattr(LiveClock, "now", lambda self: wall[0])
    with session_scope(session_factory) as session:
        set_live(session, settings)

    def ran(after: timedelta) -> list[str]:
        wall[0] += after
        return [r.name for r in jobs.run_due_live(session_factory, manifests, settings)]

    assert ran(timedelta(0)) == []  # first seen
    assert ran(timedelta(minutes=59)) == []
    assert ran(timedelta(minutes=1)) == ["a.job"]
    assert ran(timedelta(0)) == []  # another worker, or a restart, at the same time
    with session_scope(session_factory) as session:
        set_replay(session, T0, settings)
    assert ran(timedelta(hours=2)) == []
    assert [(label, now) for label, now, _ in fake_jobs.CALLS] == [
        ("record", datetime(2026, 11, 19, 9, 0, tzinfo=UTC))
    ]

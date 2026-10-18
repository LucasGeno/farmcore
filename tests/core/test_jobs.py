"""Manifest jobs: replay `step_to`, `run_now`, live APScheduler registration."""

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


def _started_app(settings, session_factory, mode: str):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    manifests = [_manifest("a", "record"), _manifest("b", "record_b", timedelta(minutes=15))]
    app = create_app(
        settings.model_copy(update={"clock_mode": mode}),
        manifests=manifests,
        session_factory=session_factory,
    )
    return app, TestClient(app)


def test_live_mode_registers_one_scheduler_job_per_jobspec(settings, session_factory) -> None:
    app, client = _started_app(settings, session_factory, "live")
    with client:
        scheduler = app.state.scheduler
        assert scheduler.running
        scheduled = {j.id: j.trigger.interval for j in scheduler.get_jobs()}
        assert scheduled == {"a.job": timedelta(hours=1), "b.job": timedelta(minutes=15)}
    assert not scheduler.running  # shutdown on lifespan exit
    assert fake_jobs.CALLS == []  # nothing ran at start


def test_live_jobs_follow_the_shared_clock_mode(settings, session_factory) -> None:
    """A step (web or CLI) switches clock_state to replay: live ticks then skip until live."""
    from farmcore.core.clock import set_live, set_replay
    from farmcore.db import session_scope

    app, client = _started_app(settings, session_factory, "live")
    with client:
        job = app.state.scheduler.get_job("a.job")
        with session_scope(session_factory) as s:
            set_replay(s, T0, settings)
        job.func(*job.args)
        assert fake_jobs.CALLS == []

        with session_scope(session_factory) as s:
            set_live(s, settings)
        job.func(*job.args)
        ((label, now, _),) = fake_jobs.CALLS
        assert label == "record" and abs(now - datetime.now(UTC)) < timedelta(seconds=5)


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
        assert "0 job(s) ran" in result.output
        assert fake_jobs.CALLS == []
    finally:
        get_settings.cache_clear()
        with committed_session_factory() as s:
            s.query(AppModule).filter_by(name="orchard").delete()
            s.commit()


def test_replay_mode_starts_no_scheduler(settings, session_factory) -> None:
    app, client = _started_app(settings, session_factory, "replay")
    with client:
        assert app.state.scheduler is None


@pytest.mark.parametrize("trigger", ["run_now", "live_tick"])
def test_immediate_jobs_wait_for_clock_coordinator_before_reading_time(
    committed_session_factory, settings, trigger
):
    from sqlalchemy import text

    from farmcore.core.clock import set_live, set_replay
    from farmcore.db import session_scope

    factory = committed_session_factory
    manifest = _manifest("a", "record")
    with session_scope(factory) as session:
        set_replay(session, T0, settings)
        if trigger == "live_tick":
            set_live(session, settings)
    started = threading.Event()
    errors = []

    def immediate():
        started.set()
        try:
            if trigger == "run_now":
                jobs.run_now(factory, [manifest], "a.job", settings)
            else:
                jobs._run_live(factory, manifest.jobs[0], settings)
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
        if trigger == "run_now":
            assert [c[1] for c in fake_jobs.CALLS] == [T0 + timedelta(days=1)]
        else:
            assert fake_jobs.CALLS == []  # tick sees replay after the coordinator released
    finally:
        thread.join(timeout=5)

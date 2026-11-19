"""Live advice on real `farmcore worker` processes: a worker killed mid-evaluation."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from farmcore.apps.advisor.advice import Advice
from farmcore.apps.advisor.manifest import JOB_NAME
from farmcore.apps.stations.seed import seed as seed_station
from farmcore.core import clock
from farmcore.core import seed as core_seed
from farmcore.core.logs import lock_farm
from farmcore.core.models.canonical import Farm, Parcel

WAIT = 15.0  # seconds; every wait below ends on an observable state, this only bounds a hang
PARCEL = "SRID=4326;MULTIPOLYGON(((-0.19 44.91,-0.18 44.91,-0.18 44.92,-0.19 44.91)))"


@pytest.fixture
def start_worker(settings, tmp_path) -> Iterator[Callable[[str], subprocess.Popen]]:
    started: list[subprocess.Popen] = []

    def start(name: str) -> subprocess.Popen:
        env = {
            **os.environ,
            "DATABASE_URL": settings.database_url,
            "DATA_DIR": str(tmp_path),
            "OPEN_METEO_BASE_URL": "http://127.0.0.1:9",  # refused: no forecast leaves the host
        }
        command = [sys.executable, "-c", "from farmcore.cli import main; main()", "worker"]
        flags = ["--concurrency", "4", "--tick", "0.2", "--stalled", "1", "--heartbeat", "0.2"]
        log = (tmp_path / f"{name}.log").open("w")
        process = subprocess.Popen([*command, *flags], env=env, stdout=log, stderr=log)
        started.append(process)
        return process

    yield start
    for process in started:
        if process.poll() is None:
            process.kill()
        process.wait()


def _until(check: Callable[[], object], what: str) -> object:
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if value := check():
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.mark.slow  # two worker processes and a stall timeout: about 10 s
def test_a_worker_killed_mid_evaluation_is_retried_and_publishes_once(
    committed_session_factory, settings, monkeypatch, tmp_path, start_worker
) -> None:
    factory = committed_session_factory
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
    with factory.begin() as session:
        core_seed.seed(session, scene=None, data_dir=tmp_path)
        seed_station(session, scene=None, data_dir=tmp_path)
        farm = core_seed.ensure_default_farm(session, settings)
        session.add(Parcel(farm_id=farm.id, name="P", alias="P1", area_ha=Decimal(1), geom=PARCEL))
        other = Farm(name="Other farm")
        session.add(other)
        clock.set_live(session, settings)

    def attempt(farm_id: int) -> tuple[int, bool] | None:
        with factory() as session:
            row = session.execute(
                text(
                    "SELECT attempt, finished_at IS NOT NULL FROM task_attempt "
                    "WHERE task_name = :n AND farm_id = :f"
                ),
                {"n": JOB_NAME, "f": farm_id},
            ).first()
        return tuple(row) if row else None

    def waiting_on_the_farm_lock() -> int:
        with factory() as session:
            return session.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event = 'advisory'"
                )
            ).scalar_one()

    # The farm's operations lock is held, so its evaluation stops inside its transaction.
    with factory() as holder:
        lock_farm(holder, farm.id)
        first = start_worker("first")
        _until(waiting_on_the_farm_lock, "the first worker mid-evaluation")
        assert attempt(farm.id) == (1, False)
        # Meanwhile another farm is evaluated.
        _until(lambda: attempt(other.id) == (1, True), "the other farm's evaluation")

        first.send_signal(signal.SIGKILL)
        first.wait()
        start_worker("second")
        _until(lambda: attempt(farm.id) == (2, False), "the retry on the second worker")
        holder.rollback()

    _until(lambda: attempt(farm.id) == (2, True), "the retried evaluation's publication")
    with factory() as session:
        advice = session.scalar(select(func.count(Advice.id)).where(Advice.farm_id == farm.id))
    assert advice == 1  # one parcel, evaluated once: the killed run left nothing

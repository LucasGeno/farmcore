"""The live forecast fetch as a durable task: real `farmcore worker` processes against a loopback
provider stub, and the in-process seams for planning and the live/replay switch."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select, text

from farmcore.apps.forecast.manifest import MANIFEST
from farmcore.apps.forecast.models import ForecastRun
from farmcore.core import clock, tasks
from farmcore.core.jobs import run_now
from farmcore.core.models.canonical import Farm
from tests.apps.forecast.conftest import fetch_live_now, provider_client

JOB = "forecast.fetch_live"
WAIT = 15.0  # seconds; every wait below ends on an observable state, this only bounds a hang


def _payload() -> dict:
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    hours = [(now + timedelta(hours=i)).isoformat() for i in range(4)]
    return {
        "latitude": 44.91,
        "longitude": -0.21,
        "utc_offset_seconds": 0,
        "hourly_units": {"precipitation": "mm", "temperature_2m": "°C"},
        "hourly": {
            "time": hours,
            "precipitation": [0, 1.5, 0, 2.0],
            "temperature_2m": [12, 13, 14, 15],
        },
    }


class Provider:
    """Loopback Open-Meteo stub. `hold(latitude)` keeps that farm's next request open until
    `release()`; every other request answers at once. `seen` lists (latitude, time) per request."""

    def __init__(self) -> None:
        self.seen: list[tuple[float, float]] = []
        self.held: set[float] = set()
        self.released = threading.Event()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                latitude = float(parse_qs(urlsplit(self.path).query)["latitude"][0])
                stub.seen.append((latitude, time.monotonic()))
                if latitude in stub.held:
                    stub.held.discard(latitude)
                    stub.released.wait(WAIT * 2)
                body = json.dumps(_payload()).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # the worker gave up on this request

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def hold(self, latitude: float) -> None:
        self.held.add(latitude)

    def release(self) -> None:
        self.released.set()

    def requests_for(self, latitude: float) -> list[float]:
        return [t for lat, t in self.seen if lat == latitude]


@pytest.fixture
def provider() -> Iterator[Provider]:
    stub = Provider()
    yield stub
    stub.release()
    stub.server.shutdown()


@pytest.fixture
def start_worker(settings, provider, tmp_path) -> Iterator[Callable[..., subprocess.Popen]]:
    started: list[subprocess.Popen] = []

    def start(name: str, *, fetch_timeout: float = 10.0) -> subprocess.Popen:
        env = {
            **os.environ,
            "DATABASE_URL": settings.database_url,
            "OPEN_METEO_BASE_URL": provider.url,
            "FORECAST_FETCH_TIMEOUT_SECONDS": str(fetch_timeout),
        }
        command = [sys.executable, "-c", "from farmcore.cli import main; main()", "worker"]
        flags = ["--concurrency", "2", "--tick", "0.2", "--stalled", "1", "--heartbeat", "0.2"]
        log = (tmp_path / f"{name}.log").open("w")
        process = subprocess.Popen([*command, *flags], env=env, stdout=log, stderr=log)
        process.log = tmp_path / f"{name}.log"  # type: ignore[attr-defined]
        started.append(process)
        return process

    yield start
    for process in started:
        if process.poll() is None:
            process.send_signal(signal.SIGCONT)
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


def _live_farms(factory, settings, *latitudes: float) -> list[int]:
    with factory.begin() as session:
        farms = [
            Farm(name=f"Farm {lat}", props={"forecast_location": {"latitude": lat, "longitude": 0}})
            for lat in latitudes
        ]
        session.add_all(farms)
        clock.set_live(session, settings)
    return [farm.id for farm in farms]


def _health(factory, farm_id: int) -> dict:
    with factory() as session:
        return session.get(Farm, farm_id).props.get("forecast_health") or {}


def _receipts(factory, farm_id: int) -> int:
    with factory() as session:
        return len(session.scalars(select(ForecastRun).where(ForecastRun.farm_id == farm_id)).all())


@pytest.mark.slow  # a worker process and a provider timeout: about 5 s
def test_a_stalled_provider_for_one_farm_does_not_delay_another(
    committed_session_factory, settings, provider, start_worker
) -> None:
    provider.hold(10.0)
    slow, fast = _live_farms(committed_session_factory, settings, 10.0, 20.0)
    start_worker("w", fetch_timeout=3.0)

    _until(lambda: _receipts(committed_session_factory, fast), "farm B's receipt")
    # B published while A's request is still open, and within a normal round trip of B's own.
    assert provider.requests_for(10.0) and "last_attempt" not in _health(
        committed_session_factory, slow
    )
    assert time.monotonic() - provider.requests_for(20.0)[0] < 2.0

    # A's failure is recorded once its deadline passes, never silently dropped.
    health = _until(
        lambda: (
            _health(committed_session_factory, slow).get("error")
            and _health(committed_session_factory, slow)
        ),
        "farm A's recorded failure",
    )
    assert health["error"] == "TimeoutError" and "last_success" not in health
    assert _receipts(committed_session_factory, slow) == 0


@pytest.mark.slow  # two worker processes and a stall timeout: about 10 s
def test_a_stalled_worker_s_fetch_is_retried_and_its_late_result_is_rejected(
    committed_session_factory, settings, provider, start_worker
) -> None:
    provider.hold(30.0)
    (farm,) = _live_farms(committed_session_factory, settings, 30.0)
    first = start_worker("first")
    _until(lambda: provider.requests_for(30.0), "the first worker's request")
    first.send_signal(signal.SIGSTOP)  # no heartbeat from now on: presumed dead

    start_worker("second")
    _until(lambda: _receipts(committed_session_factory, farm), "the retried fetch's receipt")
    assert len(provider.requests_for(30.0)) == 2

    # The first worker resumes and gets its answer: the fence rejects it at commit.
    provider.release()
    first.send_signal(signal.SIGCONT)
    _until(lambda: "stale attempt 1" in first.log.read_text(), "the first worker's rejection")
    assert _receipts(committed_session_factory, farm) == 1
    with committed_session_factory() as session:
        assert (
            session.execute(
                text("SELECT attempt FROM task_attempt WHERE farm_id = :f"), {"f": farm}
            ).scalar_one()
            == 2
        )


def test_running_the_live_job_only_enqueues_one_fetch_per_farm(
    session, session_factory, settings, monkeypatch
) -> None:
    """What a request handler (`run_now`) or a worker tick does: no provider call, one queued
    fetch per due farm however often it runs, none while that farm's fetch is in flight."""
    calls = []
    monkeypatch.setattr(httpx, "AsyncClient", provider_client(lambda url, **kw: calls.append(url)))
    farms = _live_farms(session_factory, settings, 44.9, 45.9)

    def queued() -> list[int]:
        return sorted(
            session.execute(
                text("SELECT (args->>'farm_id')::int FROM procrastinate_jobs WHERE status = 'todo'")
            ).scalars()
        )

    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    assert calls == [] and queued() == sorted(farms)

    session.execute(text("DELETE FROM procrastinate_jobs"))
    tasks.begin_attempt(session_factory, JOB, farms[0], timedelta(minutes=1))
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    assert queued() == [farms[1]]


def test_switching_to_replay_mid_fetch_publishes_nothing(
    session, session_factory, settings, monkeypatch
) -> None:
    (farm,) = _live_farms(session_factory, settings, 44.9)

    def state() -> str:
        rows = session.execute(
            text(
                "SELECT coalesce(json_agg(r ORDER BY r.id), '[]')::text FROM forecast_run r "
                "UNION ALL SELECT props::text FROM farm WHERE id = :f"
            ),
            {"f": farm},
        ).scalars()
        return "\n".join(rows)

    baseline = state()
    calls = []

    def provider(url, **kwargs):
        calls.append(url)
        with session_factory() as other:
            clock.set_replay(other, datetime(2024, 4, 10, tzinfo=UTC), settings)
            other.commit()
        return httpx.Response(200, request=httpx.Request("GET", url), json=_payload())

    monkeypatch.setattr(httpx, "AsyncClient", provider_client(provider))
    assert fetch_live_now(session_factory, settings).ok
    session.expire_all()
    assert len(calls) == 1 and state() == baseline

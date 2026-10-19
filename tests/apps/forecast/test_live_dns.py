"""The live job bounds native DNS residue without reviving canceled HTTP requests."""

import json
import socket
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from threading import enumerate as threads

import httpx
from sqlalchemy import select

from farmcore.apps.forecast.manifest import MANIFEST
from farmcore.apps.forecast.models import ForecastRun
from farmcore.apps.forecast.sources import Run
from farmcore.apps.forecast.store import import_runs
from farmcore.core import clock
from farmcore.core.jobs import run_now
from farmcore.core.models.canonical import Farm

JOB = "forecast.fetch_live"


def test_stalled_dns_has_fixed_capacity_fails_fast_recovers_and_never_revives_http(
    session, session_factory, settings, monkeypatch
):
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    payload = json.dumps(
        {
            "latitude": 44.9,
            "longitude": -0.2,
            "utc_offset_seconds": 0,
            "hourly_units": {"precipitation": "mm", "temperature_2m": "°C"},
            "hourly": {
                "time": [(now + timedelta(hours=i)).isoformat() for i in range(4)],
                "precipitation": [1.5] * 4,
                "temperature_2m": [12.0] * 4,
            },
        }
    ).encode()
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    serving = Thread(target=server.serve_forever)
    serving.start()
    release, complete, fail_once = Event(), Event(), Event()
    guard = Lock()
    lookup_calls = []
    active, maximum, finished = 0, 0, 0
    native_dns = socket.getaddrinfo
    original_client = httpx.AsyncClient

    def stalled_dns(*args, **kwargs):
        nonlocal active, maximum, finished
        with guard:
            lookup_calls.append(args)
            active += 1
            maximum = max(maximum, active)
        try:
            if fail_once.is_set():
                fail_once.clear()
                raise socket.gaierror(socket.EAI_AGAIN, "controlled provider DNS failure")
            # Safety limit makes the original executor-shutdown bug fail, never hang.
            release.wait(3)
            return native_dns("127.0.0.1", server.server_port, type=socket.SOCK_STREAM)
        finally:
            with guard:
                active -= 1
                finished += 1
                if finished == 2:
                    complete.set()

    class LocalTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.network = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):
            request.url = request.url.copy_with(
                scheme="http",
                host="forecast-dns.invalid",
                port=server.server_port,
            )
            return await self.network.handle_async_request(request)

        async def aclose(self):
            await self.network.aclose()

    try:
        monkeypatch.setattr(socket, "getaddrinfo", stalled_dns)
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda **kw: original_client(transport=LocalTransport(), trust_env=False, **kw),
        )
        configured = settings.model_copy(update={"forecast_fetch_timeout_seconds": 0.1})
        farms = [
            Farm(
                name=f"DNS farm {i}",
                props={"forecast_location": {"latitude": 44.9 + i, "longitude": -0.2}},
            )
            for i in range(5)
        ]
        session.add_all(farms)
        session.flush()
        import_runs(
            session,
            farms[0].id,
            [
                Run(
                    "prior",
                    now - timedelta(hours=1),
                    "Prior valid forecast",
                    True,
                    44.9,
                    -0.2,
                    [(now + timedelta(hours=1), "rr1_mm", 1.5)],
                )
            ],
        )
        prior = session.scalar(select(ForecastRun))
        prior_id, prior_props = prior.id, dict(prior.props)
        clock.set_live(session, configured)
        session.commit()
        started = time.monotonic()
        result = run_now(session_factory, [MANIFEST], JOB, configured)
        elapsed = time.monotonic() - started
        assert result.ok, result.error
        assert elapsed < 0.55, "native DNS completion must not hold the job's global lock"
        assert len(lookup_calls) == 2, "two total slots; saturated farms must fail immediately"
        assert maximum <= 2
        assert sum(t.name.startswith("forecast-dns-") for t in threads()) == 2
        session.expire_all()
        assert [row.id for row in session.scalars(select(ForecastRun)).all()] == [prior_id]
        assert session.get(ForecastRun, prior_id).props == prior_props
        assert all("error" in farm.props["forecast_health"] for farm in farms)
        assert farms[0].props["forecast_health"]["error"] == "TimeoutError"
        assert farms[-1].props["forecast_health"]["error"] == "ConnectError"
        assert requests == []
        # Admission stays occupied after await cancellation until native work completes.
        for farm in farms:
            farm.props = {
                **farm.props,
                "forecast_health": {"last_attempt": "2020-01-01T00:00:00+00:00"},
            }
        session.commit()
        again = time.monotonic()
        assert run_now(session_factory, [MANIFEST], JOB, configured).ok
        assert time.monotonic() - again < 0.25
        assert len(lookup_calls) == 2
        release.set()
        assert complete.wait(1)
        # Wait for both workers to retire their completions and discard closed-loop delivery.
        time.sleep(0.05)
        assert requests == [], "finishing native DNS must never restart a canceled HTTP request"
        session.expire_all()
        for farm in farms:
            farm.props = {
                **farm.props,
                "forecast_health": {"last_attempt": "2020-01-01T00:00:00+00:00"},
            }
        session.commit()
        assert run_now(session_factory, [MANIFEST], JOB, configured).ok
        session.expire_all()
        assert all("last_success" in farm.props["forecast_health"] for farm in farms), [
            farm.props["forecast_health"] for farm in farms
        ]
        assert len(requests) == 5
        # A real resolver error also releases its slot, so the same farm can retry.
        fail_once.set()
        farms[0].props = {
            **farms[0].props,
            "forecast_health": {"last_attempt": "2020-01-01T00:00:00+00:00"},
        }
        session.commit()
        assert run_now(session_factory, [MANIFEST], JOB, configured).ok
        session.expire_all()
        assert farms[0].props["forecast_health"]["error"] == "ConnectError"
        assert len(requests) == 5
        farms[0].props = {
            **farms[0].props,
            "forecast_health": {"last_attempt": "2020-01-01T00:00:00+00:00"},
        }
        session.commit()
        assert run_now(session_factory, [MANIFEST], JOB, configured).ok
        session.expire_all()
        assert "last_success" in farms[0].props["forecast_health"]
        assert len(requests) == 6
        assert maximum <= 2
        assert sum(t.name.startswith("forecast-dns-") for t in threads()) == 2
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


def test_native_dns_failure_is_per_farm_and_keeps_saved_forecast(
    session, session_factory, settings, farm, monkeypatch
):
    calls = []
    original_client = httpx.AsyncClient

    def unavailable(*args, **kwargs):
        calls.append(args)
        raise socket.gaierror(socket.EAI_AGAIN, "controlled provider DNS failure")

    monkeypatch.setattr(socket, "getaddrinfo", unavailable)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_client(trust_env=False, **kw))
    now = datetime.now(UTC)
    farm.props = {"forecast_location": {"latitude": 44.9, "longitude": -0.2}}
    import_runs(
        session,
        farm.id,
        [
            Run(
                "prior",
                now - timedelta(hours=1),
                "Prior valid forecast",
                True,
                44.9,
                -0.2,
                [(now + timedelta(hours=1), "rr1_mm", 1.5)],
            )
        ],
    )
    prior = session.scalar(select(ForecastRun))
    prior_id, prior_props = prior.id, dict(prior.props)
    clock.set_live(session, settings)
    session.commit()
    result = run_now(session_factory, [MANIFEST], JOB, settings)
    assert result.ok, result.error
    session.expire_all()
    held = session.scalars(select(ForecastRun)).all()
    assert [row.id for row in held] == [prior_id]
    assert held[0].props == prior_props
    assert farm.props["forecast_health"]["error"] == "ConnectError"
    assert len(calls) == 1

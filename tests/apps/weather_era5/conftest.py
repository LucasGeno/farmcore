"""The Copernicus CDS, faked from its documented job API: submit, job status, results, then the
CSV from a second host. Recorded shapes only; no network."""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from farmcore.apps.weather_era5 import ingest
from farmcore.apps.weather_era5 import provider as live
from farmcore.core.access import ensure_owner_grant
from farmcore.core.models.canonical import Farm

FIXTURES = Path(__file__).parent / "fixtures"
KEY = "0a1b2c3d-4e5f-6789-abcd-ef0123456789"
DATASET = "reanalysis-era5-single-levels-timeseries"
CSV_HOST = "object-store.example.org"
PIN = {"latitude": 44.9, "longitude": -0.2}


class Cds:
    """Answers like the CDS. `queued` polls answer `running` before the job succeeds (a number
    far beyond the test's deadline: it never does); `fail` names a step (`submit`, `job`) that
    errs; `throttle` submits answer 429 with Retry-After first."""

    def __init__(self, queued: int = 1, fail: str | None = None, throttle: int = 0) -> None:
        self.queued, self.fail, self.throttle = queued, fail, throttle
        self.submitted: list[dict] = []
        self.dates: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.url.host == CSV_HOST:
            assert "private-token" not in request.headers  # the key stays on the CDS host
            return httpx.Response(200, content=self._zip())
        if request.headers.get("private-token") != KEY or self.fail == "submit":
            return httpx.Response(403, json={"title": "authentication failed"})
        if path.endswith(f"/processes/{DATASET}/constraints"):
            body = json.loads((FIXTURES / "constraints.json").read_text())
            newest = datetime.now(UTC) - timedelta(days=10)  # the copy lags real time
            body["date"] = [f"1940-01-01/{newest:%Y-%m-%d}"]
            return httpx.Response(200, json=body)
        if path.endswith(f"/processes/{DATASET}/execution") and request.method == "POST":
            if self.throttle:
                self.throttle -= 1
                return httpx.Response(429, headers={"Retry-After": "0"})
            inputs = json.loads(request.content)["inputs"]
            assert inputs["data_format"] == "csv" and set(inputs) >= {"variable", "location"}
            self.submitted.append(inputs)
            self.dates = inputs["date"][0].split("/")
            return httpx.Response(201, content=(FIXTURES / "job-accepted.json").read_bytes())
        if "/jobs/" in path and path.endswith("/results"):
            return httpx.Response(200, content=(FIXTURES / "job-results.json").read_bytes())
        if "/jobs/" in path:
            status = {"status": "running"}
            if self.fail == "job":
                status = {"status": "failed", "message": "the request is too large"}
            elif self.queued <= 0:
                status = {"status": "successful"}
            self.queued -= 1
            return httpx.Response(200, json={"jobID": path.rsplit("/", 1)[1], **status})
        return httpx.Response(404)

    def _zip(self) -> bytes:
        """The asset: a zip of two CSVs (instantaneous and accumulated parameters), one row per
        hour of the requested days, from the recorded rows."""
        first, last = (datetime.strptime(d, "%Y-%m-%d") for d in self.dates)
        hours = [first + timedelta(hours=i) for i in range(((last - first).days + 1) * 24)]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for member, fixture in (("instant", "era5-instant-row"), ("accum", "era5-accum-row")):
                header, row = (FIXTURES / f"{fixture}.csv").read_text().splitlines()
                rest = row.split(",", 1)[1]
                lines = [f"{h:%Y-%m-%d %H:%M:%S},{rest}" for h in hours]
                archive.writestr(f"data_stream-{member}.csv", "\n".join([header, *lines]) + "\n")
        return buffer.getvalue()


@pytest.fixture
def cds(monkeypatch) -> Callable[..., Cds]:
    """Route the provider's HTTP to a fake CDS; polls and waits do not sleep."""
    monkeypatch.setattr(live, "POLL_SECONDS", 0.0)
    # Two whole days to fill: the fake's constraints end 10 days back, the season starts 11.
    monkeypatch.setattr(
        ingest, "season_start", lambda now: (now - timedelta(days=11)).replace(hour=0, minute=0)
    )
    monkeypatch.setattr(ingest, "FALLBACK_LAG", timedelta(days=10))
    real = httpx.Client

    def install(**kw) -> Cds:
        fake = Cds(**kw)
        monkeypatch.setattr(
            live.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(fake), **kw)
        )
        return fake

    return install


@pytest.fixture
def credentials(settings):
    return settings.model_copy(update={"cdsapi_key": SecretStr(KEY)})


@pytest.fixture
def farm(session) -> Farm:
    farm = Farm(name="Pin farm", props={"forecast_location": PIN})
    session.add(farm)
    session.flush()
    ensure_owner_grant(session, farm)
    session.commit()
    return farm


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)

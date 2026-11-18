"""Configurable Open-Meteo access: public, customer API with a key, or a self-hosted server.

The key is a query parameter on the wire but never part of stored provenance, the UI or an
error message.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select

from farmcore.apps.forecast import sources
from farmcore.apps.forecast.manifest import MANIFEST
from farmcore.apps.forecast.models import ForecastRun
from farmcore.core import clock
from farmcore.core.jobs import run_now
from farmcore.settings import Settings
from tests.apps.forecast.conftest import provider_client

JOB = "forecast.fetch_live"


@pytest.fixture
def live_payload():
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    return {
        "latitude": 44.91,
        "longitude": -0.21,
        "utc_offset_seconds": 0,
        "hourly_units": {
            "precipitation": "mm",
            "temperature_2m": "°C",
            "relative_humidity_2m": "%",
            "wind_speed_10m": "km/h",
            "wind_gusts_10m": "km/h",
        },
        "hourly": {
            "time": [(now + timedelta(hours=i)).isoformat() for i in range(4)],
            "precipitation": [None, 1.5, 0, 2.0],
            "temperature_2m": [12, 13, 14, 15],
            "relative_humidity_2m": [80, 81, 82, 83],
            "wind_speed_10m": [5, 6, 7, 8],
            "wind_gusts_10m": [9, 10, 11, 12],
        },
    }


def test_customer_base_url_sends_key_and_records_kind_without_the_key(
    client, session, session_factory, settings, farm, monkeypatch, live_payload
):
    key = "customer-secret-key"
    configured = settings.model_copy(
        update={
            "open_meteo_base_url": "https://customer-api.open-meteo.com",
            "open_meteo_api_key": SecretStr(key),
        }
    )
    farm.props = {"forecast_location": {"latitude": 44.9, "longitude": -0.2}}
    clock.set_live(session, configured)
    session.commit()
    calls = []

    def provider(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url), json=live_payload)

    monkeypatch.setattr(httpx, "AsyncClient", provider_client(provider))
    assert run_now(session_factory, [MANIFEST], JOB, configured).ok
    session.expire_all()
    assert len(calls) == 1
    parts = urlsplit(calls[0])
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "customer-api.open-meteo.com",
        "/v1/forecast",
    )
    assert parse_qs(parts.query)["apikey"] == [key]
    row = session.scalar(select(ForecastRun).where(ForecastRun.farm_id == farm.id))
    assert row is not None
    assert row.props["endpoint_kind"] == "customer"
    assert key not in json.dumps(row.props)
    assert key not in client.get("/forecast/home-weather", params={"farm": farm.id}).text


def test_self_hosted_base_url_needs_no_key_and_records_self_hosted(
    session, session_factory, settings, farm, monkeypatch, live_payload
):
    configured = settings.model_copy(
        update={"open_meteo_base_url": "http://openmeteo:8080", "open_meteo_api_key": None}
    )
    farm.props = {"forecast_location": {"latitude": 44.9, "longitude": -0.2}}
    clock.set_live(session, configured)
    session.commit()
    calls = []

    def provider(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url), json=live_payload)

    monkeypatch.setattr(httpx, "AsyncClient", provider_client(provider))
    assert run_now(session_factory, [MANIFEST], JOB, configured).ok
    session.expire_all()
    parts = urlsplit(calls[0])
    assert (parts.scheme, parts.netloc, parts.path) == ("http", "openmeteo:8080", "/v1/forecast")
    assert "apikey" not in parse_qs(parts.query)
    row = session.scalar(select(ForecastRun).where(ForecastRun.farm_id == farm.id))
    assert row is not None
    assert row.props["endpoint_kind"] == "self-hosted"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("open_meteo_base_url", "ftp://api.open-meteo.com"),
        ("open_meteo_base_url", "not-a-url"),
        ("open_meteo_base_url", "https://user:password@api.open-meteo.com"),
        ("open_meteo_single_runs_base_url", "file:///tmp/open-meteo"),
        ("open_meteo_single_runs_base_url", "https://key@single-runs-api.open-meteo.com"),
    ],
)
def test_open_meteo_urls_must_be_http_without_credentials(field, value):
    with pytest.raises(ValidationError, match=field):
        Settings(_env_file=None, **{field: value})


def test_single_runs_http_error_does_not_echo_the_key(monkeypatch):
    def unauthorized(url, **kwargs):
        return httpx.Response(401, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", unauthorized)
    with pytest.raises(ValueError) as exc:
        sources.get_json(
            "https://customer-single-runs-api.open-meteo.com/v1/forecast?apikey=top-secret"
        )
    assert "top-secret" not in str(exc.value)

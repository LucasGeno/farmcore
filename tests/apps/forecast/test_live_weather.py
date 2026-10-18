"""Live job and HTTP contracts; only the external provider is replaced."""

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import select

from farmcore.apps.forecast.manifest import MANIFEST
from farmcore.apps.forecast.models import ForecastRun
from farmcore.core import clock
from farmcore.core.jobs import run_now

JOB = "forecast.fetch_live"


def test_live_job_stores_bounded_receipt_with_units_location_and_rain(
    session, session_factory, settings, farm, monkeypatch, live_payload
):
    farm.props = {"forecast_location": {"latitude": 44.9, "longitude": -0.2}}
    clock.set_live(session, settings)
    session.commit()
    calls = []

    def provider(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(200, request=httpx.Request("GET", url), json=live_payload)

    monkeypatch.setattr(httpx, "get", provider)
    result = run_now(session_factory, [MANIFEST], JOB, settings)
    assert result is not None, "manifest must schedule the live ingestion job"
    assert result.ok, result.error
    session.expire_all()
    row = session.scalar(select(ForecastRun).where(ForecastRun.farm_id == farm.id))
    assert row is not None
    assert row.props["provider_issued_at"] is None
    assert row.props["issued_at_kind"] == "receipt"
    assert row.props["as_issued"] is False
    assert row.props["requested_location"] == {"latitude": 44.9, "longitude": -0.2}
    assert row.props["latitude"] == 44.91
    assert row.props["units"]["precipitation"] == "mm"
    assert row.props["fetched_at"] == row.available_at.isoformat()
    assert row.props["coverage"]["rr1_mm"]["known"] == 3
    assert len(calls) == 1 and calls[0][1]["timeout"] <= 10
    query = parse_qs(urlsplit(calls[0][0]).query)
    assert query["forecast_days"] == ["6"] and query["timezone"] == ["UTC"]
    assert query["wind_speed_unit"] == ["kmh"]
    # Cached receipt is immutable, even if invoked again in the same scheduling interval.
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    assert len(calls) == 1
    from farmcore.apps.forecast.store import latest_run
    from farmcore.core.access import repo_for_job

    run = latest_run(repo_for_job(session, farm.id, JOB), row.available_at)
    assert [o.rr1_mm for o in run.series] == [None, 1.5, 0.0, 2.0]


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


@pytest.mark.parametrize(
    "invalid",
    [
        "units",
        "nan",
        "shape",
        "oversized",
        "no_future",
        "offset",
        "hourly_shape",
        "units_shape",
        "coordinates",
        "negative_rain",
        "humidity_range",
        "time_element",
        "overflow",
        "time_array_object",
        "consecutive_old_only",
        "empty_values",
        "all_null_values",
        "excess_response_bytes",
    ],
)
def test_invalid_provider_response_preserves_last_good_receipt(
    session, session_factory, settings, farm, monkeypatch, live_payload, invalid
):
    from copy import deepcopy

    from farmcore.core.models.canonical import Farm

    farm.props = {"forecast_location": {"latitude": 44.9, "longitude": -0.2}}
    clock.set_live(session, settings)
    session.commit()
    payload = live_payload
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url), json=payload),
    )
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    session.expire_all()
    good = session.scalar(select(ForecastRun).where(ForecastRun.farm_id == farm.id))
    good_id, good_props = good.id, deepcopy(good.props)
    # Expire the job's shared cache through persisted farm state, not an internal mock.
    farm = session.get(Farm, farm.id)
    farm.props = {**farm.props, "forecast_health": {"last_attempt": "2020-01-01T00:00:00+00:00"}}
    session.commit()
    payload = deepcopy(live_payload)
    if invalid == "units":
        payload["hourly_units"]["wind_speed_10m"] = "m/s"
    elif invalid == "nan":
        payload["hourly"]["precipitation"][1] = "NaN"
    elif invalid == "shape":
        payload["hourly"]["precipitation"] = [0]
    elif invalid == "oversized":
        payload["hourly"]["time"] *= 100
    elif invalid == "no_future":
        payload["hourly"]["time"] = ["2020-01-01T00:00:00+00:00"] * 4
    elif invalid == "offset":
        payload["utc_offset_seconds"] = 7200
    elif invalid == "hourly_shape":
        payload["hourly"] = "bad"
    elif invalid == "units_shape":
        payload["hourly_units"] = "bad"
    elif invalid == "coordinates":
        payload["latitude"] = 1000
    elif invalid == "negative_rain":
        payload["hourly"]["precipitation"][1] = -1
    elif invalid == "humidity_range":
        payload["hourly"]["relative_humidity_2m"][1] = 150
    elif invalid == "time_element":
        payload["hourly"]["time"][1] = 42
    elif invalid == "overflow":
        payload["hourly"]["wind_speed_10m"][1] = 10**400
    elif invalid == "time_array_object":
        payload["hourly"]["time"] = {"0": "2020-01-01T00:00:00+00:00"}
    elif invalid == "consecutive_old_only":
        payload["hourly"]["time"] = [f"2020-01-01T0{i}:00:00+00:00" for i in range(4)]
    elif invalid == "empty_values":
        payload["hourly"] = {"time": payload["hourly"]["time"]}
    elif invalid == "all_null_values":
        for variable in payload["hourly"]:
            if variable != "time":
                payload["hourly"][variable] = [None] * 4
    elif invalid == "excess_response_bytes":
        payload["unused_provider_metadata"] = "x" * 256_001
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    session.expire_all()
    held = session.scalars(select(ForecastRun).where(ForecastRun.farm_id == farm.id)).all()
    assert [r.id for r in held] == [good_id]
    assert held[0].props == good_props
    assert session.get(Farm, farm.id).props["forecast_health"]["error"] == "ValueError"


def test_replay_rejects_future_issue_even_when_marked_available_and_never_fetches(
    client, session, session_factory, settings, farm, monkeypatch
):
    from farmcore.apps.forecast.sources import Run
    from farmcore.apps.forecast.store import import_runs

    now = datetime(2024, 4, 9, 6, tzinfo=UTC)
    values = [(now + timedelta(hours=1), "rr1_mm", 1.5)]
    import_runs(
        session,
        farm.id,
        [
            Run(
                "safe",
                now - timedelta(hours=2),
                "Safe saved forecast",
                True,
                None,
                None,
                values,
                available_at=now - timedelta(hours=2),
            ),
            Run(
                "future",
                now + timedelta(hours=2),
                "Future issued secret",
                True,
                None,
                None,
                values,
                available_at=now - timedelta(hours=1),
            ),
        ],
    )
    clock.set_replay(session, now, settings)
    session.commit()

    def forbidden(*args, **kwargs):
        raise AssertionError("replay must never request current weather")

    monkeypatch.setattr(httpx, "get", forbidden)
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    html = client.get("/forecast/home-weather", params={"farm": farm.id}).text
    assert "Safe saved forecast" in html
    assert "Future issued secret" not in html


def test_receipt_http_labels_fetch_time_unknown_issue_and_stale_partial_coverage(
    client, session, session_factory, settings, farm, monkeypatch, live_payload
):
    farm.props = {"forecast_location": {"latitude": 44.9, "longitude": -0.2}}
    clock.set_live(session, settings)
    session.commit()
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url), json=live_payload),
    )
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    session.expire_all()
    receipt = session.scalar(select(ForecastRun).where(ForecastRun.farm_id == farm.id))
    clock.set_replay(session, receipt.available_at + timedelta(minutes=1), settings)
    session.commit()
    html = client.get("/forecast/home-weather", params={"farm": farm.id}).text
    assert "Fetched" in html and "provider issue time unknown" in html
    assert "Forecast run " not in html
    assert "Partial forecast inputs" in html and "rain 3/4" in html
    clock.set_replay(session, receipt.available_at + timedelta(hours=2, minutes=1), settings)
    session.commit()
    for path in ("/forecast/home-weather", "/forecast/today", "/forecast/plan"):
        html = client.get(path, params={"farm": farm.id}).text
        assert "Stale forecast" in html, path
        assert "provider issue time unknown" in html, path
        assert "Forecast run " not in html, path
        assert "ECMWF IFS HRES" not in html, path


def test_identical_provider_retry_and_network_failure_preserve_receipt_and_report_health(
    client, session, session_factory, settings, farm, monkeypatch, live_payload
):
    from copy import deepcopy

    from farmcore.core.models.canonical import Farm

    farm.props = {"forecast_location": {"latitude": 44.9, "longitude": -0.2}}
    clock.set_live(session, settings)
    session.commit()
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url), json=live_payload),
    )
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    session.expire_all()
    good = session.scalar(select(ForecastRun).where(ForecastRun.farm_id == farm.id))
    original_id, original_props = good.id, deepcopy(good.props)
    for failure in (False, True):
        farm = session.get(Farm, farm.id)
        farm.props = {
            **farm.props,
            "forecast_health": {
                **farm.props["forecast_health"],
                "last_attempt": "2020-01-01T00:00:00+00:00",
            },
        }
        session.commit()
        if failure:

            def unavailable(*args, **kwargs):
                raise httpx.ConnectError("provider offline")

            monkeypatch.setattr(httpx, "get", unavailable)
        assert run_now(session_factory, [MANIFEST], JOB, settings).ok
        session.expire_all()
        held = session.scalars(select(ForecastRun).where(ForecastRun.farm_id == farm.id)).all()
        assert [r.id for r in held] == [original_id]
        assert held[0].props == original_props
    html = client.get("/forecast/home-weather", params={"farm": farm.id}).text
    assert "Live forecast refresh unavailable" in html
    assert "Last valid forecast retained" in html
    source_html = client.get("/data-sources/today", params={"farm": farm.id}).text
    assert "Live forecast refresh unavailable; last valid forecast retained." in source_html


def test_bounded_schedule_prioritizes_unattempted_farms_over_expired_cache(
    session, session_factory, settings, monkeypatch, live_payload
):
    from farmcore.core.models.canonical import Farm

    farms = [
        Farm(
            name=f"Weather farm {i}",
            props={"forecast_location": {"latitude": 44.9 + i / 100, "longitude": -0.2}},
        )
        for i in range(6)
    ]
    session.add_all(farms)
    clock.set_live(session, settings)
    session.commit()
    calls = []

    def provider(url, **kwargs):
        calls.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url), json=live_payload)

    monkeypatch.setattr(httpx, "get", provider)
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    assert len(calls) == 5
    session.expire_all()
    # At a later scheduled tick the first five caches are expired again.
    for farm in farms[:5]:
        farm.props = {
            **farm.props,
            "forecast_health": {
                **farm.props["forecast_health"],
                "last_attempt": "2020-01-01T00:00:00+00:00",
            },
        }
    session.commit()
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    assert len(calls) <= 10
    session.expire_all()
    assert "last_success" in farms[-1].props.get("forecast_health", {})


@pytest.mark.parametrize("missing", ["precipitation", "wind_speed_10m"])
def test_missing_required_forecast_inputs_are_unavailable_not_adverse_conditions(
    client, session, session_factory, settings, farm, monkeypatch, live_payload, missing
):
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    live_payload["hourly"] = {
        "time": [(start + timedelta(hours=i)).isoformat() for i in range(144)],
        "precipitation": [0.0] * 144,
        "wind_speed_10m": [5.0] * 144,
        "temperature_2m": [15.0] * 144,
        "relative_humidity_2m": [80.0] * 144,
        "wind_gusts_10m": [8.0] * 144,
    }
    del live_payload["hourly"][missing]
    farm.props = {"forecast_location": {"latitude": 44.9, "longitude": -0.2}}
    clock.set_live(session, settings)
    session.commit()
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url), json=live_payload),
    )
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    html = client.get("/forecast/plan", params={"farm": farm.id}).text
    assert "Forecast inputs unavailable" in html
    assert 'data-grade="best"' not in html and 'data-grade="good"' not in html
    assert 'data-grade="avoid"' not in html


def test_data_sources_reports_receipt_freshness_and_partial_coverage(
    client, session, session_factory, settings, farm, monkeypatch, live_payload
):
    farm.props = {"forecast_location": {"latitude": 44.9, "longitude": -0.2}}
    clock.set_live(session, settings)
    session.commit()
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url), json=live_payload),
    )
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    session.expire_all()
    row = session.scalar(select(ForecastRun).where(ForecastRun.farm_id == farm.id))
    clock.set_replay(session, row.available_at + timedelta(hours=3), settings)
    session.commit()
    html = client.get("/data-sources/today", params={"farm": farm.id}).text
    assert "provider issue time unknown" in html
    assert "Stale forecast" in html and "rain 3/4" in html


def test_concurrent_job_requests_serialize_and_fetch_once(
    committed_session_factory, settings, monkeypatch, live_payload
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from farmcore.core.models.canonical import Farm

    with committed_session_factory.begin() as session:
        farm = Farm(
            name="Concurrent weather",
            props={"forecast_location": {"latitude": 44.9, "longitude": -0.2}},
        )
        session.add(farm)
        clock.set_live(session, settings)
    entered, release, second_started = Event(), Event(), Event()
    calls = []

    def provider(url, **kwargs):
        calls.append(url)
        entered.set()
        assert release.wait(5), "test must release the external provider"
        return httpx.Response(200, request=httpx.Request("GET", url), json=live_payload)

    monkeypatch.setattr(httpx, "get", provider)

    def execute_second():
        second_started.set()
        return run_now(committed_session_factory, [MANIFEST], JOB, settings)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_now, committed_session_factory, [MANIFEST], JOB, settings)
        assert entered.wait(5)
        second = pool.submit(execute_second)
        assert second_started.wait(5)
        assert not second.done(), "second process waits while first receipt is being fetched"
        release.set()
        assert first.result(timeout=10).ok
        assert second.result(timeout=10).ok
    with committed_session_factory() as session:
        assert len(session.scalars(select(ForecastRun)).all()) == 1
    assert len(calls) == 1


def test_generated_missingness_survives_receipts_without_zero_imputation(
    session, session_factory, settings, farm, monkeypatch, live_payload
):
    from copy import deepcopy
    from random import Random

    from farmcore.apps.forecast.store import latest_run
    from farmcore.core.access import repo_for_job

    rng = Random(18042024)
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    farm.props = {"forecast_location": {"latitude": 44.9, "longitude": -0.2}}
    clock.set_live(session, settings)
    session.commit()
    payload = None
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url), json=payload),
    )
    for _ in range(12):
        payload = deepcopy(live_payload)
        missing = [rng.choice((True, False)) for _ in range(8)]
        wind_missing = [rng.choice((True, False)) for _ in range(8)]
        payload["hourly"] = {
            "time": [(start + timedelta(hours=i)).isoformat() for i in range(8)],
            "precipitation": [None if absent else 1.5 for absent in missing],
            "wind_speed_10m": [None if absent else 7.0 for absent in wind_missing],
            "temperature_2m": [12.0] * 8,
        }
        session.expire_all()
        farm.props = {
            **farm.props,
            "forecast_health": {"last_attempt": "2020-01-01T00:00:00+00:00"},
        }
        session.commit()
        assert run_now(session_factory, [MANIFEST], JOB, settings).ok
        session.expire_all()
        run = latest_run(repo_for_job(session, farm.id, JOB), datetime.now(UTC))
        assert [o.rr1_mm for o in run.series] == [None if absent else 1.5 for absent in missing]
        assert [o.ff_kmh for o in run.series] == [
            None if absent else 7.0 for absent in wind_missing
        ]
        assert run.props["coverage"]["rr1_mm"]["known"] == missing.count(False)


@pytest.mark.parametrize("health", ["invalid", {"last_attempt": "broken"}])
def test_malformed_cached_health_cannot_disable_weather_ingestion(
    session, session_factory, settings, farm, monkeypatch, live_payload, health
):
    farm.props = {
        "forecast_location": {"latitude": 44.9, "longitude": -0.2},
        "forecast_health": health,
    }
    clock.set_live(session, settings)
    session.commit()
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url), json=live_payload),
    )
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    session.expire_all()
    assert session.scalar(select(ForecastRun).where(ForecastRun.farm_id == farm.id)) is not None


def test_malformed_second_farm_response_keeps_first_farms_committed_receipt(
    session, session_factory, settings, monkeypatch, live_payload
):
    from copy import deepcopy

    from farmcore.core.models.canonical import Farm

    farms = [
        Farm(
            name=f"Isolated weather {i}",
            props={"forecast_location": {"latitude": 44.9 + i, "longitude": -0.2}},
        )
        for i in range(2)
    ]
    session.add_all(farms)
    clock.set_live(session, settings)
    session.commit()
    responses = [deepcopy(live_payload), deepcopy(live_payload)]
    responses[1]["hourly"]["time"][1] = None

    def provider(url, **kwargs):
        return httpx.Response(200, request=httpx.Request("GET", url), json=responses.pop(0))

    monkeypatch.setattr(httpx, "get", provider)
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    session.expire_all()
    rows = session.scalars(select(ForecastRun)).all()
    assert [r.farm_id for r in rows] == [farms[0].id]
    assert "last_success" in farms[0].props["forecast_health"]
    assert farms[1].props["forecast_health"]["error"] == "ValueError"


def test_notification_and_assistant_provenance_never_call_receipt_a_provider_issue(
    client, session, session_factory, settings, farm, monkeypatch, live_payload
):
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    live_payload["hourly"] = {
        "time": [(start + timedelta(hours=i)).isoformat() for i in range(144)],
        "precipitation": [2.0] * 144,
        "wind_speed_10m": [25.0] * 144,
        "temperature_2m": [15.0] * 144,
        "relative_humidity_2m": [80.0] * 144,
    }
    farm.props = {"forecast_location": {"latitude": 44.9, "longitude": -0.2}}
    clock.set_live(session, settings)
    session.commit()
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, **kw: httpx.Response(200, request=httpx.Request("GET", url), json=live_payload),
    )
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    bell = client.get("/notifications", params={"farm": farm.id}).text
    assert "provider issue time unknown" in bell
    assert " · issued " not in bell
    answer = client.get(
        "/assistant/ask", params={"farm": farm.id, "q": "Which days this week are good?"}
    )
    assert answer.status_code == 200
    assert "provider issue time unknown" in answer.json()["text"]
    assert " · issued " not in answer.json()["text"]
    from zoneinfo import ZoneInfo

    from farmcore.apps.assistant.tools import plan
    from farmcore.core.access import repo_for_job
    from farmcore.core.tools import ToolContext

    result = plan(
        ToolContext(
            repo_for_job(session, farm.id, JOB), datetime.now(UTC), ZoneInfo("Europe/Paris")
        )
    )
    assert result.facts["run"]["issued"] is None
    assert result.facts["run"]["fetched"]


@pytest.mark.parametrize(
    "location",
    [
        None,
        "invalid",
        {},
        {"latitude": 91, "longitude": 0},
        {"latitude": True, "longitude": 0},
        {"latitude": 44.9, "longitude": 10**400},
    ],
)
def test_unavailable_requested_location_never_calls_provider_or_changes_prior_receipt(
    session, session_factory, settings, farm, monkeypatch, location
):
    from copy import deepcopy

    from farmcore.apps.forecast.sources import Run
    from farmcore.apps.forecast.store import import_runs

    now = datetime.now(UTC)
    farm.props = {} if location is None else {"forecast_location": location}
    import_runs(
        session,
        farm.id,
        [
            Run(
                "saved",
                now - timedelta(hours=1),
                "Prior valid saved forecast",
                True,
                44.9,
                -0.2,
                [(now + timedelta(hours=1), "rr1_mm", 1.5)],
            )
        ],
    )
    prior = session.scalar(select(ForecastRun).where(ForecastRun.farm_id == farm.id))
    prior_id, prior_props = prior.id, deepcopy(prior.props)
    clock.set_live(session, settings)
    session.commit()

    def forbidden(*args, **kwargs):
        raise AssertionError("missing/invalid requested location must not reach provider")

    monkeypatch.setattr(httpx, "get", forbidden)
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    session.expire_all()
    held = session.scalars(select(ForecastRun).where(ForecastRun.farm_id == farm.id)).all()
    assert [row.id for row in held] == [prior_id]
    assert held[0].props == prior_props
    assert farm.props["forecast_health"]["error"] == "ValueError"


def test_live_job_uses_first_own_station_coordinates_and_retains_provider_grid_coordinates(
    session, session_factory, settings, farm, monkeypatch, live_payload
):
    from farmcore.core.models.canonical import Asset, Farm

    other = Farm(
        name="Foreign weather station",
        props={"forecast_health": {"last_attempt": datetime.now(UTC).isoformat()}},
    )
    session.add(other)
    session.flush()
    session.add(
        Asset(
            farm_id=other.id, kind="station", name="Foreign station", geom="SRID=4326;POINT(25 60)"
        )
    )
    session.flush()
    session.add(
        Asset(
            farm_id=farm.id,
            kind="station",
            name="First own station",
            geom="SRID=4326;POINT(-0.2 44.9)",
        )
    )
    session.flush()
    session.add(
        Asset(
            farm_id=farm.id,
            kind="station",
            name="Later own station",
            geom="SRID=4326;POINT(-0.3 45)",
        )
    )
    farm.props = {}
    clock.set_live(session, settings)
    session.commit()
    calls = []

    def provider(url, **kwargs):
        calls.append(parse_qs(urlsplit(url).query))
        return httpx.Response(200, request=httpx.Request("GET", url), json=live_payload)

    monkeypatch.setattr(httpx, "get", provider)
    assert run_now(session_factory, [MANIFEST], JOB, settings).ok
    session.expire_all()
    assert len(calls) == 1
    assert calls[0]["latitude"] == ["44.9"] and calls[0]["longitude"] == ["-0.2"]
    row = session.scalar(select(ForecastRun).where(ForecastRun.farm_id == farm.id))
    assert row.props["requested_location"] == {"latitude": 44.9, "longitude": -0.2}
    assert row.props["latitude"] == 44.91 and row.props["longitude"] == -0.21
    assert session.scalar(select(ForecastRun).where(ForecastRun.farm_id == other.id)) is None


@pytest.mark.parametrize("select_farm", [True, False])
def test_scoped_live_job_cannot_fetch_or_write_health_for_unselected_farm(
    session, settings, farm, monkeypatch, live_payload, select_farm
):
    from farmcore.apps.forecast.jobs import fetch_live
    from farmcore.core.manifest import JobContext
    from farmcore.core.models.canonical import Farm

    other = Farm(
        name="Unselected private farm",
        props={"forecast_location": {"latitude": 60.0, "longitude": 25.0}},
    )
    session.add(other)
    farm.props = {"forecast_location": {"latitude": 44.9, "longitude": -0.2}}
    clock.set_live(session, settings)
    session.commit()
    calls = []

    def provider(url, **kwargs):
        calls.append(parse_qs(urlsplit(url).query))
        return httpx.Response(200, request=httpx.Request("GET", url), json=live_payload)

    monkeypatch.setattr(httpx, "get", provider)
    targets = (farm.id,) if select_farm else ()
    fetch_live(JobContext(session, datetime.now(UTC), settings, farm_ids=targets))
    session.commit()
    session.expire_all()
    assert len(calls) == int(select_farm)
    if select_farm:
        assert calls[0]["latitude"] == ["44.9"]
        assert "last_success" in farm.props["forecast_health"]
    else:
        assert "forecast_health" not in farm.props
    assert "forecast_health" not in other.props
    rows = session.scalars(select(ForecastRun)).all()
    assert [row.farm_id for row in rows] == ([farm.id] if select_farm else [])

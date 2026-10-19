"""Stations pages: `/stations/` list with health, `/stations/{id}` hourly chart around now."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from farmcore.apps.stations.service import station_for_farm
from farmcore.core.access import ScopedRepository, ensure_owner_grant
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import AccessLog, Asset, Farm, Observation

NOW = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)  # 10 Apr 2024 06:00 Europe/Paris


def _station(session) -> Asset:
    farm = session.scalar(select(Farm))
    repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "test")
    return station_for_farm(repo)


def _logged(session) -> list[tuple[str, str]]:
    """(resource, scope) of the web reads (actor `owner`)."""
    rows = session.scalars(select(AccessLog).where(AccessLog.actor == "owner"))
    return [(r.resource, r.scope) for r in rows]


def _observe(session, station: Asset, *hours: int) -> None:
    """One synthetic temperature reading at each `NOW + hours`."""
    for h in hours:
        session.add(
            Observation(
                asset_id=station.id, ts=NOW + timedelta(hours=h), variable="t_c", value=10.0
            )
        )
    session.flush()


def _chart(html: str) -> dict:
    m = re.search(r'<script id="station-data" type="application/json">(.*?)</script>', html, re.S)
    assert m, "chart data block missing"
    return json.loads(m.group(1))


def test_station_list_shows_health_and_logs_the_read(client, session, seeded):
    seeded()
    station = _station(session)
    station.props = {
        **station.props,
        "last_ts": (NOW + timedelta(hours=72)).isoformat(),
        "rows": 500,
        "nulls_skipped": 3,
        "last_run": NOW.isoformat(),
    }
    session.flush()

    resp = client.get("/stations/")

    assert resp.status_code == 200
    assert "St-Émilion 33394002" in resp.text
    assert f'href="/stations/{station.id}"' in resp.text
    assert "13 Apr 2024 06:00" in resp.text  # last ingested hour, local time
    assert ">500<" in resp.text and ">3<" in resp.text
    assert ("asset", "stations:read") in _logged(session)


def test_station_page_charts_now_minus_72h_to_plus_72h(client, session, seeded):
    seeded()
    station = _station(session)
    set_replay(session, NOW)
    for hours, variable, value in [
        (-73, "t_c", 1.0),  # before the window
        (-72, "rr1_mm", 2.5),
        (0, "t_c", 11.0),
        (72, "rh_pct", 97.0),
        (73, "t_c", 5.0),  # after the window
    ]:
        session.add(
            Observation(
                asset_id=station.id,
                ts=NOW + timedelta(hours=hours),
                variable=variable,
                value=value,
            )
        )
    session.flush()

    resp = client.get(f"/stations/{station.id}")

    assert resp.status_code == 200
    assert "/static/core/vendor/chart-4.4.1.umd.min.js" in resp.text
    assert "observed future (replay)" in resp.text
    chart = _chart(resp.text)
    assert len(chart["labels"]) == 145  # every hour from now - 72 h to now + 72 h
    assert chart["labels"][0] == "07 Apr 2024 06:00"
    assert chart["labels"][-1] == "13 Apr 2024 06:00"
    assert chart["now_index"] == 72
    assert chart["replay"] is True
    assert chart["rr1_mm"][0] == 2.5
    assert chart["t_c"][72] == 11.0
    assert chart["rh_pct"][144] == 97.0
    # Missing hours stay null (a gap), never 0.
    assert chart["rr1_mm"][1] is None and chart["t_c"][0] is None
    assert 1.0 not in chart["t_c"] and 5.0 not in chart["t_c"]
    assert ("asset", "stations:read") in _logged(session)
    assert ("observation", "stations:read") in _logged(session)


def test_health_never_shows_a_latest_hour_ahead_of_the_replay_clock(client, session, seeded):
    seeded()
    station = _station(session)
    set_replay(session, NOW)
    station.props = {
        **station.props,
        "last_ts": (NOW + timedelta(hours=72)).isoformat(),
        "rows": 500,
        "nulls_skipped": 3,
        "last_run": NOW.isoformat(),
        "file": "meteofrance/demo_station_33394002_2024_2025.csv",
    }
    _observe(session, station, -1, 0, 72)

    for path in ("/stations/", f"/stations/{station.id}"):
        html = client.get(path).text

        assert "Latest hour stored" not in html, path
        assert "Empty cells skipped" not in html, path
        health = html[html.index('data-health="') :]
        assert "Observed up to" in health and "10 Apr 2024 06:00" in health, path
        assert "Replayed forecast" in health, path
        assert "3 days, to 13 Apr 2024 06:00" in health, path


def test_forecast_span_reads_in_whole_days_or_hours(client, session, seeded):
    seeded()
    station = _station(session)
    set_replay(session, NOW)
    # Halves round up (never to the nearest even): 36 h is 2 days, 60 h is 3 days.
    for hours, words in (
        (73, "3 days, to 13 Apr 2024 07:00"),
        (60, "3 days, to"),
        (36, "2 days, to"),
        (24, "1 day, to"),
        (5, "5 hours, to"),
    ):
        station.props = {**station.props, "last_ts": (NOW + timedelta(hours=hours)).isoformat()}
        session.flush()

        assert words in client.get("/stations/").text, hours


def test_observed_up_to_is_the_newest_stored_hour_at_or_before_the_clock(client, session, seeded):
    seeded()
    station = _station(session)
    set_replay(session, NOW)
    station.props = {**station.props, "last_ts": (NOW + timedelta(hours=72)).isoformat()}
    _observe(session, station, -3, 1, 72)  # nothing stored at now itself

    health = client.get("/stations/").text
    health = health[health.index('data-health="') :]

    assert "Observed up to" in health and "10 Apr 2024 03:00" in health
    assert "10 Apr 2024 06:00" not in health


def test_observed_up_to_is_nothing_yet_when_the_clock_is_before_the_first_reading(
    client, session, seeded
):
    seeded()
    station = _station(session)
    set_replay(session, NOW)
    station.props = {**station.props, "last_ts": (NOW + timedelta(hours=72)).isoformat()}
    _observe(session, station, 10, 72)

    health = client.get("/stations/").text
    health = health[health.index('data-health="') :]

    assert "nothing yet" in health
    assert "10 Apr 2024 06:00" not in health
    assert "3 days, to 13 Apr 2024 06:00" in health


def test_health_without_data_ahead_of_the_clock_shows_no_forecast_line(client, session, seeded):
    seeded()
    station = _station(session)
    set_replay(session, NOW)
    station.props = {**station.props, "last_ts": (NOW - timedelta(hours=5)).isoformat()}
    session.flush()

    html = client.get("/stations/").text

    assert "Observed up to" in html and "10 Apr 2024 01:00" in html
    assert "Replayed forecast" not in html


def test_visitors_do_not_see_the_import_file_path(settings, session_factory, session, seeded):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr

    from farmcore.main import create_app

    seeded()
    station = _station(session)
    station.props = {**station.props, "file": "meteofrance/demo_station_33394002_2024_2025.csv"}
    session.flush()
    app = create_app(
        settings.model_copy(
            update={
                "admin_token": SecretStr("tok-st"),
                "demo_mode": True,
                "demo_farm_ids": [station.farm_id],
            }
        ),
        session_factory=session_factory,
    )

    with TestClient(app) as c:
        visitor = c.get("/stations/").text
        admin = c.get("/stations/", headers={"X-Admin-Token": "tok-st"}).text

    assert "demo_station_33394002_2024_2025.csv" not in visitor
    assert "demo_station_33394002_2024_2025.csv" in admin


def test_unknown_station_is_404(client, session, seeded):
    seeded()

    assert client.get("/stations/999999").status_code == 404


def test_station_list_shows_an_import_error(client, session, seeded):
    seeded()
    station = _station(session)
    station.props = {**station.props, "last_run": NOW.isoformat(), "error": "file missing"}
    session.flush()

    resp = client.get("/stations/")

    assert resp.status_code == 200
    assert "Import failed: file missing" in resp.text

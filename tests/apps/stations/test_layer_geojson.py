"""`GET /stations/layer.geojson`: the "Weather" map layer. The farm's own station
(`Asset.kind == "station"`, never `regional`) as one Point: temperature now, rain of the last
48 h (hour-ending `rr1_mm` in (now - 48 h, now]), the local time of the latest reading, and a
stale flag. A missing value is null, never 0; no reading up to now gives no feature.

Synthetic hours on the seeded demo station; replay clock at Wed 10 Apr 2024 06:00 local."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Asset, Observation
from farmcore.core.seed import ensure_default_farm

NOW = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)  # 06:00 Europe/Paris
URL = "/stations/layer.geojson"
VARIABLES = ("rr1_mm", "t_c", "rh_pct", "ff_kmh")


def _station(session) -> Asset:
    return session.scalar(select(Asset).where(Asset.kind == "station"))


def _hours(session, asset: Asset, hours: dict[int, tuple]) -> None:
    """hours: offset from NOW -> (rr1_mm, t_c, rh_pct, ff_kmh); None = no reading."""
    for offset, values in hours.items():
        for variable, value in zip(VARIABLES, values, strict=True):
            if value is not None:
                session.add(
                    Observation(
                        asset_id=asset.id,
                        ts=NOW + timedelta(hours=offset),
                        variable=variable,
                        value=value,
                    )
                )
    session.flush()


def _layer(client) -> dict:
    resp = client.get(URL)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/geo+json")
    body = resp.json()
    assert body["type"] == "FeatureCollection"
    return body


def _props(client) -> dict:
    features = _layer(client)["features"]
    assert len(features) == 1
    return features[0]["properties"]


def test_station_point_with_temperature_rain_48h_and_reading_time(client, session, seeded):
    seeded()
    set_replay(session, NOW)
    station = _station(session)
    _hours(
        session,
        station,
        {
            -48: (5.0, 3.0, 90.0, 4.0),  # ends the hour before the window: not counted
            -47: (1.2, 4.0, 90.0, 4.0),
            -10: (0.4, 8.0, 85.0, 5.0),
            -1: (0.0, 9.0, 80.0, 5.0),
            0: (0.6, 11.4, 93.0, 7.2),
            1: (9.0, 20.0, 50.0, 3.0),  # after the clock: not counted, not "now"
        },
    )

    feature = _layer(client)["features"][0]

    assert feature["geometry"]["type"] == "Point"
    lon, lat = feature["geometry"]["coordinates"]
    assert -0.3 < lon < 0.0 and 44.8 < lat < 45.0  # the seeded St-Émilion station
    props = feature["properties"]
    assert props["name"] == station.name
    assert props["t_c"] == 11.4
    assert props["rain_48h_mm"] == 2.2  # 1.2 + 0.4 + 0.0 + 0.6
    assert props["as_of"] == "06:00"
    assert props["as_of_text"] == "today 06:00"
    assert props["stale"] is False
    assert props["url"] == "/stations/"


def test_missing_values_are_null_never_zero(client, session, seeded):
    seeded()
    set_replay(session, NOW)
    _hours(session, _station(session), {0: (None, None, 80.0, 5.0)})

    props = _props(client)

    assert props["t_c"] is None
    assert props["rain_48h_mm"] is None
    assert props["as_of"] == "06:00"


def test_station_without_coordinates_has_no_map_feature(client, session, seeded):
    seeded()
    set_replay(session, NOW)
    station = _station(session)
    _hours(session, station, {0: (0.0, 11.4, 80.0, 5.0)})
    station.geom = None
    session.flush()

    assert _layer(client)["features"] == []


def test_reading_older_than_two_hours_is_stale_and_keeps_its_time(client, session, seeded):
    seeded()
    set_replay(session, NOW)
    _hours(session, _station(session), {-3: (0.0, 7.0, 80.0, 5.0)})

    props = _props(client)

    assert props["stale"] is True
    assert props["as_of"] == "03:00"
    assert props["as_of_text"] == "today 03:00"
    assert props["t_c"] == 7.0


def test_latest_reading_before_the_48h_window_still_shows_with_no_rain_figure(
    client, session, seeded
):
    seeded()
    set_replay(session, NOW)
    _hours(session, _station(session), {-72: (3.0, 6.0, 80.0, 5.0)})

    props = _props(client)

    assert props["stale"] is True and props["t_c"] == 6.0
    assert props["as_of"] == "06:00"  # local time of day of that reading, 3 days back
    assert props["as_of_text"] == "Sun 06:00"  # the day too: not to be read as today
    assert props["rain_48h_mm"] is None


def test_no_reading_up_to_now_is_an_empty_collection(client, session, seeded):
    seeded()
    set_replay(session, NOW)
    _hours(session, _station(session), {1: (0.0, 9.0, 80.0, 5.0)})  # only after the clock

    assert _layer(client)["features"] == []


def test_no_station_is_an_empty_collection(client, session, settings, monkeypatch, tmp_path):
    from farmcore.core import seed as core_seed

    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
    core_seed.seed(session, scene=None, data_dir=tmp_path)
    set_replay(session, NOW)

    assert _layer(client)["features"] == []


def test_a_regional_asset_with_wild_values_changes_nothing(client, session, settings, seeded):
    seeded()
    set_replay(session, NOW)
    _hours(session, _station(session), {-1: (0.2, 9.0, 80.0, 5.0), 0: (0.0, 10.0, 80.0, 5.0)})
    before = _layer(client)

    farm = ensure_default_farm(session, settings)
    regional = Asset(
        farm_id=farm.id,
        kind="regional",
        name="synthetic regional cell",
        geom="SRID=4326;POINT(2.0 47.0)",
    )
    session.add(regional)
    session.flush()
    _hours(session, regional, {h: (9.0, 40.0, 5.0, 120.0) for h in range(-47, 1)})

    assert _layer(client) == before

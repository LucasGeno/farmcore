"""The Today weather card (`GET /stations/today`): condition now, next rain from the 72 h
replay forecast, last 48 h of rain, a 24 h strip (inline SVG), and a link to the station page.
Synthetic hours on the seeded demo station; the clock is replay at 10 Apr 2024 06:00 local."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from farmcore.core import seed as core_seed
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Asset, Observation

NOW = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)  # Wed 10 Apr 2024 06:00 Europe/Paris


def _station(session) -> Asset:
    return session.scalar(select(Asset).where(Asset.kind == "station"))


def _hours(session, station: Asset, hours: dict[int, tuple]) -> None:
    """hours: offset from NOW -> (rr1_mm, t_c, rh_pct, ff_kmh); None = no reading."""
    for offset, values in hours.items():
        for variable, value in zip(("rr1_mm", "t_c", "rh_pct", "ff_kmh"), values, strict=True):
            if value is not None:
                session.add(
                    Observation(
                        asset_id=station.id,
                        ts=NOW + timedelta(hours=offset),
                        variable=variable,
                        value=value,
                    )
                )
    session.flush()


def _card(client) -> str:
    resp = client.get("/stations/today")
    assert resp.status_code == 200
    return html.unescape(resp.text)


def _text(body: str) -> str:
    """Visible text, tags stripped, whitespace collapsed."""
    return " ".join(re.sub(r"<[^>]+>", " ", body).split())


def _dry(offsets) -> dict[int, tuple]:
    return {h: (0.0, 12.0, 70.0, 8.0) for h in offsets}


def test_no_station_says_so(client, session, settings, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(core_seed, "get_settings", lambda: settings)
    core_seed.seed(session, scene=None, data_dir=tmp_path)
    set_replay(session, NOW)
    body = _text(_card(client))
    assert "No weather station connected yet" in body


def test_station_without_readings_says_so(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    body = _card(client)
    assert "No readings from" in _text(body)
    assert f'href="/stations/{_station(session).id}"' in body


def test_now_shows_temperature_condition_wind_humidity_and_reading_time(
    client, session, seeded
) -> None:
    seeded()
    set_replay(session, NOW)
    station = _station(session)
    _hours(session, station, {-1: (0.0, 9.0, 80.0, 5.0), 0: (0.0, 11.4, 93.0, 7.2)})
    body = _text(_card(client))
    assert "Weather" in body and station.name in body
    assert "06:00" in body
    assert "11 °C" in body
    assert "Dry · humid" in body
    assert "7 km/h" in body and "93 %" in body
    assert "Raining" not in body


def test_rain_in_the_latest_hour_reads_raining(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    _hours(session, _station(session), {0: (0.4, 8.0, 97.0, 3.0)})
    body = _text(_card(client))
    assert "Raining" in body and "Dry" not in body


def test_next_rain_says_when_and_how_much_in_the_24_h_from_its_start(
    client, session, seeded
) -> None:
    seeded()
    set_replay(session, NOW)
    hours = _dry(range(0, 40))
    hours[2] = (0.1, 12.0, 70.0, 8.0)  # below the 0.2 mm gauge tip: not rain
    hours[8] = (1.0, 12.0, 90.0, 8.0)  # ts Wed 14:00 local = rain 13:00-14:00: the first rain
    hours[9] = (2.0, 12.0, 90.0, 8.0)
    hours[31] = (0.5, 12.0, 90.0, 8.0)  # 23 h after the start: counted
    hours[32] = (5.0, 12.0, 90.0, 8.0)  # 24 h after the start: not counted
    _hours(session, _station(session), hours)
    body = _text(_card(client))
    assert "Rain from today 13:00" in body
    assert "3.5 mm in 24 h" in body
    assert "forecast" in body


def test_no_rain_in_the_next_three_days(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    hours = _dry(range(0, 73))
    hours[73] = (4.0, 12.0, 90.0, 8.0)  # beyond the 72 h horizon
    _hours(session, _station(session), hours)
    body = _text(_card(client))
    assert "No rain over 1 mm in the next 3 days" in body
    assert "Rain from" not in body


def test_a_lone_gauge_tip_is_not_rain_and_the_next_day_reads_tomorrow(
    client, session, seeded
) -> None:
    seeded()
    set_replay(session, NOW)
    hours = _dry(range(0, 60))
    hours[1] = (0.2, 12.0, 90.0, 8.0)  # Wed 07:00: drizzle, 0.2 mm in its 24 h
    hours[27] = (0.8, 12.0, 90.0, 8.0)  # Thu 09:00: 0.8 + 0.4 = 1.2 mm in 24 h
    hours[28] = (0.4, 12.0, 90.0, 8.0)
    _hours(session, _station(session), hours)
    body = _text(_card(client))
    assert "Rain from tomorrow 08:00" in body and "1.2 mm in 24 h" in body


def test_no_forecast_hours_never_claims_a_dry_spell(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    _hours(session, _station(session), _dry(range(-3, 1)))
    body = _text(_card(client))
    assert "No forecast" in body
    assert "No rain over 1 mm in the next 3 days" not in body


def test_last_48_h_rain_sums_observed_hours_only(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    hours = _dry(range(0, 3))
    hours[-49] = (5.0, 9.0, 90.0, 4.0)  # older than 48 h
    hours[-47] = (1.0, 9.0, 90.0, 4.0)
    hours[0] = (0.2, 9.0, 90.0, 4.0)
    hours[2] = (9.0, 9.0, 90.0, 4.0)  # after now: forecast, not "last 48 h"
    _hours(session, _station(session), hours)
    body = _text(_card(client))
    assert "Last 48 h: at least 1.2 mm" in body
    assert "2/48 h of rain readings" in body


def test_card_links_to_the_station_page(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    station = _station(session)
    _hours(session, station, _dry(range(0, 25)))
    body = _card(client)
    assert re.search(rf'<a [^>]*href="/stations/{station.id}"', body)
    assert "Open weather" in body


def test_24_h_strip_is_an_accessible_inline_svg(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    hours = _dry(range(0, 25))
    hours[5] = (1.5, 14.0, 90.0, 8.0)
    hours[6] = (0.5, 15.0, 90.0, 8.0)
    _hours(session, _station(session), hours)
    body = _card(client)
    svg = re.search(r"<svg[^>]*role=\"img\"[^>]*>", body)
    assert svg, "no role=img svg"
    label = re.search(r'aria-label="([^"]+)"', svg.group(0))
    assert label and "Next 24 h" in label.group(1) and "2.0 mm" in label.group(1)
    assert body.count('data-rain-bar="') == 2  # one bar per hour with rain
    assert "chart.js" not in body.lower()


def test_missing_rain_with_other_weather_never_becomes_dry(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    _hours(session, _station(session), {h: (None, 12.0, 70.0, 8.0) for h in range(73)})
    body = _text(_card(client))
    assert "Rain unknown" in body
    assert "Rain forecast incomplete" in body
    assert "Dry" not in body and "No rain over" not in body
    assert "no rain" not in _card(client)
    assert "rain readings unavailable" in _card(client)


def test_gap_in_forecast_cannot_support_a_dry_horizon(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    hours = _dry(range(73))
    hours[12] = (None, 12.0, 70.0, 8.0)
    _hours(session, _station(session), hours)
    body = _text(_card(client))
    assert "Rain forecast incomplete" in body
    assert "No rain over" not in body
    assert "rain coverage 23/24 h" in _card(client)


def test_missing_rain_hour_does_not_hide_known_rain_or_claim_exact_sum(
    client, session, seeded
) -> None:
    seeded()
    set_replay(session, NOW)
    hours = _dry(range(73))
    hours[1] = (1.5, 12.0, 70.0, 8.0)
    hours[2] = (None, 12.0, 70.0, 8.0)
    _hours(session, _station(session), hours)
    body = _text(_card(client))
    assert "Rain from today 06:00" in body
    assert "At least 1.5 mm in 24 h" in body
    assert "rain coverage 23/24 h" in _card(client)


def test_recent_rain_total_with_gaps_is_labelled_incomplete(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    _hours(session, _station(session), {-1: (1.2, 12.0, 70.0, 8.0), 0: (0.0, 12.0, 70.0, 8.0)})
    body = _text(_card(client))
    assert "Last 48 h: at least 1.2 mm" in body
    assert "2/48 h of rain readings" in body


def test_complete_recent_rain_total_is_not_marked_incomplete(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    _hours(session, _station(session), _dry(range(-47, 73)))
    body = _text(_card(client))
    assert "Last 48 h: 0.0 mm" in body
    assert "at least" not in body.lower()
    assert "Rain forecast incomplete" not in body


def test_known_later_rain_does_not_imply_no_earlier_rain_when_readings_missing(
    client, session, seeded
) -> None:
    seeded()
    set_replay(session, NOW)
    hours = _dry(range(73))
    hours[1] = (None, 12.0, 70.0, 8.0)
    hours[5] = (1.5, 12.0, 70.0, 8.0)
    _hours(session, _station(session), hours)
    body = _text(_card(client))
    assert "Rain in available forecast at today 10:00" in body
    assert "Earlier rain readings missing" in body
    assert "Rain from today 10:00" not in body


def test_rain_window_completeness_uses_only_the_bounded_forecast() -> None:
    from farmcore.apps.stations.today import weather_card

    rows = [
        {
            "ts": NOW + timedelta(hours=h),
            "rr1_mm": 1.5 if h == 60 else 8.0 if h == 73 else 0.0,
            "t_c": 12.0,
            "rh_pct": 70.0,
            "ff_kmh": 8.0,
        }
        for h in range(85)
    ]
    card = weather_card(rows, NOW, "Europe/Paris")
    assert card["next_rain"]["mm"] == "1.5"
    assert card["next_rain"]["complete"] is False

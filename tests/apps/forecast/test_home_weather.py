"""Home strip: only the selected farm's forecast available at replay now."""

import html
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from farmcore.apps.forecast import sources, store
from farmcore.core import clock
from tests.apps.forecast.conftest import FIXTURE


def test_home_weather_shows_five_source_labelled_days_at_the_replay_clock(
    client, session, settings, farm
):
    store.import_runs(session, farm.id, sources.FileSource(FIXTURE).runs())
    clock.set_replay(session, datetime(2024, 4, 9, 4, tzinfo=UTC), settings)
    session.commit()
    response = client.get("/forecast/home-weather", params={"farm": farm.id})
    assert response.status_code == 200
    html = response.text
    assert html.count('data-weather-day="') == 5
    assert 'data-weather-day="2024-04-09"' in html
    assert 'data-weather-day="2024-04-13"' in html
    assert "Forecast run 9 Apr 00:00 UTC" in html
    assert "available 9 Apr 00:00 UTC" in html
    assert "forecast as issued" in html
    assert "Rain" in html and "Wind" in html and "Gust" in html and "°C" in html
    assert "information only" in html
    assert "<html" not in html
    assert f"/stations/?farm={farm.id}" in html


def test_home_weather_never_borrows_another_farms_run_and_disappears_when_off(
    client, session, settings, farm
):
    from farmcore.core.access import ensure_owner_grant
    from farmcore.core.models.canonical import Farm
    from farmcore.core.models.kernel import AppModule

    store.import_runs(session, farm.id, sources.FileSource(FIXTURE).runs())
    other = Farm(name="Spanish archive", props={"country": "ES"})
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    clock.set_replay(session, datetime(2024, 4, 9, 4, tzinfo=UTC), settings)
    session.commit()
    client.cookies.set("farmcore_tour", "1")
    response = client.get("/forecast/home-weather", params={"farm": other.id})
    assert response.status_code == 200
    assert "No forecast yet" in response.text and "Synthetic test forecast" not in response.text
    assert (
        "forecast as issued" in client.get("/forecast/home-weather", params={"farm": farm.id}).text
    )
    session.get(AppModule, "forecast").state = "uninstalled"
    session.commit()
    assert client.get("/forecast/home-weather").text == ""


def test_home_weather_retains_partial_days_missing_gusts_and_local_rain_buckets(
    client, session, settings, farm
):
    from datetime import timedelta

    run = sources.Run(
        "synthetic",
        datetime(2024, 4, 8, tzinfo=UTC),
        "Synthetic partial weather",
        False,
        None,
        None,
        # 22Z ends the hour immediately before local midnight (rain belongs to 8 Apr).
        [
            (datetime(2024, 4, 8, 22, tzinfo=UTC), "rr1_mm", 7.5),
            (datetime(2024, 4, 8, 22, tzinfo=UTC), "t_c", 12.5),
            (datetime(2024, 4, 9, 4, tzinfo=UTC), "ff_kmh", 8.0),
        ],
    )
    store.import_runs(session, farm.id, [run])
    clock.set_replay(session, datetime(2024, 4, 8, 20, tzinfo=UTC), settings)
    session.commit()
    html = client.get("/forecast/home-weather").text
    today, tomorrow = html.split('data-weather-day="2024-04-09"', 1)
    assert "7.5 mm*" in today
    assert '*<span class="sr-only">partial data</span>' in today
    assert "12–12 °C*" in tomorrow
    assert "7.5 mm" not in tomorrow
    assert "not the forecast as it was issued" in html
    assert "No forecast yet" in html
    # A run that ended before the selected clock is never reused as future weather.
    clock.set_replay(session, run.values[-1][0] + timedelta(hours=1), settings)
    session.commit()
    assert "Synthetic partial weather" not in client.get("/forecast/home-weather").text


def test_home_weather_is_one_compact_strip_linking_to_plan(client, session, settings, farm):
    store.import_runs(session, farm.id, sources.FileSource(FIXTURE).runs())
    clock.set_replay(session, datetime(2024, 4, 9, 4, tzinfo=UTC), settings)
    session.commit()
    html = client.get("/forecast/home-weather", params={"farm": farm.id}).text
    assert html.count("data-weather-strip") == 1
    assert "overflow-x-auto" in html
    assert html.count('href="/forecast/plan?') == 5
    assert "<h3" not in html and "Gust max" not in html


def test_each_home_weather_link_opens_its_matching_plan_day(client, session, settings, farm):
    store.import_runs(session, farm.id, sources.FileSource(FIXTURE).runs())
    clock.set_replay(session, datetime(2024, 4, 9, 4, tzinfo=UTC), settings)
    session.commit()
    body = client.get("/forecast/home-weather", params={"farm": farm.id}).text
    days_and_links = re.findall(r'data-weather-day="([^"]+)"[^>]*>\s*<a href="([^"]+)"', body)
    assert len(days_and_links) == 5
    for day, raw_url in days_and_links:
        url = html.unescape(raw_url)
        parts = urlsplit(url)
        assert parts.path == "/forecast/plan"
        assert parse_qs(parts.query) == {"day": [day], "farm": [str(farm.id)]}
        assert parts.fragment == f"plan-day-{day}"
        response = client.get(url)
        assert response.status_code == 200
        assert f'id="plan-day-{day}"' in response.text
        assert f'data-selected-day="{day}"' in response.text

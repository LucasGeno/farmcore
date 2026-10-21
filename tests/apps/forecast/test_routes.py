"""The graded spray window on the parcel advice card and on Today, read from the run issued by
clock time `now`; a short honest note without one, nothing when the app is switched off."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import event

from farmcore.apps.forecast import sources, store
from farmcore.core import clock, ext_ids
from farmcore.core.access import FARM_COOKIE, ensure_owner_grant
from farmcore.core.models.canonical import Advice, Farm, Parcel
from farmcore.core.models.kernel import AppModule, ClockState
from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.forecast.conftest import FIXTURE

SQUARE = "SRID=4326;MULTIPOLYGON(((-0.16 44.89,-0.15 44.89,-0.15 44.90,-0.16 44.90,-0.16 44.89)))"


def _utc(day: int, hour: int = 0) -> datetime:
    return datetime(2024, 4, day, hour, tzinfo=UTC)


@pytest.fixture
def parcel(session, farm) -> Parcel:
    parcel, _ = ext_ids.upsert(
        session,
        source="test-parcels",
        key="FR-TEST-1",
        model=Parcel,
        values={
            "farm_id": farm.id,
            "name": "Test parcel 1",
            "alias": "P1",
            "iacs_id": "FR-TEST-1",
            "area_ha": Decimal("1.25"),
            "geom": SQUARE,
            "props": {},
        },
    )
    session.flush()
    return parcel


@pytest.fixture
def imported(session, farm):
    store.import_runs(session, farm.id, sources.FileSource(FIXTURE).runs())
    session.flush()


def _get(client, session, settings, path: str, now: datetime) -> str:
    clock.set_replay(session, now, settings)
    session.commit()
    resp = client.get(path)
    assert resp.status_code == 200, resp.text
    return resp.text


def _days(html: str) -> list[tuple[str, str]]:
    return re.findall(r'data-day="([0-9-]+)" data-grade="([a-z_]+)"', html)


def test_busy_preference_round_trip_reranks_today_and_parcel_without_a_db_write(
    client, session, settings, farm, parcel, imported, connection
):
    _get(client, session, settings, "/forecast/today", _utc(9, 4))
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", record)
    try:
        response = client.get(
            "/forecast/busy",
            params={"day": "2024-04-09", "busy": "1", "next": "/forecast/today"},
            follow_redirects=False,
        )
    finally:
        event.remove(connection, "before_cursor_execute", record)

    assert response.status_code == 303
    assert not any(re.match(r"\s*(INSERT|UPDATE|DELETE)\b", s, re.I) for s in statements)
    assert response.headers["location"] == "/forecast/today"
    assert client.cookies[f"farmcore_forecast_busy_{farm.id}"] == "2024-04-09"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie
    for path in ("/forecast/plan", f"/forecast/parcels/{parcel.id}"):
        html = client.get(path).text
        assert _days(html)[:2] == [("2024-04-09", "busy"), ("2024-04-10", "best")]
        assert "Unavailable (busy)" in html


def test_each_day_has_a_native_form_and_a_scoped_htmx_busy_free_button(
    client, session, settings, farm, parcel, imported
):
    html = _get(
        client, session, settings, f"/forecast/parcels/{parcel.id}?farm={farm.id}", _utc(9, 4)
    )
    forms = re.findall(r"<form\b.*?</form>", html, re.S)
    assert len(forms) == 5
    assert all('method="get"' in form and "/forecast/busy" in form for form in forms)
    assert all('hx-target="closest [data-spray-window]"' in form for form in forms)
    assert all('hx-swap="outerHTML"' in form for form in forms)
    assert all(f'name="farm" value="{farm.id}"' in form for form in forms)
    assert all(f"/forecast/parcels/{parcel.id}?farm={farm.id}" in form for form in forms)
    assert 'aria-pressed="false"' in html and ">Busy<" in html
    assert "this browser" in html

    client.get("/forecast/busy", params={"day": "2024-04-09", "busy": "1"})
    busy_html = client.get(f"/forecast/parcels/{parcel.id}").text
    assert 'aria-pressed="true"' in busy_html and ">Free<" in busy_html
    assert re.findall(r'aria-label="(Busy on [^"]+)"', html) == re.findall(
        r'aria-label="(Busy on [^"]+)"', busy_html
    )


def test_busy_free_is_idempotent_and_all_busy_has_no_best(
    client, session, settings, farm, parcel, imported
):
    _get(client, session, settings, "/forecast/today", _utc(9, 4))
    for d in range(9, 14):
        for _ in range(2):
            response = client.get(
                "/forecast/busy",
                params={"day": f"2024-04-{d:02}", "busy": "1", "next": "/forecast/plan"},
            )
            assert response.status_code == 200
    assert all(grade == "busy" for _, grade in _days(response.text)[:5])
    for _ in range(2):
        response = client.get(
            "/forecast/busy",
            params={"day": "2024-04-10", "busy": "0", "next": "/forecast/plan"},
        )
    assert dict(_days(response.text))["2024-04-10"] == "best"
    assert dict(_days(client.get(f"/forecast/parcels/{parcel.id}").text))["2024-04-09"] == "busy"


def test_busy_cookie_is_per_farm_and_explicit_farm_beats_global_cookie(
    client, session, settings, farm, parcel, imported
):
    _get(client, session, settings, "/forecast/today", _utc(9, 4))
    other = Farm(name="Another forecast farm")
    session.add(other)
    session.flush()
    ensure_owner_grant(session, other)
    store.import_runs(session, other.id, sources.FileSource(FIXTURE).runs())
    session.commit()
    client.cookies.set(FARM_COOKIE, str(other.id))
    response = client.get(
        "/forecast/busy",
        params={
            "farm": farm.id,
            "day": "2024-04-09",
            "busy": "1",
            "next": f"/forecast/parcels/{parcel.id}?farm={farm.id}",
        },
    )
    assert dict(_days(response.text))["2024-04-09"] == "busy"
    assert dict(_days(client.get("/forecast/plan").text))["2024-04-09"] == "best"
    assert client.get(f"/forecast/parcels/{parcel.id}").status_code == 404
    response = client.get("/forecast/plan", params={"farm": farm.id})
    assert dict(_days(response.text))["2024-04-09"] == "busy"
    assert f"farmcore_forecast_busy_{other.id}" not in client.cookies
    client.cookies.clear()
    assert dict(_days(client.get("/forecast/plan").text))["2024-04-09"] == "best"


def test_cookie_discards_invalid_and_past_dates_using_local_replay_today(
    client, session, settings, farm, imported
):
    _get(client, session, settings, "/forecast/today", _utc(8, 22))  # 9 Apr local
    name = f"farmcore_forecast_busy_{farm.id}"
    client.cookies.set(name, "2024-04-08,bad,2024-02-30,20240409,2024-04-09,2024-04-09")
    response = client.get("/forecast/plan")
    assert _days(response.text)[:2] == [("2024-04-09", "busy"), ("2024-04-10", "avoid")]
    # Simulate the server cookie (avoid httpx retaining two domains for the same name).
    client.cookies.clear()
    client.cookies.set(name, "2024-04-08,2024-04-09", domain="testserver.local", path="/")
    response = client.get(
        "/forecast/busy", params={"day": "2024-04-08", "busy": "1"}, follow_redirects=False
    )
    assert client.cookies[name] == "2024-04-09"
    response = client.get(
        "/forecast/busy", params={"day": "2024-04-09", "busy": "0"}, follow_redirects=False
    )
    assert name not in client.cookies


@pytest.mark.parametrize("next_", ["//evil.example", "https://evil.example", "/\\evil", "oops", ""])
def test_busy_preference_safe_next(client, session, settings, farm, next_):
    clock.set_replay(session, _utc(9, 4), settings)
    session.commit()
    response = client.get(
        "/forecast/busy",
        params={"day": "2024-04-09", "busy": "1", "next": next_},
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/"


@pytest.mark.parametrize(
    "params",
    [
        {"day": "bad", "busy": "1"},
        {"day": "2024-02-30", "busy": "1"},
        {"day": "2024-04-09", "busy": "yes"},
        {"day": "2024-04-09"},
    ],
)
def test_busy_preference_invalid_form_has_no_cookie(client, farm, params):
    response = client.get("/forecast/busy", params=params, follow_redirects=False)
    assert response.status_code == 422
    assert "set-cookie" not in response.headers


def test_busy_preference_app_off_has_no_cookie(client, session, farm):
    session.get(AppModule, "forecast").state = "uninstalled"
    session.commit()
    response = client.get(
        "/forecast/busy", params={"day": "2024-04-09", "busy": "1"}, follow_redirects=False
    )
    assert response.status_code == 404
    assert "set-cookie" not in response.headers


def test_busy_preference_does_not_initialize_a_missing_clock(
    client, session, settings, farm, connection
):
    row = session.get(ClockState, 1)
    if row is not None:
        session.delete(row)
    session.commit()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", record)
    try:
        response = client.get(
            "/forecast/busy", params={"day": "2024-04-09", "busy": "1"}, follow_redirects=False
        )
    finally:
        event.remove(connection, "before_cursor_execute", record)
    assert response.status_code == 303
    assert not any(re.match(r"\s*(INSERT|UPDATE|DELETE)\b", s, re.I) for s in statements)
    assert session.get(ClockState, 1) is None


def test_busy_cookie_is_bounded_and_https_only_secure(client, session, settings, farm):
    clock.set_replay(session, _utc(9, 4), settings)
    session.commit()
    name = f"farmcore_forecast_busy_{farm.id}"
    days = [(date(2024, 4, 9) + timedelta(days=i)).isoformat() for i in range(180)]
    client.cookies.set(name, ",".join(days), domain="testserver.local", path="/")
    response = client.get(
        "/forecast/busy", params={"day": "2025-04-09", "busy": "1"}, follow_redirects=False
    )
    assert response.status_code == 422
    response = client.get(
        "https://testserver/forecast/busy",
        params={"day": "2024-04-09", "busy": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "Secure" in response.headers["set-cookie"]
    assert len(response.headers["set-cookie"]) < 4096


def test_parcel_window_ranks_five_days_with_words_and_the_issue_time(
    client, session, settings, parcel, imported
):
    html = _get(client, session, settings, f"/forecast/parcels/{parcel.id}", _utc(9, 4))

    assert _days(html) == [
        ("2024-04-09", "best"),
        ("2024-04-10", "good"),
        ("2024-04-11", "good"),
        ("2024-04-12", "avoid"),
        ("2024-04-13", "good"),
    ]
    assert ">Best weather<" in html and ">Avoid<" in html  # words, not only a colour
    assert "Open 06:00–20:00 (14 h): no rain, wind up to 8 km/h" in html
    assert "Rain 21.0 mm, wind up to 22 km/h" in html
    assert "Forecast run 9 Apr 00:00 UTC · available 9 Apr 00:00 UTC" in html
    assert "%" not in html


def test_the_window_never_reads_a_run_issued_after_now(client, session, settings, parcel, imported):
    """At 8 Apr 14:00 the 9 Apr run does not exist yet: the 8 Apr run, wet on 10 Apr, is used."""
    early = _get(client, session, settings, f"/forecast/parcels/{parcel.id}", _utc(8, 12))
    late = _get(client, session, settings, f"/forecast/parcels/{parcel.id}", _utc(9, 4))

    assert ("2024-04-10", "avoid") in _days(early)
    assert "Forecast run 8 Apr 00:00 UTC" in early
    assert ("2024-04-10", "good") in _days(late)


def test_without_a_forecast_by_now_the_note_says_so(client, session, settings, parcel, imported):
    html = _get(client, session, settings, f"/forecast/parcels/{parcel.id}", _utc(7, 12))

    assert _days(html) == []
    assert "No forecast loaded" in html


def test_protected_days_say_so_without_changing_the_grade(
    client, session, settings, farm, parcel, imported
):
    complete_station(session, farm.id, _utc(9, 4))
    session.add(
        Advice(
            farm_id=farm.id,
            parcel_id=parcel.id,
            evaluated_at=_utc(9, 4),
            decision="protected",
            reasons=[],
            protected_until=_utc(10, 20),  # 10 Apr 22:00 Paris
            products=[],
        )
    )
    html = _get(client, session, settings, f"/forecast/parcels/{parcel.id}", _utc(9, 4))

    days = dict(_days(html))
    assert days["2024-04-10"] == "good"
    assert html.count("Still protected until 10 Apr") == 2  # 9 and 10 Apr
    eleventh = html[html.index('data-day="2024-04-11"') :]
    assert "Still protected" not in eleventh


def test_today_shows_the_farm_window(client, session, settings, parcel, imported):
    html = _get(client, session, settings, "/forecast/today", _utc(9, 4))

    assert len(_days(html)) == 3
    assert "Spray window" in html and "See Plan" in html


def test_the_advice_card_carries_the_window_slot_only_while_the_app_is_on(
    client, session, settings, parcel, imported
):
    complete_station(session, parcel.farm_id, _utc(9, 4))
    on = _get(client, session, settings, f"/advisor/parcels/{parcel.id}", _utc(9, 4))
    session.get(AppModule, "forecast").state = "uninstalled"
    off = _get(client, session, settings, f"/advisor/parcels/{parcel.id}", _utc(9, 4))
    fragment = _get(client, session, settings, f"/forecast/parcels/{parcel.id}", _utc(9, 4))

    assert f"/forecast/parcels/{parcel.id}" in on
    assert "/forecast/" not in off
    assert fragment.strip() == ""


def test_window_markup_wraps_on_a_phone(client, session, settings, parcel, imported):
    html = _get(client, session, settings, f"/forecast/parcels/{parcel.id}", _utc(9, 4))

    for row in re.findall(r"<li [^>]*data-day[^>]*>", html):
        assert "flex-wrap" in row
    assert "min-w-0" in html and "break-words" in html


def test_an_ecmwf_run_shows_its_times_attribution_and_gusts_as_information(
    client, session, settings, farm, parcel
):
    from tests.apps.forecast.test_sources import RUNS

    store.import_runs(session, farm.id, sources.SingleRunsDir(RUNS).runs())
    # 9 Apr 06:00 Paris: the 9 Apr 00Z run is out only at 07:34 UTC, so the 12Z run is not
    # there either; at 22:00 Paris (20Z) the 12Z run (out 19:34 UTC) is used
    html = _get(client, session, settings, f"/forecast/parcels/{parcel.id}", _utc(9, 20))

    assert "Forecast run 9 Apr 12:00 UTC · available 9 Apr 19:34 UTC" in html
    assert "ECMWF IFS HRES via Open-Meteo" in html
    assert 'href="https://open-meteo.com/"' in html and "Weather data by Open-Meteo.com" in html
    assert 'href="https://creativecommons.org/licenses/by/4.0/"' in html
    assert "aggregated to daily values" in html
    assert "gusts up to 35 km/h" in html.lower()  # information only, no gust rule


def test_the_header_says_weather_only_which_forecast_and_the_configured_band(
    client, session, settings, parcel, imported
):
    card = _get(client, session, settings, f"/forecast/parcels/{parcel.id}", _utc(9, 4))
    today = _get(client, session, settings, "/forecast/today", _utc(9, 4))

    assert (
        "Spraying weather only — whether a parcel needs a spray, and by when, is the advice above."
        in card
    )
    assert "the advice's window above uses the station's weather" in card
    assert ">Best weather<" in card and ">Best<" not in card
    assert "06:00–20:00" in card  # the advisor calendar's daytime band
    assert "is the advice above" not in today
    assert "Spraying weather only" in today and "the station's weather" in today


def test_a_stale_run_reads_as_no_forecast(client, session, settings, parcel, imported):
    html = _get(client, session, settings, f"/forecast/parcels/{parcel.id}", _utc(20, 4))

    assert "No forecast loaded" in html and "data-day" not in html


def test_field_window_keeps_weather_scope_visible_and_discloses_forecast_method(
    client, session, settings, farm, imported
):
    body = _get(client, session, settings, "/forecast/today", _utc(9, 4))
    detail = re.search(r"<details\b[^>]*data-forecast-method[^>]*>(.*?)</details>", body, re.S)
    assert detail is not None and " open" not in detail.group(0).split(">")[0]
    assert "Forecast run" in detail.group(0)
    assert "data-weather-only" in body.replace(detail.group(0), "")
    assert "Busy on" in body and "See Plan" in body
    client.cookies.set("expert", "1")
    expert = client.get("/forecast/today").text
    assert re.search(r"<details\b[^>]*data-forecast-method[^>]* open", expert)


def test_field_forecast_freshness_and_reconstruction_limit_stay_outside_closed_method(
    client, session, settings, farm, imported
):
    from sqlalchemy import select

    from farmcore.apps.forecast.models import ForecastRun

    for run in session.scalars(select(ForecastRun).where(ForecastRun.farm_id == farm.id)):
        run.props = {**run.props, "as_issued": False, "label": "Synthetic reconstructed forecast"}
    body = _get(client, session, settings, "/forecast/today", _utc(9, 4))
    visible = re.sub(
        r"<details\b[^>]*data-forecast-method[^>]*>.*?</details>", "", body, flags=re.S
    )
    assert "Forecast run 9 Apr 00:00 UTC" in visible
    assert "available 9 Apr 00:00 UTC" in visible
    assert "Synthetic reconstructed forecast" in visible
    assert "not the forecast as it was issued" in visible

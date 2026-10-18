"""Plan and Home share one replay-aware forecast/availability view."""

from datetime import UTC, datetime

from farmcore.core import clock
from tests.apps.forecast.test_plan_calendar import top3
from tests.apps.forecast.test_routes import _days
from tests.apps.forecast.test_routes import imported as imported


def test_plan_has_fourteen_dates_and_home_previews_the_same_suggestions(
    client, session, settings, farm, imported
):
    clock.set_replay(session, datetime(2024, 4, 9, 4, tzinfo=UTC), settings)
    session.commit()
    page = client.get("/forecast/plan", params={"farm": farm.id})
    assert page.status_code == 200
    days = _days(page.text)
    assert len(days) == 14 and len(dict(days)) == 14
    assert days[0][0] == "2024-04-09" and days[-1] == ("2024-04-22", "no_forecast")
    assert 'id="plan-days"' in page.text
    assert "No forecast yet" in page.text
    assert "Spraying weather" in page.text and "data-field-week" in page.text
    opened = client.get("/forecast/plan", params={"farm": farm.id, "day": "2024-04-10"}).text
    assert "Current advice" in opened or "No current treatment window" in opened
    assert sorted(_days(client.get("/forecast/today").text)) == top3(days)
    fragment = client.get("/forecast/plan/days", params={"farm": farm.id})
    assert fragment.status_code == 200 and "<html" not in fragment.text
    assert _days(fragment.text) == days
    assert f'hx-get="/forecast/plan/days?day=2024-04-09&amp;farm={farm.id}"' in fragment.text
    assert "data-poll-feed" in fragment.text


def test_busy_toggles_keep_unknown_weather_honest_and_rerank_home_identically(
    client, session, settings, farm, imported
):
    clock.set_replay(session, datetime(2024, 4, 9, 4, tzinfo=UTC), settings)
    session.commit()
    for day in ("2024-04-09", "2024-04-22"):
        response = client.get(
            "/forecast/busy",
            params={
                "day": day,
                "busy": "1",
                "farm": farm.id,
                "next": f"/forecast/plan?farm={farm.id}&day={day}",
            },
        )
        assert response.status_code == 200
    days = dict(_days(response.text))
    assert days["2024-04-09"] == "busy"
    assert days["2024-04-22"] == "no_forecast"
    assert 'id="plan-busy-2024-04-22"' in response.text
    assert sorted(_days(client.get("/forecast/today").text)) == top3(_days(response.text))
    assert 'hx-select="#plan-days"' in response.text
    assert f'name="farm" value="{farm.id}"' in response.text


def test_plan_replans_busy_advice_and_keeps_other_current_windows(
    client, session, settings, farm, imported
):
    from farmcore.core.models.canonical import Advice
    from tests.apps.share.conftest import make_parcel

    now = datetime(2024, 4, 9, 4, tzinfo=UTC)
    p1, p2, p3 = [make_parcel(session, farm, n) for n in (1, 2, 3)]
    session.add_all(
        [
            Advice(
                farm_id=farm.id,
                parcel_id=p1.id,
                evaluated_at=now,
                decision="spray",
                next_window_start=now,
                next_window_end=now.replace(hour=6),
                reasons=[],
                products=[],
            ),
            Advice(
                farm_id=farm.id,
                parcel_id=p2.id,
                evaluated_at=now,
                decision="wait",
                next_window_start=now.replace(day=10),
                reasons=[],
                products=[],
            ),
            Advice(
                farm_id=farm.id,
                parcel_id=p3.id,
                evaluated_at=now,
                decision="spray",
                reasons=[],
                products=[],
            ),
        ]
    )
    clock.set_replay(session, now, settings)
    session.commit()
    client.cookies.set(f"farmcore_forecast_busy_{farm.id}", "2024-04-09")
    html = client.get("/forecast/plan", params={"farm": farm.id}).text
    today = client.get("/forecast/plan", params={"farm": farm.id, "day": "2024-04-09"}).text
    tomorrow = client.get("/forecast/plan", params={"farm": farm.id, "day": "2024-04-10"}).text
    assert html.count("data-has-advice") == 1  # both windows now belong to tomorrow
    assert f'data-advice-parcel="{p1.id}"' not in today
    assert f'data-advice-parcel="{p1.id}"' in tomorrow
    assert f'data-advice-parcel="{p2.id}"' not in today
    assert f'data-advice-parcel="{p2.id}"' in tomorrow
    assert 'data-rescheduled-to="2024-04-10"' in today
    assert "Rescheduled from Tue 9 Apr" in tomorrow
    assert f'data-needs-window="{p3.id}"' in html


def test_home_never_picks_a_different_best_than_the_fourteen_day_plan(
    client, session, settings, farm
):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    from farmcore.apps.forecast import sources, store

    begin = datetime(2024, 4, 8, 22, tzinfo=UTC)
    values = []
    for i in range(194):
        ts = begin + timedelta(hours=i)
        wind = 2 if ts.astimezone(ZoneInfo("Europe/Paris")).day == 14 else 8
        values.extend(
            (ts, key, value) for key, value in (("ff_kmh", wind), ("rr1_mm", 0), ("t_c", 15))
        )
    store.import_runs(
        session,
        farm.id,
        [sources.Run("synthetic", begin, "Synthetic long horizon", True, None, None, values)],
    )
    clock.set_replay(session, begin + timedelta(hours=6), settings)
    session.commit()
    home = client.get("/forecast/today").text
    plan = client.get("/forecast/plan").text
    assert sorted(_days(home)) == top3(_days(plan))
    assert ("2024-04-14", "best") in _days(plan) and ("2024-04-14", "best") in _days(home)
    assert "/forecast/plan" in home


def test_after_hours_today_stays_pinned_without_a_fifteenth_date(
    client, session, settings, farm, imported
):
    clock.set_replay(session, datetime(2024, 4, 9, 19, tzinfo=UTC), settings)
    session.commit()
    html = client.get("/forecast/plan").text
    days = _days(html)
    assert len(days) == 14 and days[0] == ("2024-04-09", "passed")
    assert days[-1][0] == "2024-04-22"
    assert "Today’s spray hours have passed" in html
    assert "· Europe/Paris" in html
    assert ("2024-04-09", "passed") not in _days(client.get("/forecast/today").text)
    assert sorted(_days(client.get("/forecast/today").text)) == top3(days)


def test_plan_dst_dates_and_weather_completeness_count_the_actual_local_hours(
    client, session, settings, farm
):
    from datetime import date

    from farmcore.apps.forecast.weather import day_weather

    clock.set_replay(session, datetime(2024, 3, 30, 23, tzinfo=UTC), settings)
    session.commit()
    days = _days(client.get("/forecast/plan").text)
    assert len(days) == 14 and days[0][0] == "2024-03-31" and days[-1][0] == "2024-04-13"
    assert day_weather(None, date(2024, 3, 31), "Europe/Paris")["expected"] == 23
    assert day_weather(None, date(2024, 10, 27), "Europe/Paris")["expected"] == 25


def test_plan_tour_reads_and_busy_remain_farm_local_and_app_off_hides_the_page(
    client, session, settings, farm, imported, connection
):
    import re

    from sqlalchemy import event

    from farmcore.core.access import ensure_owner_grant
    from farmcore.core.models.canonical import Farm
    from farmcore.core.models.kernel import AppModule

    es = Farm(
        name="Spanish demo archive",
        props={"advisor_unavailable_reason": "Advisor withheld: timestamp basis unverified"},
    )
    session.add(es)
    session.flush()
    ensure_owner_grant(session, es)
    clock.set_replay(session, datetime(2024, 4, 9, 4, tzinfo=UTC), settings)
    session.commit()
    client.cookies.set("farmcore_tour", "1")
    response = client.get("/forecast/plan", params={"farm": es.id})
    assert response.status_code == 200
    assert "Advisor withheld" in response.text and "Synthetic test forecast" not in response.text
    assert len(_days(response.text)) == 14
    assert all(grade == "no_forecast" for _, grade in _days(response.text))
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(connection, "before_cursor_execute", record)
    try:
        response = client.get(
            "/forecast/busy",
            follow_redirects=False,
            params={
                "farm": es.id,
                "day": "2024-04-09",
                "busy": "1",
                "next": f"/forecast/plan?farm={es.id}",
            },
        )
    finally:
        event.remove(connection, "before_cursor_execute", record)
    assert not any(re.match(r"\s*(INSERT|UPDATE|DELETE)\b", s, re.I) for s in statements)
    assert f"farmcore_forecast_busy_{es.id}" in client.cookies
    assert f"farmcore_forecast_busy_{farm.id}" not in client.cookies
    assert (
        dict(_days(client.get("/forecast/today", params={"farm": farm.id}).text))["2024-04-09"]
        == "best"
    )
    session.get(AppModule, "forecast").state = "uninstalled"
    session.commit()
    assert client.get("/forecast/plan").status_code == 404
    assert client.get("/forecast/plan/days").text == ""


def test_unverified_spanish_archive_does_not_claim_spray_hours_have_passed(
    client, session, settings, farm
):
    from farmcore.core.access import ensure_owner_grant
    from farmcore.core.models.canonical import Farm

    es = Farm(
        name="Spanish archive",
        props={
            "region": "ES418",
            "advisor_unavailable_reason": "Advisor withheld: unverified time basis",
        },
    )
    session.add(es)
    session.flush()
    ensure_owner_grant(session, es)
    clock.set_replay(session, datetime(2024, 4, 9, 19, tzinfo=UTC), settings)
    session.commit()
    html = client.get("/forecast/plan", params={"farm": es.id}).text
    assert all(grade == "no_forecast" for _, grade in _days(html))
    assert "spray hours have passed" not in html
    assert "Advisor withheld" in html


def test_plan_intro_is_one_line_with_details_disclosure(client, session, settings, farm):
    page = client.get("/forecast/plan", params={"farm": farm.id}).text
    main = page.split('<main id="main"', 1)[1].split("</main>", 1)[0]
    assert main.count("<details") == 1
    assert "this browser" in main.split("<details", 1)[1]
    assert "this browser" not in main.split("<details", 1)[0]

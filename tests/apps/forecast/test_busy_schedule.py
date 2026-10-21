"""Availability replans calendar treatment windows through the public HTTP routes."""

import re
from datetime import UTC, datetime, timedelta

import pytest

from farmcore.apps.forecast import sources, store
from farmcore.core import clock
from farmcore.core.models.canonical import Advice
from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.forecast.test_routes import parcel as parcel


def _schedule(session, settings, farm, parcel, winds, start_day=9):
    now = datetime(2024, 4, start_day, 4, tzinfo=UTC)
    complete_station(session, farm.id, now)
    begin = datetime(2024, 4, 9, tzinfo=UTC)
    values = []
    for i in range(150):
        ts = begin + timedelta(hours=i)
        wind = winds.get(ts.day, 8)
        values.extend((ts, k, v) for k, v in (("ff_kmh", wind), ("rr1_mm", 0), ("t_c", 15)))
    store.import_runs(
        session,
        farm.id,
        [sources.Run("schedule", begin, "Schedule weather", True, None, None, values)],
    )
    row = Advice(
        farm_id=farm.id,
        parcel_id=parcel.id,
        evaluated_at=now,
        decision="spray",
        next_window_start=now,
        next_window_end=now.replace(hour=16),
        reasons=[],
        products=[],
    )
    session.add(row)
    clock.set_replay(session, now, settings)
    session.commit()
    return row


@pytest.fixture
def scheduled(session, settings, farm, parcel):
    # The 11th has the best wind; the 10th is the next best available day.
    return _schedule(session, settings, farm, parcel, {10: 4, 11: 2})


def _mark(client, farm, day, busy="1"):
    return client.get(
        "/forecast/busy",
        params={
            "farm": farm.id,
            "day": f"2024-04-{day:02}",
            "busy": busy,
            "next": f"/forecast/plan?farm={farm.id}&day=2024-04-{day:02}",
        },
    ).text


def _detail(client, farm, day):
    html = client.get("/forecast/plan", params={"farm": farm.id, "day": day}).text
    return html.split('id="plan-detail"', 1)[1]


def test_busy_automatically_moves_treatment_to_earliest_free_window_and_free_restores_it(
    client,
    session,
    farm,
    parcel,
    scheduled,
):
    html = _mark(client, farm, 9)
    assert 'data-rescheduled-to="2024-04-10"' in html
    assert f'data-advice-parcel="{parcel.id}"' not in html.split('id="plan-detail"', 1)[1]
    moved = _detail(client, farm, "2024-04-10")
    assert f'data-advice-parcel="{parcel.id}"' in moved
    assert "Rescheduled from Tue 9 Apr" in moved
    _mark(client, farm, 10)
    assert f'data-advice-parcel="{parcel.id}"' in _detail(client, farm, "2024-04-11")
    html = _mark(client, farm, 9, "0")
    assert f'data-advice-parcel="{parcel.id}"' in html.split('id="plan-detail"', 1)[1]
    assert "data-rescheduled-to" not in html
    # Browser planning never rewrites the engine's evaluation or a completed spray.
    session.refresh(scheduled)
    assert scheduled.next_window_start == datetime(2024, 4, 9, 4, tzinfo=UTC)
    assert scheduled.decision == "spray"


def test_busy_day_moves_treatment_to_the_next_open_day_not_the_longest_window(
    client, session, settings, farm, parcel
):
    # Fri 12 is calmer than Thu 11, but both are suitable: the earlier one wins.
    _schedule(session, settings, farm, parcel, {11: 6, 12: 2}, start_day=10)
    html = _mark(client, farm, 10)
    assert 'data-rescheduled-to="2024-04-11"' in html
    assert f'data-advice-parcel="{parcel.id}"' in _detail(client, farm, "2024-04-11")


def test_all_busy_leaves_treatment_needing_window_instead_of_scheduling_unsuitable_weather(
    client,
    farm,
    parcel,
    scheduled,
):
    for day in range(9, 13):
        html = _mark(client, farm, day)
    assert f'data-needs-window="{parcel.id}"' in html
    assert "No suitable available window" in html
    assert not re.search(r'data-advice-parcel="', html)


def test_parcel_weather_shows_the_same_rescheduled_treatment_window(
    client,
    farm,
    parcel,
    scheduled,
):
    _mark(client, farm, 9)
    html = client.get(f"/forecast/parcels/{parcel.id}", params={"farm": farm.id}).text
    assert 'data-planned-treatment="2024-04-10"' in html
    assert "Rescheduled from Tue 9 Apr" in html
    for day in (10, 11, 12):
        _mark(client, farm, day)
    html = client.get(f"/forecast/parcels/{parcel.id}", params={"farm": farm.id}).text
    assert "No suitable available window" in html


@pytest.mark.parametrize("blocked_by", ["sulphur_heat", "wind", "missing", "horizon"])
def test_rescheduling_rejects_product_unsafe_unknown_and_out_of_horizon_windows(
    client,
    session,
    farm,
    parcel,
    scheduled,
    blocked_by,
):
    now = datetime(2024, 4, 9, 4, tzinfo=UTC)
    values = []
    for i in range(150):
        ts = now.replace(hour=0) + timedelta(hours=i)
        wind = 60 if blocked_by == "wind" or (blocked_by == "horizon" and ts.day < 13) else 2
        temp = 40 if blocked_by == "sulphur_heat" else 15
        values.extend((ts, k, v) for k, v in (("ff_kmh", wind), ("t_c", temp)))
        if blocked_by != "missing":
            values.append((ts, "rr1_mm", 0))
    store.import_runs(
        session,
        farm.id,
        [
            sources.Run(
                "restricted",
                now,
                "Restricted weather",
                True,
                None,
                None,
                values,
            )
        ],
    )
    if blocked_by == "sulphur_heat":
        scheduled.products = [{"name": "Sulphur", "targets": ["powdery"]}]
    session.commit()
    html = _mark(client, farm, 9)
    assert f'data-needs-window="{parcel.id}"' in html
    assert "No suitable available window" in html
    assert "data-rescheduled-to" not in html


def test_default_best_day_workspace_link_preserves_selected_farm_and_anchor(
    client,
    session,
    farm,
    parcel,
    scheduled,
):
    import html
    from urllib.parse import parse_qs, urlsplit

    scheduled.next_window_start = datetime(2024, 4, 11, 4, tzinfo=UTC)
    scheduled.next_window_end = datetime(2024, 4, 11, 16, tzinfo=UTC)
    session.commit()
    body = client.get("/forecast/plan", params={"farm": farm.id}).text
    assert 'data-plan-detail="2024-04-11"' in body
    targets = [
        html.unescape(u)
        for u in re.findall(r'href="([^"]+)"', body)
        if urlsplit(html.unescape(u)).path == f"/fields/parcels/{parcel.id}"
    ]
    assert len(targets) == 1
    origin = parse_qs(urlsplit(targets[0]).query)["return_to"][0]
    assert parse_qs(urlsplit(origin).query) == {"farm": [str(farm.id)], "day": ["2024-04-11"]}
    assert urlsplit(origin).fragment == "plan-day-2024-04-11"
    returned = client.get(origin)
    assert 'id="plan-day-2024-04-11"' in returned.text
    assert 'id="plan-heading"' in returned.text
    assert "/static/forecast/calendar.css" in body and "/static/forecast/calendar.js" in body


def test_undated_workspace_link_returns_to_scoped_focusable_plan_heading(
    client,
    session,
    farm,
    parcel,
    scheduled,
):
    import html
    from urllib.parse import parse_qs, urlsplit

    scheduled.next_window_start = scheduled.next_window_end = None
    session.commit()
    body = client.get("/forecast/plan", params={"farm": farm.id, "day": "2024-04-11"}).text
    target = next(
        html.unescape(u)
        for u in re.findall(r'href="([^"]+)"', body)
        if urlsplit(html.unescape(u)).path == f"/fields/parcels/{parcel.id}"
    )
    origin = parse_qs(urlsplit(target).query)["return_to"][0]
    assert urlsplit(origin).path == "/forecast/plan" and urlsplit(origin).fragment == "plan-heading"
    assert parse_qs(urlsplit(origin).query) == {"farm": [str(farm.id)]}
    workspace = client.get(target)
    assert "Back to Plan" in workspace.text and html.escape(origin, quote=True) in workspace.text
    returned = client.get(origin)
    assert re.search(r'<h1[^>]*id="plan-heading"[^>]*tabindex="-1"', returned.text)


def test_rescheduled_selected_day_survives_workspace_named_return(
    client, session, farm, parcel, scheduled
):
    import html
    from urllib.parse import parse_qs, urlsplit

    _mark(client, farm, 9)
    before = dict(client.cookies)
    body = client.get("/forecast/plan", params={"farm": farm.id, "day": "2024-04-10"}).text
    target = next(
        html.unescape(u)
        for u in re.findall(r'href="([^"]+)"', body)
        if urlsplit(html.unescape(u)).path == f"/fields/parcels/{parcel.id}"
    )
    origin = parse_qs(urlsplit(target).query)["return_to"][0]
    assert parse_qs(urlsplit(origin).query) == {"farm": [str(farm.id)], "day": ["2024-04-10"]}
    assert urlsplit(origin).fragment == "plan-day-2024-04-10"
    workspace = client.get(target)
    assert "Back to Plan" in workspace.text
    returned = client.get(origin)
    assert 'data-plan-detail="2024-04-10"' in returned.text
    assert f'data-advice-parcel="{parcel.id}"' in returned.text
    assert "Rescheduled from Tue 9 Apr" in returned.text
    assert dict(client.cookies) == before
    session.refresh(scheduled)
    assert scheduled.next_window_start == datetime(2024, 4, 9, 4, tzinfo=UTC)

"""Plan as a real two-week calendar; Home previews only the top three days of the same result."""

import re
from datetime import UTC, datetime
from pathlib import Path

from farmcore.apps import forecast
from farmcore.core import clock
from tests.apps.forecast.test_routes import _days
from tests.apps.forecast.test_routes import imported as imported

RANK = {"best": 0, "good": 1, "possible": 2}  # Home never suggests an Avoid day


def top3(days):
    """What Home must show: the three best graded days of the plan, in date order."""
    ranked = sorted(
        ((i, d, g) for i, (d, g) in enumerate(days) if g in RANK), key=lambda t: (RANK[t[2]], t[0])
    )
    return sorted((d, g) for _, d, g in ranked[:3])


def _plan(client, session, settings, farm, query=""):
    clock.set_replay(session, datetime(2024, 4, 9, 4, tzinfo=UTC), settings)
    session.commit()
    return client.get(f"/forecast/plan?farm={farm.id}{query}").text


def test_field_week_keeps_fourteen_readable_days_for_navigation_and_native_fallback(
    client, session, settings, farm, imported
):
    html = _plan(client, session, settings, farm)
    assert len(re.findall(r"data-weekday-header", html)) == 14
    assert re.findall(r'data-weekday="([A-Za-z]+)"', html) == [
        "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Mon"
    ] * 2  # fmt: skip
    cells = re.findall(r"<li [^>]*data-plan-cell[^>]*>", html)
    assert len(cells) == 14
    assert "data-plan-grid" in html
    assert cells[0].count('aria-current="date"') == 1
    assert sum('aria-current="date"' in c for c in cells) == 1


def test_each_cell_has_date_word_icon_and_opens_the_availability_panel(
    client, session, settings, farm, imported
):
    html = _plan(client, session, settings, farm)
    cell = re.search(r'<li [^>]*data-day="2024-04-10".*?</li>', html, re.S).group(0)
    assert re.search(r"data-plan-date[^>]*>\s*10\s*<", cell)
    assert "data-plan-word" in cell and "<svg" in cell
    assert "/forecast/plan?day=2024-04-10" in cell
    opened = _plan(client, session, settings, farm, "&day=2024-04-10")
    detail = opened.split('id="plan-detail"', 1)[1]
    assert 'id="plan-busy-2024-04-10"' in detail and 'method="get"' in detail
    assert "/forecast/busy" in detail
    assert re.search(r"data-plan-word[^>]*>\s*(Best|Good|Maybe|Avoid)\s*<", cell)
    no_forecast = re.search(r'<li [^>]*data-day="2024-04-22".*?</li>', html, re.S).group(0)
    assert "No forecast" in no_forecast


def test_tapping_a_day_opens_details_below_and_is_deep_linkable(
    client, session, settings, farm, imported
):
    closed = _plan(client, session, settings, farm)
    assert 'data-plan-detail="2024-04-09"' in closed
    html = _plan(client, session, settings, farm, "&day=2024-04-10")
    assert html.count("data-plan-detail") == 1
    assert 'data-plan-detail="2024-04-10"' in html
    assert html.index("data-plan-detail") > html.rindex("data-plan-cell")
    assert len(re.findall(r'aria-current="true"', html)) == 1
    detail = html.split("data-plan-detail", 1)[1]
    assert "Current advice" in detail or "No current treatment window" in detail
    assert 'data-day="2024-04-10" data-grade' not in detail  # grid stays the one source of cells
    assert len(_days(html)) == 14
    # polling and busy keep the opened day
    assert "day=2024-04-10" in re.search(r'hx-get="([^"]*plan/days[^"]*)"', html).group(1)
    assert "day%3D2024-04-10" in html or "day=2024-04-10" in html


def test_bad_or_out_of_range_day_is_ignored(client, session, settings, farm, imported):
    for bad in ("nonsense", "2024-05-30", "2024-04-01"):
        html = _plan(client, session, settings, farm, f"&day={bad}")
        assert 'data-plan-detail="2024-04-09"' in html


def test_home_window_is_the_top_three_days_of_the_shared_plan(
    client, session, settings, farm, imported
):
    plan = _days(_plan(client, session, settings, farm))
    home_html = client.get("/forecast/today").text
    home = _days(home_html)
    assert len(home) == 3 and sorted(home) == top3(plan)
    assert "See Plan" in home_html and "/forecast/plan" in home_html


def test_home_window_skips_busy_days_when_choosing_the_top_three(
    client, session, settings, farm, imported
):
    _plan(client, session, settings, farm)
    first = _days(client.get("/forecast/today").text)
    best = first[0][0]
    client.get("/forecast/busy", params={"day": best, "busy": "1"})
    after = _days(client.get("/forecast/today").text)
    assert best not in [d for d, _ in after] and len(after) == 3


def _cell(html, day):
    return re.search(rf'<li [^>]*data-day="{day}".*?</li>', html, re.S).group(0)


def test_cell_busy_control_is_a_fixed_label_toggle_with_state(
    client, session, settings, farm, imported
):
    html = _plan(client, session, settings, farm, "&day=2024-04-11")
    detail = html.split('id="plan-detail"', 1)[1]
    button = re.search(r'<button id="plan-busy-2024-04-11".*?</button>', detail, re.S).group(0)
    assert 'aria-pressed="false"' in button and 'aria-label="Busy on Thu 11 Apr"' in button
    assert "<svg" not in button and ">Busy<" in button
    assert "text-spray" in button
    client.get("/forecast/busy", params={"day": "2024-04-11", "busy": "1"})
    html = client.get(f"/forecast/plan?farm={farm.id}&day=2024-04-11").text
    cell = _cell(html, "2024-04-11")
    detail = html.split('id="plan-detail"', 1)[1]
    button = re.search(r'<button id="plan-busy-2024-04-11".*?</button>', detail, re.S).group(0)
    assert 'aria-pressed="true"' in button and 'aria-label="Busy on Thu 11 Apr"' in button
    assert ">Busy<" in button and "bg-spray-soft" in button
    assert "Unavailable for spraying" in button
    chip = re.search(r"<span data-plan-word.*?</span>", cell, re.S).group(0)
    assert "Busy" in chip


def test_no_calendar_text_below_twelve_px_on_a_phone(client, session, settings, farm, imported):
    html = _plan(client, session, settings, farm, "&day=2024-04-10")
    grid = html.split('id="plan-days"', 1)[1].split('id="plan-detail"', 1)[0]
    assert not re.findall(r"(?<![:\w-])text-\[(?:[0-9]|1[01])px\]", grid)


def test_day_link_updates_the_plan_and_keeps_a_native_detail_anchor(
    client, session, settings, farm, imported
):
    html = _plan(client, session, settings, farm, "&day=2024-04-10")
    assert "aria-expanded" not in html
    cell = _cell(html, "2024-04-10")
    assert 'aria-current="true"' in cell and 'aria-controls="plan-detail"' in cell
    assert 'hx-swap="outerHTML"' in cell
    assert '#plan-detail"' in cell


def test_grid_has_a_legend_for_dash_and_busy_toggle(client, session, settings, farm, imported):
    html = _plan(client, session, settings, farm)
    legend = re.search(r"<p [^>]*data-plan-legend.*?</p>", html, re.S).group(0)
    assert (
        "no forecast yet" in legend.lower()
        and "busy" in legend.lower()
        and "tap again" in legend.lower()
        and "<svg" not in legend
    )


def test_home_top_three_never_includes_an_avoid_day(client, session, settings, farm):
    from farmcore.apps.forecast import planning

    days = [
        {"day": f"2024-04-{n:02}", "grade": g, "eligible": True}
        for n, g in ((9, "avoid"), (10, "good"), (11, "avoid"), (12, "possible"), (13, "avoid"))
    ]
    assert [d["grade"] for d in planning.top_days(days)] == ["good", "possible"]


def test_field_week_opens_best_day_with_one_availability_control_and_source_disclosure(
    client, session, settings, farm, imported
):
    html = _plan(client, session, settings, farm)
    assert "data-field-week" in html
    assert 'data-plan-detail="2024-04-09"' in html
    assert html.count('action="/forecast/busy') == 1
    assert 'data-plan-nav="next"' in html and 'data-plan-nav="previous"' in html
    assert html.index("data-best-window") < html.index("data-plan-grid")
    assert html.index("data-plan-sources") > html.index('id="plan-detail"')
    assert 'id="plan-busy-2024-04-09"' in html


def test_paging_buttons_keep_a_44px_target_on_phones():
    css = (Path(forecast.__file__).parent / "static" / "calendar.css").read_text()
    for rule in re.findall(r"\.fc-plan-toolbar button\{([^}]*)\}", css):
        assert not re.search(r"min-(?:width|height):(?:[0-3]\d|4[0-3])px", rule)

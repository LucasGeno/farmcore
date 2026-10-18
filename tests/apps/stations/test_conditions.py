"""Spray conditions per hour from the station's hourly rows (pure function, no DB).

A row at ts T describes the hour ending at T, so the cell for the hour 12:00-13:00 reads the
row at 13:00 and is labelled by its start, 12:00."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from farmcore.apps.stations.conditions import spray_conditions

TZ = "Europe/Paris"
NOW = datetime(2024, 4, 10, 10, 0, tzinfo=UTC)  # Wed 10 Apr 2024 12:00 Europe/Paris
H = timedelta(hours=1)
DRY = (0.0, 8.0)  # (rr1_mm, ff_kmh)


def _rows(hours: dict[int, tuple[float | None, float | None]]) -> list[dict]:
    """hours: offset of the row's ts from NOW -> (rr1_mm, ff_kmh)."""
    return [
        {"ts": NOW + H * k, "rr1_mm": r, "t_c": 12.0, "rh_pct": 70.0, "ff_kmh": w}
        for k, (r, w) in sorted(hours.items())
    ]


def _dry(last: int = 30) -> dict[int, tuple]:
    return dict.fromkeys(range(-2, last + 1), DRY)


def _states(hours: dict[int, tuple], n: int = 24) -> list[str]:
    return [c["state"] for c in spray_conditions(_rows(hours), NOW, TZ, hours=n)["hours"]]


def test_cells_are_labelled_by_their_start_hour_and_read_the_row_at_their_end() -> None:
    hours = _dry()
    hours[3] = (1.0, 8.0)  # the hour 14:00-15:00 local
    result = spray_conditions(_rows(hours), NOW, TZ)
    cells = result["hours"]
    assert len(cells) == 24
    assert cells[0]["label"] == "12:00" and cells[0]["ts"] == NOW
    assert cells[0]["now"] and not cells[1]["now"]
    assert cells[2]["label"] == "14:00" and cells[2]["state"] == "no"
    assert "Rain 1.0 mm" in cells[2]["reason"]


def test_good_when_dry_and_calm() -> None:
    cell = spray_conditions(_rows(_dry()), NOW, TZ)["hours"][0]
    assert cell["state"] == "good"
    assert cell["reason"] == "Wind 8 km/h, dry"


def test_wind_thresholds_12_marginal_19_no() -> None:
    for wind, state in [(11.9, "good"), (12.0, "marginal"), (18.9, "marginal"), (19.0, "no")]:
        hours = _dry()
        hours[1] = (0.0, wind)
        assert _states(hours)[0] == state, wind


def test_rain_from_one_gauge_tip_is_no() -> None:
    hours = _dry()
    hours[1] = (0.1, 8.0)  # below one tip: not rain
    assert _states(hours)[0] == "good"
    hours[1] = (0.2, 8.0)
    assert _states(hours)[0] == "no"


def test_rain_in_the_2_hours_after_is_marginal() -> None:
    hours = _dry()
    hours[5] = (0.6, 8.0)  # rain 16:00-17:00
    states = _states(hours)
    # 13:00 (rain 3 h after) good; 14:00 and 15:00 marginal; 16:00 no; 17:00 good
    assert states[1:6] == ["good", "marginal", "marginal", "no", "good"]
    assert "Rain 0.6 mm within 2 h" in spray_conditions(_rows(hours), NOW, TZ)["hours"][2]["reason"]


def test_missing_wind_or_rain_is_unknown_never_good() -> None:
    hours = _dry()
    hours[1] = (0.0, None)
    hours[2] = (None, 8.0)
    states = _states(hours)
    assert states[0] == "unknown" and states[1] == "unknown"


def test_a_known_no_wins_over_a_missing_value() -> None:
    hours = _dry()
    hours[1] = (None, 25.0)
    hours[2] = (1.0, None)
    assert _states(hours)[:2] == ["no", "no"]


def test_missing_rain_after_is_unknown_unless_already_marginal() -> None:
    hours = _dry()
    hours[3] = (None, 8.0)
    states = _states(hours)
    assert states[0] == "unknown"  # could be wash-off rain
    hours[1] = (0.0, 14.0)
    assert _states(hours)[0] == "marginal"


def test_summary_good_until_the_end_of_the_last_good_hour() -> None:
    hours = _dry()
    hours[3] = (0.0, 22.0)  # 14:00-15:00 too windy
    hours[4] = (0.0, 22.0)
    assert spray_conditions(_rows(hours), NOW, TZ)["summary"] == ("Dry, light wind until 14:00")


def test_summary_good_all_the_next_24_h() -> None:
    result = spray_conditions(_rows(_dry()), NOW, TZ)
    assert result["summary"] == "Dry, light wind all the next 24 h"
    assert result["state"] == "good"


def test_summary_too_windy_then_good() -> None:
    hours = _dry()
    for k in (1, 2, 3):
        hours[k] = (0.0, 25.0)
    result = spray_conditions(_rows(hours), NOW, TZ)
    assert result["summary"] == "Strong wind until 15:00, then dry, light wind"
    assert result["state"] == "no"


def test_summary_rain_no_spraying_until_and_good_from_midnight() -> None:
    hours = _dry()
    for k in range(1, 5):  # rain 12:00-16:00
        hours[k] = (1.0, 8.0)
    for k in (5, 6):  # windy 16:00-18:00
        hours[k] = (0.0, 25.0)
    for k in range(7, 13):  # windy too until midnight
        hours[k] = (0.0, 25.0)
    assert spray_conditions(_rows(hours), NOW, TZ)["summary"] == (
        "Rain until 16:00, dry, light wind from midnight"
    )


def test_summary_marginal_now() -> None:
    hours = _dry()
    hours[1] = (0.0, 14.0)
    assert spray_conditions(_rows(hours), NOW, TZ)["summary"] == (
        "Breezy until 13:00, then dry, light wind"
    )
    hours = _dry()
    hours[2] = (0.4, 8.0)
    assert spray_conditions(_rows(hours), NOW, TZ)["summary"] == (
        "Rain soon until 13:00, dry, light wind from 14:00"
    )


def test_summary_never_good_in_the_window() -> None:
    hours = {k: (0.0, 30.0) for k in range(-2, 31)}
    assert spray_conditions(_rows(hours), NOW, TZ)["summary"] == ("Strong wind all the next 24 h")


def test_no_rows_after_now_is_no_forecast() -> None:
    result = spray_conditions(_rows(dict.fromkeys(range(-5, 1), DRY)), NOW, TZ)
    assert result["summary"] == "No forecast"
    assert result["state"] == "none"
    assert {c["state"] for c in result["hours"]} == {"unknown"}


def test_short_forecast_never_claims_24_h_and_marks_the_rest_unknown() -> None:
    # Rows up to now + 12 h: the cells that can be judged (own hour + 2 h after) end at 21:00.
    result = spray_conditions(_rows(_dry(last=12)), NOW, TZ)
    states = [c["state"] for c in result["hours"]]
    assert states[:10] == ["good"] * 10
    assert set(states[10:]) == {"unknown"}
    assert result["summary"] == "Dry, light wind for the next 10 h"
    assert "beyond the forecast" in result["hours"][12]["reason"].lower()


def test_hours_on_the_next_day_read_tomorrow() -> None:
    hours = _dry()
    for k in range(15, 31):  # windy from 02:00 local
        hours[k] = (0.0, 25.0)
    assert spray_conditions(_rows(hours), NOW, TZ)["summary"] == (
        "Dry, light wind until tomorrow 02:00"
    )


def test_short_forecast_never_good_says_n_hours() -> None:
    hours = {k: (0.0, 30.0) for k in range(-2, 7)}
    assert spray_conditions(_rows(hours), NOW, TZ)["summary"] == ("Strong wind for the next 4 h")


def test_now_between_hours_starts_at_the_current_hour() -> None:
    result = spray_conditions(_rows(_dry()), NOW + timedelta(minutes=40), TZ)
    assert result["hours"][0]["label"] == "12:00" and result["hours"][0]["now"]


def test_short_windy_forecast_preserves_known_danger_and_renders_cells():
    from pathlib import Path

    from jinja2 import Environment, FileSystemLoader

    from farmcore.apps import stations
    from farmcore.apps.stations.conditions import rules

    result = spray_conditions(_rows({1: (0.0, 30.0), 2: (0.0, 30.0)}), NOW, TZ)
    assert result["covered"] == 0  # insufficient wash-off horizon
    assert result["state"] == "no"
    assert "wind" in result["summary"].lower()
    assert "incomplete" in result["summary"].lower()
    assert [c["state"] for c in result["hours"]] == ["no", "no"] + ["unknown"] * 22
    env = Environment(loader=FileSystemLoader(Path(stations.__file__).parent / "templates"))
    html = env.get_template("stations/_spray.html").render(spray=result, spray_rules=rules())
    assert html.count('data-spray-cell="no"') == 2
    assert html.count('data-spray-cell="unknown"') == 22
    assert "Wind 30 km/h" in html


def test_19_kmh_flag_is_factual_while_advisor_boundary_stays_open():
    from farmcore.apps.advisor.engine import AdvisorConfig, HourlyObs
    from farmcore.apps.advisor.engine.window import spray_window

    rows = _rows({k: (0.0, 19.0) for k in range(0, 76)})
    window = spray_window(
        [HourlyObs(**r) for r in rows], AdvisorConfig.for_region_year("FRI12", 2024), NOW
    )
    result = spray_conditions(rows, NOW, TZ)
    assert window.open_now is True
    assert result["hours"][0]["reason"] == "Wind 19 km/h"
    assert result["summary"] == "Strong wind all the next 24 h"

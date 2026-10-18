"""The spray conditions card at the top of the Weather page, and its one line on the Today
weather card. Synthetic hours on the seeded demo station; replay at 10 Apr 2024 06:00 local."""

from __future__ import annotations

import html
import re

from farmcore.core.clock import set_replay
from tests.apps.stations.test_today_widget import NOW, _card, _dry, _hours, _station, _text


def _page(client, station) -> str:
    resp = client.get(f"/stations/{station.id}")
    assert resp.status_code == 200
    return html.unescape(resp.text)


def test_weather_page_opens_with_the_spray_card_before_the_charts(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    station = _station(session)
    hours = _dry(range(-3, 30))
    hours[1] = (0.0, 12.0, 70.0, 25.0)  # 06:00-07:00 too windy (rr1, t, rh, wind)
    _hours(session, station, hours)
    body = _page(client, station)
    card = body.index('id="spray-conditions"')
    assert card < body.index("chart-rain-temp") and card < body.index("Import health")
    text = _text(body)
    assert "Weather flags · next 24 h" in text
    assert "Strong wind until 07:00, then dry, light wind" in text
    assert (
        "Conservative weather view: gauge-tip rain and wind flags. "
        "See Crop protection for whether and when to apply." in text
    )
    assert "How weather is flagged" in text and "19 km/h" in text and "Beaufort" in text
    for word in ("Dry, light wind", "Caution", "Rain or strong wind", "No data"):
        assert word in text


def test_strip_has_24_labelled_cells_with_the_current_hour_first(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    station = _station(session)
    hours = _dry(range(-3, 30))
    hours[3] = (1.0, 12.0, 90.0, 8.0)  # rain 08:00-09:00
    _hours(session, station, hours)
    body = _page(client, station)
    cells = re.findall(r'data-spray-cell="(\w+)"', body)
    assert len(cells) == 24
    assert cells[:4] == ["marginal", "marginal", "no", "good"]
    labels = re.findall(r'<span role="img" aria-label="([^"]+)"', body)
    assert len(labels) == 24
    assert labels[0] == "Now, 06:00: Caution, Rain 1.0 mm within 2 h"
    assert labels[2] == "08:00: Weather flag, Rain 1.0 mm"
    assert ">✓<" in body and ">✕<" in body and ">~<" in body
    assert "outline-ink" in body.split('data-spray-cell="')[1]  # the current hour stands out
    assert "bg-spray" not in body.split('id="spray-conditions"')[1].split("</section>")[0]


def test_weather_page_without_forecast_says_so(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    station = _station(session)
    _hours(session, station, _dry(range(-3, 1)))
    body = _page(client, station)
    text = _text(body)
    assert "No forecast" in text
    assert 'data-spray-cell="' not in body


def test_today_card_adds_the_spray_line(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    hours = _dry(range(-1, 30))
    hours[5] = (0.0, 12.0, 70.0, 22.0)  # 10:00-11:00 too windy
    _hours(session, _station(session), hours)
    body = _card(client)
    line = re.search(r'data-spray-line="(\w+)"[^>]*>(.*?)</p>', body, re.S)
    assert line and line.group(1) == "good"
    assert "Dry, light wind until 10:00" in _text(line.group(2))
    assert ">✓<" in line.group(2)


def test_today_card_has_no_spray_line_without_forecast(client, session, seeded) -> None:
    seeded()
    set_replay(session, NOW)
    _hours(session, _station(session), _dry(range(-3, 1)))
    body = _card(client)
    assert "data-spray-line" not in body
    assert "Spray conditions" not in _text(body)

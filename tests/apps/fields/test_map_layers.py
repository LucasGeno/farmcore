"""The map's layer chips come from the visible apps' `map_layers` only
(`registry.visible_manifests`). Crop protection gives the "Spray advice" colouring (a "Colour by"
chip, one active), Weather an independent station toggle (off by default). Switching an app off
on /apps removes its chip, legend and actions at once."""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from farmcore.apps.fields.seed import seed
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Parcel
from farmcore.core.models.kernel import AppModule

NOW = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)
PAGES = ["/fields/", "/fields/today"]
ADVICE = {
    "key": "advice",
    "label": "Spray advice",
    "url": "/advisor/layer.geojson",
    "kind": "parcels",
    "default_on": True,
    "icon": "spray",
}
WEATHER = {
    "key": "weather",
    "label": "Weather",
    "url": "/stations/layer.geojson",
    "kind": "points",
    "default_on": False,
    "icon": "station",
}


@pytest.fixture
def seeded(session, synthetic_data_dir, settings):
    seed(session, scene=None, data_dir=synthetic_data_dir)
    set_replay(session, NOW, settings)
    session.commit()
    return session


def _switch_off(session, name: str) -> None:
    session.get(AppModule, name).state = "uninstalled"
    session.commit()


def _layers(page: str) -> list[dict]:
    match = re.search(r"data-layers='([^']*)'", page)
    assert match, "data-layers not found"
    return json.loads(html_lib.unescape(match.group(1)))


def _expected_layers(path, session, *layers):
    """Both Map and the Home preview freeze their authorized fixture farm."""
    farm_id = session.scalar(select(Parcel.farm_id).limit(1))
    return [{**layer, "url": f"{layer['url']}?farm={farm_id}"} for layer in layers]


def _chips(page: str) -> dict[str, str]:
    """layer key -> the chip's opening <button> tag."""
    tags = re.findall(r"<button[^>]*data-layer-chip=[^>]*>", page)
    return {re.search(r'data-layer-chip="([^"]+)"', tag).group(1): tag for tag in tags}


@pytest.mark.parametrize("path", PAGES)
def test_map_carries_the_visible_layers_in_order(client, seeded, path):
    page = client.get(path).text

    assert _layers(page) == _expected_layers(path, seeded, ADVICE, WEATHER)


@pytest.mark.parametrize("path", PAGES)
def test_one_colour_by_chip_is_active_and_weather_starts_off(client, seeded, path):
    page = client.get(path).text

    chips = _chips(page)
    assert set(chips) == {"advice", "weather"}
    assert 'aria-pressed="true"' in chips["advice"]
    assert 'aria-pressed="false"' in chips["weather"]
    assert 'data-layer-kind="parcels"' in chips["advice"]
    assert 'data-layer-kind="points"' in chips["weather"]
    pressed_parcels = [
        t for t in chips.values() if 'data-layer-kind="parcels"' in t and 'aria-pressed="true"' in t
    ]
    assert len(pressed_parcels) == 1
    assert all('type="button"' in t for t in chips.values())


@pytest.mark.parametrize("path", PAGES)
def test_chips_sit_in_one_scroll_row_and_the_legend_is_titled_after_the_colouring(
    client, seeded, path
):
    page = client.get(path).text

    row = re.search(r'<div[^>]*class="[^"]*\bfc-map-chips\b[^"]*"[^>]*>', page)
    assert row, "chip row"
    assert page.index(row.group(0)) < page.index("data-layer-chip")
    legend = re.search(r'<[^>]*data-map-legend[^>]*data-legend-for="advice"[^>]*>', page)
    assert legend, "legend for the advice layer"
    assert "Spray advice" in page[page.index(legend.group(0)) :].split("</ul>", 1)[0]
    # The basemap switch stays its own control, not a chip.
    assert "data-basemap-toggle" in page and 'data-layer-chip="satellite"' not in page


@pytest.mark.parametrize("path", PAGES)
def test_advisor_switched_off_leaves_neutral_parcels_without_advice(client, seeded, path):
    _switch_off(seeded, "advisor")

    page = client.get(path).text

    assert _layers(page) == _expected_layers(path, seeded, WEATHER)
    assert set(_chips(page)) == {"weather"}
    assert "data-map-legend" not in page
    assert "Open advice card" not in page
    assert "/advisor/" not in page
    assert "Spray advice" not in page


def test_advisor_switched_off_the_parcel_list_keeps_alias_and_name_only(client, seeded):
    _switch_off(seeded, "advisor")

    page = client.get("/fields/").text

    item = re.search(r'<li[^>]*data-parcel="P2"[^>]*>(.*?)</li>', page, re.S)
    assert item and "Synthetic Two" in item.group(1)
    assert "data-decision" not in item.group(1) and "No advice yet" not in item.group(1)
    assert "coloured by" not in page


@pytest.mark.parametrize("path", PAGES)
def test_weather_switched_off_has_no_weather_chip(client, seeded, path):
    _switch_off(seeded, "stations")

    page = client.get(path).text

    assert _layers(page) == _expected_layers(path, seeded, ADVICE)
    assert set(_chips(page)) == {"advice"}
    assert "/stations/layer.geojson" not in page


# --- map.js structure (the browser check covers the behaviour) -------------------------------


def _js(client) -> str:
    return client.get("/static/fields/map.js").text


def test_map_script_keeps_one_parcels_colouring_active(client):
    js = _js(client)

    assert "[data-layer-chip]" in js
    assert 'kind === "parcels"' in js
    body = js.split("function colourBy(", 1)[1].split("\n    function ", 1)[0]
    assert 'setAttribute("aria-pressed"' in body  # every parcels chip re-marked: one pressed


def test_map_script_toggles_point_layers_on_their_own(client):
    js = _js(client)

    assert 'kind === "points"' in js
    body = js.split("function togglePoints(", 1)[1].split("\n    function ", 1)[0]
    assert 'setAttribute("aria-pressed"' in body
    assert "colourBy" not in body  # a point toggle never changes the colouring


def test_map_script_fetches_each_layer_and_reports_a_failure_on_that_layer_only(client):
    js = _js(client)

    assert "function fetchLayer(" in js
    assert "function layerFailed(" in js
    failed = js.split("function layerFailed(", 1)[1].split("\n    function ", 1)[0]
    assert "say(" not in failed  # not the map-wide status line
    assert "data-chip-note" in failed


def test_neutral_parcels_take_no_decision_colour_from_the_base_geojson(client):
    js = _js(client)
    add = js.split("function addParcels(", 1)[1].split("\n    function ", 1)[0]

    assert '["get", "decision"]' not in add  # the colour comes from the active layer only
    assert '["get", "fc_hatch"]' in add
    assert '["get", "fc_colour"]' in js


def test_chip_row_never_wraps(client):
    css = client.get("/static/core/farmcore.css").text

    rule = re.search(r"\.fc-map-chips\s*\{([^}]*)\}", css)
    assert rule, "no .fc-map-chips rule"
    assert "flex-wrap: nowrap" in rule.group(1) and "overflow-x: auto" in rule.group(1)


@pytest.mark.parametrize("path", PAGES)
def test_chip_row_is_a_labelled_group_not_a_toolbar(client, seeded, path):
    # No arrow-key roving focus, so not announced as an ARIA toolbar.
    page = client.get(path).text

    row = re.search(r"<div[^>]*data-map-chips[^>]*>", page).group(0)
    assert 'role="group"' in row and 'aria-label="Map layers"' in row
    assert 'role="toolbar"' not in page


def _fn(js: str, name: str) -> str:
    return js.split(f"function {name}(", 1)[1].split("\n    function ", 1)[0]


def test_popup_says_protected_until_only_for_a_protected_decision(client):
    body = _fn(_js(client), "parcelPopupFacts")

    assert 'info.decision === "protected" && info.protected_until' in body


def test_station_popup_never_calls_a_stale_reading_now(client):
    body = _fn(_js(client), "pointPopup")

    assert '"Last reading "' in body and "p.stale" in body
    assert "p.as_of_text" in body  # the day as well as the time ("Sun 06:00")


def test_station_popup_words_partial_rain_like_the_today_card(client):
    body = _fn(_js(client), "pointPopup")

    assert '"at least "' in body
    assert '"/48 h of rain readings"' in body
    assert "of 48 h known" not in body


def test_failed_point_layer_retries_on_one_tap_and_ignores_a_late_failure(client):
    js = _js(client)
    wire = _fn(js, "wireChips")
    points = wire.split('def.kind === "points"', 1)[1]
    assert 'dataset.state === "error"' in points  # a pressed, failed chip: tap retries
    toggle = _fn(js, "togglePoints")
    catch = toggle.split(".catch(", 1)[1]
    assert 'getAttribute("aria-pressed") === "true"' in catch  # switched off since: no error


def test_station_marker_opens_once_by_key_or_pointer_and_never_taps_the_parcel_under_it(client):
    # QA: MapLibre's Enter/Space keypress toggle plus the button's own click toggled twice (no
    # popup); a pointer click also reached the map and opened the parcel under the station.
    body = _fn(_js(client), "pointMarker")

    keypress = body.split('addEventListener("keypress"', 1)[1].split("}, true);", 1)[0]
    assert "stopImmediatePropagation()" in keypress  # capture phase, on the button itself
    assert "preventDefault" not in keypress  # the button still fires its native click
    click = body.split('addEventListener("click"', 1)[1].split("});", 1)[0]
    assert "stopPropagation()" in click and "marker.togglePopup()" in click


def test_station_marker_has_a_44px_hit_area(client):
    css = client.get("/static/core/farmcore.css").text

    rule = re.search(r"\.fc-map-point::before\s*\{([^}]*)\}", css)
    assert rule, "no hit-area rule on .fc-map-point"
    assert "min-width: 44px" in rule.group(1) and "min-height: 44px" in rule.group(1)


@pytest.mark.parametrize("advisor_on", [True, False])
def test_the_phone_parcel_list_cannot_grow_wider_than_the_screen(
    client, session, seeded, advisor_on
):
    # QA F3: an implicit auto grid track sizes to the longest name (420 px at 375 px wide). One
    # explicit shrinking column (grid-cols-1 = minmax(0, 1fr)) and min-w-0 items let the name
    # truncate instead.
    if not advisor_on:
        _switch_off(session, "advisor")
    page = client.get("/fields/").text
    [grid] = re.findall(
        r'<ul class="([^"]*)"[^>]*>\s*(?:\{%[^%]*%\}\s*)*<li[^>]*data-parcel=', page
    )
    assert "grid-cols-1" in grid.split()
    items = re.findall(r'<li class="([^"]*)" data-parcel=', page)
    assert items and all("min-w-0" in c.split() for c in items)


@pytest.mark.parametrize("advisor_on", [True, False])
def test_the_map_introduction_says_a_tap_opens_the_parcel_facts(client, seeded, advisor_on):
    # A tap opens the parcel panel in both line-ups, not a card or a name popup.
    if not advisor_on:
        _switch_off(seeded, "advisor")

    page = client.get("/fields/").text

    assert "Tap a parcel to see its facts." in page
    assert "Tap a parcel for its card." not in page and "Tap a parcel for its name." not in page

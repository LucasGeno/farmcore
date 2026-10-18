"""`GET /fields/`: the satellite basemap hooks, the decision colours and the icon + word legend
(colour is never the only signal)."""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import UTC, datetime

import pytest

from farmcore.apps.fields.seed import seed
from farmcore.apps.fields.service import DECISION_STYLES
from farmcore.core import ext_ids
from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Log

# 10 Apr 2024 06:00 Europe/Paris (CEST, UTC+2).
NOW = datetime(2024, 4, 10, 4, 0, tzinfo=UTC)

# The shell tokens: spray #b91c1c, wait #a45a00,
# protected #15803d, none the muted ink. The icons are the core ones named after the decision.
TOKENS = {
    "spray": ("Spray", "#b91c1c"),
    "wait": ("Wait", "#a45a00"),
    "protected": ("Protected", "#15803d"),
    "none": ("No action", "#4b5563"),
}
ICONS = {"spray": "spray", "wait": "wait", "protected": "protected", "none": "none"}

IGN_WMTS = (
    "https://data.geopf.fr/wmts?SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0"
    "&LAYER=ORTHOIMAGERY.ORTHOPHOTOS&STYLE=normal&TILEMATRIXSET=PM"
    "&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&FORMAT=image/jpeg"
)
OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


@pytest.fixture
def seeded(session, synthetic_data_dir, settings):
    seed(session, scene=None, data_dir=synthetic_data_dir)
    set_replay(session, NOW, settings)
    session.commit()
    return session


def _data_attr(page: str, name: str) -> dict:
    match = re.search(rf"{name}='([^']*)'", page)
    assert match, f"{name} not found"
    return json.loads(html_lib.unescape(match.group(1)))


def test_decision_styles_are_the_design_tokens_with_the_same_keys_and_shape():
    assert DECISION_STYLES == TOKENS


def test_legend_has_icon_and_word_per_decision_and_styles_carry_the_new_colours(client, seeded):
    page = client.get("/fields/").text

    styles = _data_attr(page, "data-styles")
    assert {d: tuple(v) for d, v in styles.items()} == TOKENS
    legend = page[
        page.index('id="map-legend"') : page.index("</ul>", page.index('id="map-legend"'))
    ]
    for decision, (label, _colour) in TOKENS.items():
        item = re.search(rf'<li[^>]*data-decision="{decision}"[^>]*>(.*?)</li>', legend, re.S)
        assert item, decision
        assert f'data-icon="{ICONS[decision]}"' in item.group(1)
        assert "<svg" in item.group(1)  # the core icon, next to the word
        assert label in item.group(1)


@pytest.mark.parametrize("path", ["/fields/", "/fields/today"])
def test_legend_sits_inside_the_map_and_labels_use_the_shell_font(client, seeded, path):
    page = client.get(path).text

    wrap = page.index('class="fc-map-wrap"')
    legend = page.index("data-map-legend")
    status = page.index("data-map-status")
    assert wrap < page.index("data-geojson-url") < legend < status  # in the wrap, over the map
    assert re.search(r"\.fc-map \.fc-legend \{[^}]*position: absolute", page)
    assert "Inter" not in page and '"DM Sans"' in page
    # The popup chip is the core decision_chip, one template per decision.
    for decision in TOKENS:
        assert f'<template data-chip="{decision}">' in page
    assert '<template data-chip="not-advised">' in page


def test_map_page_carries_both_basemaps_the_ign_attribution_and_the_toggle(client, seeded):
    page = client.get("/fields/").text

    basemaps = _data_attr(page, "data-basemaps")
    assert basemaps["satellite"]["tiles"] == [IGN_WMTS]
    assert "© IGN" in basemaps["satellite"]["attribution"]
    assert basemaps["plan"]["tiles"] == [OSM_TILES]
    assert "OpenStreetMap" in basemaps["plan"]["attribution"]
    toggle = re.search(r"<button[^>]*data-basemap-toggle[^>]*>(.*?)</button>", page, re.S)
    assert toggle, "toggle button"
    assert "Satellite" in toggle.group(1) and "Plan" in toggle.group(1)
    assert 'id="parcel-map"' in page and "data-parcel-map" in page


def test_parcel_list_names_each_card_link_and_shows_the_decision_icon(client, seeded):
    page = client.get("/fields/").text

    assert 'aria-label="Open parcel P2 Synthetic Two"' in page
    item = re.search(r'<li[^>]*data-parcel="P2"[^>]*>(.*?)</li>', page, re.S)
    assert item and 'data-icon="none"' in item.group(1) and "No advice yet" in item.group(1)
    assert 'data-decision="none"' in item.group(1)  # the core decision_chip


def test_map_script_dispatches_parcel_tap_and_respects_reduced_motion(client):
    js = client.get("/static/fields/map.js").text

    assert 'new CustomEvent("farmcore:parcel-tap"' in js
    assert "prefers-reduced-motion" in js
    assert "[data-parcel-map]" in js


def test_map_labels_carry_the_alias_only(client):
    js = client.get("/static/fields/map.js").text

    assert "label.textContent = f.properties.alias;" in js
    assert "f.properties.name" not in js.split("function addParcels", 1)[1].split("fit();", 1)[0]


def test_map_declutter_moves_an_overlapping_label_before_it_hides_it(client):
    # QA: at the default fit on a phone, P10's label was hidden, so one parcel had no alias.
    js = client.get("/static/fields/map.js").text
    declutter = js.split("function declutter()", 1)[1].split("function select(", 1)[0]

    assert "LABEL_NUDGES" in declutter  # offsets tried in turn
    assert "style.translate" in declutter  # MapLibre owns the marker's `transform`
    assert declutter.index("LABEL_NUDGES") < declutter.index('"hidden"')


def test_parcel_list_shows_the_last_spray_in_the_app_date_format(client, seeded):
    parcel = ext_ids.lookup(seeded, "ign-rpg-2024", "900002")
    log = Log(
        farm_id=parcel.farm_id,
        type="spray",
        status="done",
        name="spray",
        ts_start=datetime(2024, 4, 9, 6, 0, tzinfo=UTC),
    )
    log.parcels.append(parcel)
    seeded.add(log)
    seeded.commit()

    page = client.get("/fields/").text

    assert "Last spray 9 Apr 2024" in page
    assert "2024-04-09" not in page


@pytest.mark.parametrize("path", ["/fields/", "/fields/today"])
def test_basemap_toggle_keeps_its_visible_words_in_its_name_and_has_one_state_signal(
    client, seeded, path
):
    page = client.get(path).text

    tag = re.search(r"<button[^>]*data-basemap-toggle[^>]*>", page, re.S).group(0)
    assert "aria-pressed" not in tag  # the label states the layer; no second state signal
    label = re.search(r'aria-label="([^"]*)"', tag)
    assert label is None or label.group(1).startswith("Satellite / Plan")  # WCAG 2.5.3
    # map.js shows it once mounted with the stored choice, so no stale "Satellite" state flashes
    # (and no dead button when the map library fails).
    assert re.search(r"\shidden[\s>]", tag)


@pytest.mark.parametrize("path", ["/fields/", "/fields/today"])
def test_map_library_script_records_whether_it_loaded_or_failed(client, seeded, path):
    page = client.get(path).text

    tag = re.search(r"<script[^>]*/static/core/vendor/maplibre-gl-4\.7\.1\.js[^>]*>", page).group(0)
    assert "onload=" in tag and "onerror=" in tag and "data-maplibre" in tag


def test_map_script_waits_only_for_a_library_still_loading(client):
    js = client.get("/static/fields/map.js").text

    assert "dataset.maplibre" in js  # start() reads the flag the script tag sets


@pytest.mark.parametrize("path", ["/fields/", "/fields/today"])
def test_map_zoom_buttons_meet_the_44px_touch_target(client, seeded, path):
    # MapLibre's own zoom buttons are 29px; the fragment's styles lift them.
    page = client.get(path).text

    rule = re.search(r"\.fc-map \.maplibregl-ctrl-group button\s*\{([^}]*)\}", page)
    assert rule, "no touch-target rule for the map controls"
    assert re.search(r"width:\s*44px", rule.group(1))
    assert re.search(r"height:\s*44px", rule.group(1))

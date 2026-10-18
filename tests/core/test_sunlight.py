"""N6 Sunlight mode: a high-contrast cookie preference beside Expert. Pure backgrounds, larger
text, thicker borders and stronger status colours, all AAA (>= 7:1) for status text."""

from __future__ import annotations

import re
from pathlib import Path

from farmcore.apps.fields.service import DECISION_STYLES, HIGH_CONTRAST_STYLES

CSS = (Path(__file__).resolve().parents[2] / "src/farmcore/core/static/farmcore.css").read_text()


def _lum(hex_: str) -> float:
    rgb = [int(hex_[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _high_vars() -> dict[str, str]:
    block = CSS.split('html[data-contrast="high"] {', 1)[1].split("}", 1)[0]
    out = {}
    for name, r, g, b in re.findall(r"--([a-z0-9-]+)-rgb: (\d+) (\d+) (\d+)", block):
        out[name] = f"#{int(r):02x}{int(g):02x}{int(b):02x}"
    return out


def test_pref_sets_and_clears_the_cookie_and_stays_on_site(client):
    on = client.get("/prefs/sunlight?on=1&next=/plan", follow_redirects=False)
    assert on.status_code == 303 and on.headers["location"] == "/plan"
    assert (
        "sunlight=1" in on.headers["set-cookie"]
        and "samesite=lax" in on.headers["set-cookie"].lower()
    )
    evil = client.get("/prefs/sunlight?on=1&next=https://evil.example", follow_redirects=False)
    assert evil.headers["location"] == "/"
    off = client.get("/prefs/sunlight?on=0", follow_redirects=False)
    assert 'sunlight=""' in off.headers["set-cookie"] or "Max-Age=0" in off.headers["set-cookie"]


def test_html_carries_the_contrast_flag_only_with_the_cookie(client):
    assert 'data-contrast="high"' not in client.get("/today/").text
    client.cookies.set("sunlight", "1")
    assert '<html lang="en" data-contrast="high"' in client.get("/today/").text


def test_toggle_sits_next_to_expert_in_more_and_sidebar(client):
    page = client.get("/today/").text
    sheet = page.split('id="more-menu"', 1)[1]
    assert 0 < sheet.index("Sunlight mode") - sheet.index(">Expert</button>") < 1500
    side = page.split('id="navbar"', 1)[1]
    assert "Sunlight mode" in side and 'action="/prefs/sunlight"' in page


def test_high_contrast_theme_is_pure_and_strong():
    v = _high_vars()
    assert v["ground"] == "#ffffff" and v["surface"] == "#ffffff" and v["ink"] == "#000000"
    assert _ratio(v["muted"], "#ffffff") >= 7 and v["line"] == "#000000"
    for name in ("spray", "wait", "protected"):
        assert _ratio(v[name], v[f"{name}-soft"]) >= 7, name
        assert _ratio(v[name], "#ffffff") >= 7, name
    assert "font-size: 112.5%" in CSS.split('html[data-contrast="high"] {', 1)[1]
    assert re.search(r'\[data-contrast="high"\] \.border \{[^}]*border-width: 2px', CSS)


def test_map_colours_have_a_stronger_set_with_the_same_hues():
    assert set(HIGH_CONTRAST_STYLES) == set(DECISION_STYLES)
    for key, (label, colour) in HIGH_CONTRAST_STYLES.items():
        assert label == DECISION_STYLES[key][0] and colour != DECISION_STYLES[key][1]
        assert _ratio(colour, "#ffffff") >= 7, key


def test_layer_geojson_uses_the_strong_colours_in_sunlight(client):
    from farmcore.apps.fields.service import styles_for

    class Req:
        cookies = {"sunlight": "1"}

    assert styles_for(Req()) is HIGH_CONTRAST_STYLES
    Req.cookies = {}
    assert styles_for(Req()) is DECISION_STYLES


def test_file_inputs_cannot_outgrow_the_page():
    """Operations' ISO-XML file picker is as wide as its label at Sunlight's larger text (406 px
    at 375): any file input is capped at its container."""
    assert 'input[type="file"] { max-width: 100%; min-width: 0; }' in CSS


def test_larger_text_keeps_the_forecast_strip_and_selects_intact():
    strip = '[data-contrast="high"] [data-weather-strip] li'
    assert f"{strip} {{ min-width: 5.25rem; white-space: nowrap; }}" in CSS
    assert '[data-contrast="high"] select { max-width: 100%' in CSS


def test_nothing_in_the_forecast_strip_can_escape_its_scroller():
    """Absolutely positioned sr-only text needs the scroller as its containing block, or it
    stretches the whole page sideways once Sunlight's larger text widens the strip (375 px)."""
    src = Path(__file__).resolve().parents[2] / "src/farmcore/apps/forecast/templates/forecast"
    ol = (src / "_home_weather.html").read_text().split("<ol data-weather-strip", 1)[1]
    assert (
        'class="relative flex' in ol.split(">", 1)[0] and "overflow-x-auto" in ol.split(">", 1)[0]
    )
    assert "#notifications-feed:not([open]) > section { display: none; }" in CSS

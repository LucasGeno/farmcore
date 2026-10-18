"""Viewer controls choose density and contrast independently, without authority."""

import re
from html import unescape

import pytest
from fastapi.testclient import TestClient

from tests.core.test_nav import _app


@pytest.mark.parametrize(
    "expert,sunlight", [(False, False), (False, True), (True, False), (True, True)]
)
def test_field_expert_and_sunlight_are_native_independent_controls(client, expert, sunlight):
    if expert:
        client.cookies.set("expert", "1")
    if sunlight:
        client.cookies.set("sunlight", "1")
    path = "/apps?farm=2&return_to=parcel&parcel_id=7"
    page = client.get(path).text
    assert f'data-view="{"expert" if expert else "field"}"' in page
    assert ('data-contrast="high"' in page) is sunlight
    for section in (
        page.split("<aside", 1)[1].split("</aside>", 1)[0],
        page.split('id="more-menu"', 1)[1].split("</dialog>", 1)[0],
    ):
        forms = re.findall(r'<form\b[^>]*action="/prefs/[^>]+>.*?</form>', section, re.S)
        assert len(forms) == 2
        density, contrast = forms
        assert 'method="get"' in density and 'action="/prefs/expert"' in density
        assert "<legend" in density and ">View</legend>" in density
        for value, label, selected in [("0", "Field", not expert), ("1", "Expert", expert)]:
            assert re.search(
                rf'<button[^>]*name="on"[^>]*value="{value}"[^>]*aria-pressed="{str(selected).lower()}"[^>]*>{label}</button>',
                density,
            )
        assert 'action="/prefs/sunlight"' in contrast
        assert f'aria-pressed="{str(sunlight).lower()}"' in contrast
        assert "Sunlight mode" in contrast
        for form in forms:
            assert unescape(re.search(r'name="next" value="([^"]+)"', form)[1]) == path
        assert not re.search(r"<a[^>]*aria-pressed", section)


def test_changing_view_and_sunlight_preserves_context_and_authority(settings, session_factory):
    with TestClient(_app(settings, session_factory)) as browser:
        target = "/apps?farm=2&return_to=parcel&parcel_id=7"
        for pref, on in [("sunlight", 1), ("expert", 1), ("expert", 0), ("sunlight", 0)]:
            response = browser.get(
                f"/prefs/{pref}", params={"on": on, "next": target}, follow_redirects=False
            )
            assert response.status_code == 303 and response.headers["location"] == target
            page = browser.get(target).text
            assert ('data-view="expert"' in page) is (pref == "expert" and on == 1)
            assert ('data-contrast="high"' in page) is not (pref == "sunlight" and on == 0)
            assert 'href="/demo/"' not in page
            assert browser.post("/clock/step", data={"to": "2024-04-02T06:00"}).status_code == 403
        for pref in ("expert", "sunlight"):
            response = browser.get(
                f"/prefs/{pref}", params={"next": "//evil.example"}, follow_redirects=False
            )
            assert response.headers["location"] == "/"


def test_header_clock_retains_date_time_replay_and_bell_with_compact_wrapping(client):
    client.cookies.set("sunlight", "1")
    page = client.get("/apps").text
    header = page.split("<header", 1)[1].split("</header>", 1)[0]
    assert 'class="fc-header-row ' in header
    assert 'class="fc-header-clock ' in header
    assert 'data-shell-slot="notifications"' in header
    clock = client.get("/clock").text
    assert 'class="fc-clock-summary ' in clock
    assert 'class="fc-clock-time ' in clock
    assert "data-clock-date" in clock and "01 Apr" in clock
    assert "data-clock-hour" in clock and "06:00" in clock
    assert ">Replay</span>" in clock

"""Cold return positions through rendered HTTP links; browser motion/focus is separate."""

import html
import re
from urllib.parse import parse_qs, urlsplit

from farmcore.core.clock import get_clock
from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.advisor.test_workspace import stored as stored
from tests.apps.fields.test_workspace import app_client as app_client
from tests.apps.fields.test_workspace import farms as farms


def test_list_workspace_return_names_focusable_source_row(app_client, farms):
    farm, _, parcel, _, _ = farms
    page = app_client.get("/fields/", params={"view": "list", "q": "Merlot"})
    urls = [html.unescape(u) for u in re.findall(r'href="([^"]+)"', page.text)]
    detail = next(u for u in urls if urlsplit(u).path == f"/fields/parcels/{parcel.id}")
    origin = parse_qs(urlsplit(detail).query)["return_to"][0]
    assert urlsplit(origin).fragment == f"parcel-{parcel.id}"
    assert parse_qs(urlsplit(origin).query) == {
        "view": ["list"],
        "q": ["Merlot"],
        "farm": [str(farm.id)],
    }
    row = re.search(rf'<li\b[^>]*id="parcel-{parcel.id}"[^>]*>', page.text)[0]
    assert 'tabindex="-1"' in row
    assert "focus:outline" in re.search(r'class="([^"]+)"', row)[1].split()
    assert "data-return-heading" in page.text


def test_direct_workspace_and_legacy_map_returns_name_existing_panel_heading(
    app_client, client, farms, session, settings
):
    farm, _, parcel, _, _ = farms
    complete_station(session, farm.id, get_clock(session, settings).now())
    session.commit()
    body = app_client.get(f"/fields/parcels/{parcel.id}").text
    urls = [html.unescape(u) for u in re.findall(r'href="([^"]+)"', body)]
    assert f"/fields/?parcel={parcel.id}&farm={farm.id}#map-panel-heading" in urls
    fragment = client.get(f"/advisor/map/parcels/{parcel.id}", params={"farm": farm.id}).text
    detail = next(
        html.unescape(u)
        for u in re.findall(r'href="([^"]+)"', fragment)
        if u.startswith("/advisor/parcels/")
    )
    origin = parse_qs(urlsplit(detail).query)["return_to"][0]
    assert urlsplit(origin).fragment == "map-panel-heading"
    returned = app_client.get(origin)
    heading = re.search(r'<h2[^>]*id="map-panel-heading"[^>]*tabindex="-1"[^>]*>', returned.text)[0]
    assert "scroll-mt-20" in re.search(r'class="([^\"]+)"', heading)[1].split()


def test_source_pages_expose_labelled_fallback_heading_and_single_return_script(
    client, farms, session, settings
):
    farm, _, parcel, _, _ = farms
    complete_station(session, farm.id, get_clock(session, settings).now())
    session.commit()
    for path, heading in (
        ("/today/", "home-heading"),
        ("/advisor/", "advisor-heading"),
        ("/forecast/plan", "plan-heading"),
        ("/activity/", "activity-heading"),
        (f"/fields/parcels/{parcel.id}", "parcel-heading"),
    ):
        page = client.get(path, params={"farm": farm.id})
        assert page.status_code == 200
        tag = re.search(rf'<h1\b[^>]*id="{heading}"[^>]*>', page.text)
        assert tag and "data-return-heading" in tag[0] and 'tabindex="-1"' in tag[0], path
        assert "focus:outline" in re.search(r'class="([^"]+)"', tag[0])[1].split()
        assert page.text.count('src="/static/core/navigation.js"') == 1
    assert client.get("/static/core/navigation.js").status_code == 200


def test_home_and_advisor_source_row_anchors_are_native_focus_targets(client, stored):
    farm, _, parcel, _ = stored
    for path, prefix in (
        ("/advisor/today?view=attention", "home"),
        ("/advisor/today", "home"),
        ("/advisor/?model=all", "advisor"),
        ("/advisor/?model=powdery", "advisor"),
    ):
        page = client.get(path + f"&farm={farm.id}" if "?" in path else path + f"?farm={farm.id}")
        target = re.search(rf'<(?:li|div)\b[^>]*id="{prefix}-parcel-{parcel.id}"[^>]*>', page.text)
        assert target and 'tabindex="-1"' in target[0], path


def test_compact_map_card_contains_long_variety_and_workspace_retains_full_value(
    app_client, farms, session
):
    farm, _, parcel, _, _ = farms
    variety = "UnbrokenImportedVariety" * 40
    parcel.props = {**parcel.props, "variety": variety}
    session.commit()
    page = app_client.get("/fields/", params={"farm": farm.id})
    card = re.search(rf'<li\b[^>]*data-parcel="{parcel.alias}"[^>]*>.*?</li>', page.text, re.S)[0]
    text = re.search(r"<span\b[^>]*>[^<]*" + variety + r"[^<]*</span>", card)[0]
    assert "truncate" in re.search(r'class="([^\"]+)"', text)[1].split()
    workspace = app_client.get(f"/fields/parcels/{parcel.id}", params={"farm": farm.id})
    assert variety in workspace.text

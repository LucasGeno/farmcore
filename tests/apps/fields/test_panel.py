"""The Map's Farm today / parcel panel shell (fields owns selection and the shell; apps own
their facts). Fields composes one shared fragment slot per section URL that a visible app
declares (`farm_sections`, `parcel_sections`), in manifest load order, never importing app code.

HTTP/template level. The panel controller (`static/panel.js`: tap, history, focus, Escape) and
the slot lifecycle run in the browser and are not exercised by these tests."""

from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from farmcore.apps.fields.manifest import MANIFEST as FIELDS
from farmcore.core.access import FARM_COOKIE, ensure_owner_grant
from farmcore.core.manifest import Manifest
from farmcore.core.models.canonical import Farm
from farmcore.core.models.kernel import AppModule
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app
from tests.apps.operations.conftest import make_parcel

STATIC = Path(__file__).resolve().parents[3] / "src" / "farmcore" / "apps" / "fields" / "static"

# Fake section providers (no routes: the shell never calls them; B's real endpoints are tested
# in their own apps).
ALPHA = Manifest(
    name="alpha",
    version="0.1.0",
    title="Crop protection",
    farm_sections=("/alpha/map/farm",),
    parcel_sections=("/alpha/map/parcels/{id}",),
)
BETA = Manifest(
    name="beta",
    version="0.1.0",
    title="Records",
    # A farm section with "{id}" is a parcel URL declared in the wrong place: skipped, never
    # fetched with a literal "{id}".
    farm_sections=("/beta/map/farm", "/beta/map/{id}/misdeclared"),
    parcel_sections=("/beta/map/parcels/{id}", "/beta/map/parcels/{id}/copper"),
)
QUIET = Manifest(name="quiet", version="0.1.0")  # declares no section


@pytest.fixture
def app_client(settings, session_factory):
    ms = [FIELDS, ALPHA, BETA, QUIET]
    with TestClient(create_app(settings, manifests=ms, session_factory=session_factory)) as c:
        yield c


@pytest.fixture
def farms(app_client, session, settings):
    first = ensure_default_farm(session, settings)
    second = Farm(name="Panel QA B")
    session.add(second)
    session.flush()
    ensure_owner_grant(session, second)
    mine = make_parcel(session, first, 101, area="1.5", props={"variety": "Merlot"})
    other = make_parcel(session, second, 202, area="2", props={})
    session.commit()
    app_client.cookies.set(FARM_COOKIE, str(first.id), domain="testserver.local")
    return first, second, mine, other


def slots(markup: str) -> list[dict]:
    """Each slot wrapper: id, label, and the GET URLs of its body and Retry."""
    out = []
    for match in re.finditer(
        r'<div id="([^"]+)" data-fragment data-fragment-label="([^"]*)"', markup
    ):
        start = match.start()
        end = markup.index("</button>", start)
        chunk = markup[start:end]
        out.append(
            {
                "id": match[1],
                "label": html.unescape(match[2]),
                "urls": [html.unescape(u) for u in re.findall(r'hx-get="([^"]+)"', chunk)],
                "target": re.search(r'hx-target="(#[^"]+)"', chunk)[1],
            }
        )
    return out


# --- Parcel shell ----------------------------------------------------------------------------


def test_parcel_panel_has_facts_close_heading_and_one_slot_per_section(app_client, farms):
    first, _, mine, _ = farms
    resp = app_client.get(f"/fields/panel/parcels/{mine.id}")

    assert resp.status_code == 200
    body = resp.text
    assert "P101" in body and "Test parcel 101" in body
    assert "1.50 ha" in body and "Merlot" in body
    heading = re.search(r"<h2\b[^>]*data-panel-heading[^>]*>", body)[0]
    assert 'tabindex="-1"' in heading
    close = re.search(r"<a\b[^>]*data-panel-close[^>]*>", body)[0]
    assert 'aria-label="Close P101 panel"' in close
    assert "min-h-[44px]" in close and "min-w-[44px]" in close
    assert html.unescape(re.search(r'href="([^"]+)"', close)[1]) == f"/fields/?farm={first.id}"

    found = slots(body)
    assert [s["label"] for s in found] == ["Crop protection", "Records", "Records"]
    pid = mine.id
    assert [s["urls"] for s in found] == [
        [f"/alpha/map/parcels/{pid}?farm={first.id}"] * 2,
        [f"/beta/map/parcels/{pid}?farm={first.id}"] * 2,
        [f"/beta/map/parcels/{pid}/copper?farm={first.id}"] * 2,
    ]
    ids = [s["id"] for s in found]
    assert len(set(ids)) == 3
    assert [i.rsplit("-", 1)[0] for i in ids] == [
        "sec-alpha-parcel-1",
        "sec-beta-parcel-1",
        "sec-beta-parcel-2",
    ]
    assert all(re.fullmatch(r"[a-z0-9-]+", i) for i in ids)
    assert all(s["target"] == f"#{s['id']}-body" for s in found)
    assert "{id}" not in body
    assert "/alpha/map/farm" not in body  # farm sections stay in Farm today


def test_each_mount_gets_fresh_slot_ids(app_client, farms):
    _, _, mine, _ = farms
    first = [s["id"] for s in slots(app_client.get(f"/fields/panel/parcels/{mine.id}").text)]
    second = [s["id"] for s in slots(app_client.get(f"/fields/panel/parcels/{mine.id}").text)]
    assert not set(first) & set(second)


def test_alien_parcel_is_404_and_shows_nothing_of_it(app_client, farms):
    _, second, _, other = farms
    resp = app_client.get(f"/fields/panel/parcels/{other.id}")
    assert resp.status_code == 404
    assert "P202" not in resp.text and "/alpha/map/parcels" not in resp.text
    assert app_client.get("/fields/panel/parcels/999999").status_code == 404
    # The same parcel under its own farm (query wins over the cookie farm).
    assert app_client.get(f"/fields/panel/parcels/{other.id}?farm={second.id}").status_code == 200


@pytest.mark.parametrize("bad", ["0", "99999999999999999999"])
def test_out_of_range_parcel_ids_never_reach_the_database(app_client, farms, bad):
    assert app_client.get(f"/fields/panel/parcels/{bad}").status_code == 404


# --- Farm today shell ------------------------------------------------------------------------


def test_farm_panel_is_farm_today_with_the_farm_sections(app_client, farms):
    first, _, _, _ = farms
    resp = app_client.get("/fields/panel")
    assert resp.status_code == 200
    assert "Farm today" in resp.text
    found = slots(resp.text)
    assert [s["urls"][0] for s in found] == [
        f"/alpha/map/farm?farm={first.id}",
        f"/beta/map/farm?farm={first.id}",
    ]
    assert [s["id"].rsplit("-", 1)[0] for s in found] == ["sec-alpha-farm-1", "sec-beta-farm-1"]
    assert "data-panel-close" not in resp.text
    assert "{id}" not in resp.text and "misdeclared" not in resp.text


# --- Farm context: ?farm= (B) wins over the cookie (A) ----------------------------------------


def test_query_farm_survives_panel_slot_retry_and_show_on_map_urls(app_client, farms):
    _, second, _, other = farms
    q = f"farm={second.id}"

    panel = app_client.get(f"/fields/panel/parcels/{other.id}", params={"farm": second.id}).text
    for slot in slots(panel):
        assert all(u.endswith(q) for u in slot["urls"]), slot
    close = re.search(r"<a\b[^>]*data-panel-close[^>]*>", panel)[0]
    assert html.unescape(re.search(r'href="([^"]+)"', close)[1]) == f"/fields/?{q}"

    farm_panel = app_client.get("/fields/panel", params={"farm": second.id}).text
    assert [s["urls"][0] for s in slots(farm_panel)] == [
        f"/alpha/map/farm?{q}",
        f"/beta/map/farm?{q}",
    ]

    page = html.unescape(
        app_client.get("/fields/", params={"farm": second.id, "parcel": other.id}).text
    )
    assert 'data-parcel="P202"' in page and 'data-parcel="P101"' not in page
    assert f'href="/fields/?parcel={other.id}&{q}"' in page  # Show on map
    assert f'data-panel-parcel-url="/fields/panel/parcels/{{id}}?{q}"' in page
    assert f'data-panel-farm-url="/fields/panel?{q}"' in page
    assert f"/alpha/map/parcels/{other.id}?{q}" in page


def test_cookie_only_map_freezes_its_resolved_farm_in_read_urls(app_client, farms):
    first, _, mine, _ = farms
    page = html.unescape(app_client.get("/fields/").text)
    assert f'href="/fields/?parcel={mine.id}&farm={first.id}"' in page
    assert f'data-panel-farm-url="/fields/panel?farm={first.id}"' in page
    assert f"/alpha/map/farm?farm={first.id}" in page


# --- Server-rendered panel (no JS) -----------------------------------------------------------


def test_map_page_opens_farm_today_by_default(app_client, farms):
    page = app_client.get("/fields/").text
    panel = page.split("data-map-panel", 1)[1]
    assert "Farm today" in panel
    assert "/alpha/map/farm" in panel and "/alpha/map/parcels/" not in panel


def test_map_page_with_parcel_renders_the_parcel_panel_open(app_client, farms):
    _, _, mine, _ = farms
    page = app_client.get("/fields/", params={"parcel": mine.id}).text
    panel = page.split("data-map-panel", 1)[1]
    assert "Test parcel 101" in panel
    assert f"/alpha/map/parcels/{mine.id}" in panel
    assert "data-panel-close" in panel
    assert re.search(rf'data-map-panel[^>]*data-parcel-id="{mine.id}"', page)


def test_map_page_with_an_alien_parcel_shows_farm_today_and_nothing_of_it(app_client, farms):
    _, _, _, other = farms
    resp = app_client.get("/fields/", params={"parcel": other.id})
    assert resp.status_code == 200
    assert "P202" not in resp.text and "Test parcel 202" not in resp.text
    assert "Farm today" in resp.text
    assert "That parcel is not on this farm." in resp.text


@pytest.mark.parametrize(
    "bad", ["abc", "1.5", "", "-3", "99999999999999999999", "\u00b2", "\u0663", "1 2"]
)
def test_map_page_with_a_malformed_parcel_shows_farm_today_not_an_error(app_client, farms, bad):
    """A hand-edited or truncated shared link still opens the map (no 422)."""
    resp = app_client.get("/fields/", params={"parcel": bad})
    assert resp.status_code == 200
    assert "Farm today" in resp.text


def test_map_page_trims_whitespace_around_a_real_parcel_id(app_client, farms):
    """A copied link with stray spaces still opens that parcel."""
    _, _, mine, _ = farms
    page = app_client.get("/fields/", params={"parcel": f" {mine.id} "}).text
    assert re.search(rf'data-map-panel[^>]*data-parcel-id="{mine.id}"', page)
    assert "Test parcel 101" in page.split("data-map-panel", 1)[1]
    assert "That parcel is not on this farm." not in page


def test_list_rows_offer_show_on_map_and_open_the_workspace(client, farms):
    """Real apps: the workspace opens; a secondary link selects the parcel on the map."""
    first, _, mine, _ = farms
    page = html.unescape(client.get("/fields/").text)
    row = page.split('data-parcel="P101"', 1)[1].split("</li>", 1)[0]
    detail = urlsplit(re.search(r'href="([^"]+)"', row)[1])
    assert detail.path == f"/fields/parcels/{mine.id}"
    query = parse_qs(detail.query)
    assert query["farm"] == [str(first.id)]
    [origin] = query["return_to"]
    assert urlsplit(origin).path == "/fields/"
    assert parse_qs(urlsplit(origin).query) == {"parcel": [str(mine.id)], "farm": [str(first.id)]}
    show = re.search(r"<a\b[^>]*data-show-on-map[^>]*>", row)[0]
    assert f'href="/fields/?parcel={mine.id}&farm={first.id}"' in show
    assert f'data-parcel-id="{mine.id}"' in show
    assert "min-h-[44px]" in show


# --- Visible apps only, live ------------------------------------------------------------------


def test_a_hidden_app_contributes_no_section_and_returns_without_restart(
    app_client, farms, session
):
    _, _, mine, _ = farms
    session.get(AppModule, "beta").state = "uninstalled"
    session.commit()
    off = app_client.get(f"/fields/panel/parcels/{mine.id}").text
    assert [s["label"] for s in slots(off)] == ["Crop protection"]
    assert "/beta/" not in off
    assert "/beta/" not in app_client.get("/fields/panel").text

    session.get(AppModule, "beta").state = "installed"
    session.commit()
    on = app_client.get(f"/fields/panel/parcels/{mine.id}").text
    assert [s["label"] for s in slots(on)] == ["Crop protection", "Records", "Records"]


def test_no_sections_says_so(settings, session_factory, session):
    farm = ensure_default_farm(session, settings)
    parcel = make_parcel(session, farm, 7, area="1", props={})
    session.commit()
    with TestClient(create_app(settings, manifests=[FIELDS], session_factory=session_factory)) as c:
        body = c.get(f"/fields/panel/parcels/{parcel.id}").text
        farm_body = c.get("/fields/panel").text
    assert not slots(body) and not slots(farm_body)
    assert "No app adds facts here yet." in body and "No app adds facts here yet." in farm_body


# --- Map script seams ------------------------------------------------------------------------


def test_parcel_tap_event_is_unchanged_and_a_select_api_exists():
    js = (STATIC / "map.js").read_text()
    assert (
        '        var tap = new CustomEvent("farmcore:parcel-tap", {\n'
        "          detail: { id: p.id, alias: p.alias, "
        "decision: info ? info.decision : undefined },\n"
        "          cancelable: true,\n"
        "        });\n"
        "        if (!document.dispatchEvent(tap)) return;"
    ) in js
    assert "window.farmcoreParcelMap = { mountAll: mountAll, select: selectParcel };" in js


def test_panel_controller_takes_the_tap_and_owns_history_and_focus():
    js = (STATIC / "panel.js").read_text()
    for needle in (
        '"farmcore:parcel-tap"',
        "preventDefault()",
        "history.pushState",
        '"popstate"',
        '"Escape"',
        "farmcoreParcelMap.select",
        '"htmx:abort"',
        "htmx.process",
    ):
        assert needle in js, needle


def test_closing_a_parcel_opened_over_another_parcel_goes_to_farm_today():
    """Opened at `?parcel=X`, tap Y, then X or Escape: the panel shows Farm today (it does not
    step Back to X). Only a panel opened over Farm today closes through history.back()."""
    js = (STATIC / "panel.js").read_text()
    assert "fromFarm" in js
    back = js.index("history.back()")
    guard = js.rfind("if (", 0, back)
    assert "fromFarm" in js[guard:back], "history.back() must be guarded by state.fromFarm"


def test_desktop_selection_scrolls_once_so_the_focused_heading_clears_the_sticky_header(
    app_client, farms
):
    """At lg the panel is beside the map, not a fixed sheet: after a keyboard "Show on map" the
    focused heading must be on screen below the sticky header. One scroll only (the map region,
    with a scroll margin over the header), done when focus moves; the phone sheet is fixed, so
    there only the map is brought into view."""
    page = app_client.get("/fields/").text
    region = re.search(r"<div[^>]*data-map-region[^>]*>", page)
    assert region and "scroll-mt-20" in region[0]  # 5rem > the 4rem sticky header
    js = (STATIC / "panel.js").read_text()
    focus = js[js.index("function focusHeading") : js.index("function load(")]
    assert "revealPanel(p, heading)" in focus and "preventScroll: true" in focus
    reveal = js[js.index("function revealPanel") : js.index("function focusHeading")]
    for needle in (
        "isSheet(p)",
        "headerBottom()",
        "window.innerHeight",
        "[data-map-region]",
        "reducedMotion()",
    ):
        assert needle in reveal, needle
    show = js[js.index('a[data-show-on-map]"') :]
    show = show[: show.index("return;")]
    assert "isSheet(" in show  # the map scroll is phone-only; desktop scrolls once on focus
    assert (
        'position === "fixed"'
        in js[js.index("function isSheet") : js.index("function headerBottom")]
    )


def test_map_page_loads_the_panel_controller(app_client, farms):
    page = app_client.get("/fields/").text
    assert page.count('src="/static/fields/panel.js"') == 1


def test_panel_teardown_aborts_through_the_slot_wrapper_before_replacing():
    """hx-sync keeps the in-flight xhr on `[data-fragment]`: the wrapper is the abort owner, and
    the abort runs before the old content leaves the document (static contract)."""
    js = (STATIC / "panel.js").read_text()
    teardown = js[
        js.index("function teardown(") : js.index("\n  }\n", js.index("function teardown("))
    ]
    assert '"[data-fragment], [data-fragment-body], [data-fragment-retry]"' in teardown
    assert 'trigger(el, "htmx:abort")' in teardown
    assert js.index("teardown(p);") < js.index("p.replaceChildren")


def test_the_phone_fold_never_hides_the_desktop_panel(app_client, farms):
    """Review R1 (controller): folding is phone-only. The fold is a `data-collapsed` state on the
    panel that hides the facts only below lg (`max-lg:` utilities), never the `hidden`
    attribute, so widening to lg shows the facts and the desktop panel keeps its scroll, and
    narrowing back finds the toggle's aria-expanded matching what the phone shows.
    Static/template contract; the resize itself is not run here (no browser)."""
    body = app_client.get("/fields/panel").text
    tag = re.search(r"<div\b[^>]*data-panel-body[^>]*>", body)[0]
    classes = attr_class(tag)
    assert classes == {"max-lg:group-data-[collapsed]:hidden"}  # nothing that hides at lg
    toggle = re.search(r"<button\b[^>]*data-panel-toggle[^>]*>", body)[0]
    assert 'aria-controls="map-panel-body"' in toggle and 'aria-expanded="true"' in toggle
    assert "lg:hidden" in attr_class(toggle)

    page = app_client.get("/fields/").text
    aside = attr_class(re.search(r"<aside\b[^>]*data-map-panel[^>]*>", page)[0])
    assert "group" in aside
    collapsed = {c for c in aside if "data-[collapsed]" in c}
    assert collapsed and all(c.startswith("max-lg:") for c in collapsed), collapsed

    js = (STATIC / "panel.js").read_text()
    fold = js[
        js.index("function setCollapsed(") : js.index("\n  }\n", js.index("function setCollapsed("))
    ]
    assert ".hidden" not in fold and "data-collapsed" in fold and "aria-expanded" in fold


def attr_class(tag: str) -> set[str]:
    match = re.search(r'class="([^"]*)"', tag)
    return set(match[1].split()) if match else set()

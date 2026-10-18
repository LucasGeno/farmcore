"""`/today/` assembles the apps' `today` fragments; `/` opens it; each fragment says what to
do today in plain words."""

from __future__ import annotations

import ast
import html
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from farmcore.apps.today.manifest import MANIFEST as TODAY
from farmcore.core import jobs
from farmcore.core.access import FARM_COOKIE
from farmcore.core.manifest import Manifest, Widget
from farmcore.core.models.canonical import AccessLog, Asset, Observation, Parcel
from farmcore.main import create_app
from tests.apps.advisor import scenes
from tests.apps.advisor.scenes import confirm_spray, local

manifests, seeded, step = scenes.manifests, scenes.seeded, scenes.step

TODAY_DIR = Path(__file__).resolve().parents[3] / "src" / "farmcore" / "apps" / "today"


def _app(settings, session_factory, ms):
    return TestClient(create_app(settings, manifests=ms, session_factory=session_factory))


def test_root_opens_today_when_the_today_app_is_loaded(settings, session_factory) -> None:
    with _app(settings, session_factory, None) as c:
        resp = c.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/today/"


def test_today_emits_one_slot_per_fragment_in_order(settings, session_factory) -> None:
    fakes = [
        TODAY,
        Manifest(name="late", version="0.1.0", widgets=(Widget("/late/today", 40),)),
        Manifest(name="early", version="0.1.0", widgets=(Widget("/early/today", 10),)),
        Manifest(name="mid", version="0.1.0", widgets=(Widget("/mid/today", 20),)),
    ]
    with _app(settings, session_factory, fakes) as c:
        page = c.get("/today/").text
    order = [page.index(f'hx-get="/{n}/today"') for n in ("early", "mid", "late")]
    assert order == sorted(order)
    assert 'hx-trigger="load"' in page


def test_today_without_fragments_says_so(settings, session_factory) -> None:
    with _app(settings, session_factory, [TODAY]) as c:
        page = c.get("/today/").text
    assert "Install an app to fill Home" in page


def test_today_app_imports_no_other_app() -> None:
    for path in TODAY_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            for name in names:
                assert not name.startswith("farmcore.apps."), f"{path.name} imports {name}"


def test_real_apps_declare_their_today_fragments(settings, session_factory) -> None:
    with _app(settings, session_factory, None) as c:
        assert "/data-sources/today" not in c.get("/today/").text  # Expert off (N4)
        c.cookies.set("expert", "1")
        page = c.get("/today/").text
    urls = [
        "/advisor/today?view=attention",
        "/stations/today?compact=1",
        "/forecast/home-weather",
        "/fields/today?desktop=1",
        "/activity/today",
        "/forecast/today",
        "/advisor/today?view=calm",
        "/data-sources/today",
    ]
    positions = [page.index(f'hx-get="{u}"') for u in urls]
    assert positions == sorted(positions)
    assert 'hx-get="/fields/today"' not in page  # preview keeps its independent Retry shell
    assert "/operations/today" not in page


@pytest.mark.data
def test_advisor_today_leads_with_the_spray_count(client, session, seeded, step) -> None:
    seeded()
    step("2024-04-10 06:00")
    body = html.unescape(client.get("/advisor/today").text)
    assert 'id="today-hero"' in body and 'data-decision="spray"' in body
    assert "Spray" in body and ">10<" in body and "parcels today" in body
    assert "copper" in body
    # The window runs to the forecast edge (now + 72 h): no fake deadline.
    assert "spray window open now" in body and "until Sat 13 Apr" not in body
    farm_id = session.scalar(select(Parcel.farm_id).where(Parcel.alias == "P1"))
    parcels = {
        p.alias: p.id for p in session.scalars(select(Parcel).where(Parcel.farm_id == farm_id))
    }
    rows = re.findall(r'<li\b(?=[^>]*\bdata-parcel="([^"]+)")([^>]*)>(.*?)</li>', body, re.S)
    assert sorted(alias for alias, _, _ in rows) == sorted(parcels)
    assert len(rows) == 10
    for alias, attrs, markup in rows:
        expected_id = parcels[alias]
        assert f'id="home-parcel-{expected_id}"' in attrs
        link = re.search(r'<a\b[^>]*\bhref="([^"]+)"', markup)
        assert link is not None, f"{alias} links to its own parcel"
        destination = urlsplit(link.group(1))
        assert destination.path == f"/fields/parcels/{expected_id}"
        assert parse_qs(destination.query) == {
            "view": ["protection"],
            "farm": [str(farm_id)],
            "return_to": [f"/?farm={farm_id}#home-parcel-{expected_id}"],
        }
    assert "parcels protected" not in body


@pytest.mark.data
def test_confirmed_sprays_move_parcels_to_protected(
    client, session, session_factory, settings, manifests, seeded, step
) -> None:
    seeded()
    step("2024-04-10 06:00")
    for alias in ("P5", "P6"):
        confirm_spray(session, settings, alias, local("2024-04-10 06:00"), "copper-contact")
    assert jobs.run_now(session_factory, manifests, "advisor.evaluate", settings).ok
    body = html.unescape(client.get("/advisor/today").text)
    assert ">8<" in body
    assert "2 parcels protected" in body and "Les Granges" in body


@pytest.mark.data
def test_activity_shows_the_confirmed_voice_spray(client, session, settings, seeded, step) -> None:
    seeded()
    step("2024-04-10 06:00")
    confirm_spray(session, settings, "P5", local("2024-04-10 06:00"), "copper-contact")
    body = html.unescape(client.get("/activity/today").text)
    first = body[body.index("data-activity=") :].split("</li>")[0]
    assert "Spray confirmed on P5" in first
    assert "Voice note" in first and "copper 2 kg/ha" in first
    assert 'href="/operations/register"' in first


@pytest.mark.data
def test_weather_card_names_the_next_rain_from_the_replay_forecast(client, seeded, step) -> None:
    # Mon 8 Apr 2024 12:00 local: dry and warm; the station's next rain is 21:00 that evening
    # (1.4 + 1.2 + 0.4 + 0.6 + 0.2 + 0.2 + 0.8 + 0.2 mm in the 24 h from then).
    seeded()
    step("2024-04-08 12:00")
    body = " ".join(html.unescape(client.get("/stations/today").text).split())
    assert "21 °C" in body and "Dry" in body and "Raining" not in body
    assert "Rain from today 20:00" in body
    assert "5.0 mm in 24 h" in body
    assert "Last 48 h: 6.7 mm" in body


@pytest.mark.data
def test_weather_card_says_no_rain_in_a_dry_spell(client, seeded, step) -> None:
    # Thu 4 Apr 2024 06:00 local: no rain at the station until Sun 7 Apr 20:00 (86 h).
    seeded()
    step("2024-04-04 06:00")
    body = html.unescape(client.get("/stations/today").text)
    assert "No rain over 1 mm in the next 3 days" in body


@pytest.mark.data
def test_stations_today_reads_the_station_only(client, session, seeded, step) -> None:
    seeded()
    step("2024-04-10 06:00")
    before = client.get("/stations/today").text
    station = session.scalar(select(Asset).where(Asset.kind == "station"))
    regional = Asset(farm_id=station.farm_id, kind="regional", name="Organiser region")
    session.add(regional)
    session.flush()
    session.add(
        Observation(asset_id=regional.id, ts=local("2024-04-10 05:00"), variable="t_c", value=99.0)
    )
    session.commit()
    after = client.get("/stations/today").text
    assert after == before
    assert "99" not in after


@pytest.mark.data
def test_each_fragment_writes_the_access_log(client, session, seeded, step) -> None:
    seeded()
    step("2024-04-10 06:00")
    for url in ("/advisor/today", "/stations/today", "/activity/today"):
        before = session.scalar(select(AccessLog.id).order_by(AccessLog.id.desc()).limit(1)) or 0
        assert client.get(url).status_code == 200
        after = session.scalar(select(AccessLog.id).order_by(AccessLog.id.desc()).limit(1)) or 0
        assert after > before, url


# The hero's words (window phrasing, no advice vs nothing to do, no product union) are unit
# tested on `advisor.presenter.present_farm_advice`: tests/apps/advisor/test_presenter.py.


def test_full_widgets_span_both_desktop_columns_on_a_wrapper_the_swap_keeps(
    settings, session_factory
) -> None:
    fakes = [
        TODAY,
        Manifest(name="wide", version="0.1.0", widgets=(Widget("/wide/today", 10),)),
        Manifest(name="half", version="0.1.0", widgets=(Widget("/half/today", 20, "half"),)),
        Manifest(name="pair", version="0.1.0", widgets=(Widget("/pair/today", 30, "half"),)),
    ]
    with _app(settings, session_factory, fakes) as c:
        page = c.get("/today/").text
    wide = page.split('hx-get="/wide/today"', 1)[0].rsplit("<div", 2)[-2]
    half = page.split('hx-get="/half/today"', 1)[0].rsplit("<div", 2)[-2]
    assert "lg:col-span-2" in wide and 'data-span="full"' in wide
    assert "lg:col-span-2" not in half


def test_a_half_widget_without_a_partner_goes_full() -> None:
    from farmcore.apps.today.routes import _paired

    full, a, b, c = (
        Widget("/f"),
        Widget("/a", span="half"),
        Widget("/b", span="half"),
        Widget("/c", span="half"),
    )
    assert [w.span for w in _paired([full, a, b, c])] == ["full", "half", "half", "full"]
    assert [w.span for w in _paired([a, full, b])] == ["full", "full", "full"]


# --- Each Today widget sits in the shared named fragment slot (E1) -----------------------


def _slot(page: str, url: str) -> str:
    """The `[data-fragment]` wrapper markup of the slot that loads `url`."""
    head, _ = page.split(f'hx-get="{url}"', 1)
    start = head.rindex("data-fragment ")
    start = head.rindex("<div", 0, start)
    end = page.index("</button>", start)
    return page[start:end]


def test_each_widget_slot_is_named_after_its_label_or_owning_app(settings, session_factory):
    fakes = [
        TODAY,
        Manifest(name="wx", version="0.1.0", title="Weather", widgets=(Widget("/wx/today", 20),)),
        Manifest(
            name="log",
            version="0.1.0",
            title="Diary",
            widgets=(Widget("/log/today", 10, "full", 'Recent "news" <b>'),),
        ),
        Manifest(name="plain", version="0.1.0", widgets=(Widget("/plain/today", 30),)),
    ]
    with _app(settings, session_factory, fakes) as c:
        page = c.get("/today/").text

    order = [page.index(f'hx-get="/{n}/today"') for n in ("log", "wx", "plain")]
    assert order == sorted(order)
    assert "Weather could not load." in _slot(page, "/wx/today")
    assert "Retry Weather" in _slot(page, "/wx/today")
    assert "Recent &#34;news&#34; &lt;b&gt; could not load." in _slot(page, "/log/today")
    assert "Retry Plain" in _slot(page, "/plain/today")
    ids = re.findall(r'<div id="([^"]+)" data-fragment[\s>]', page)
    assert len(ids) == 3 and len(set(ids)) == 3
    assert all(re.fullmatch(r"[a-z0-9-]+", i) for i in ids)


def test_a_failed_widget_keeps_its_slot_and_retry(settings, session_factory):
    """E1: no outerHTML swap and no inline error that replaces the slot."""
    fakes = [TODAY, Manifest(name="wx", version="0.1.0", widgets=(Widget("/wx/today", 20),))]
    with _app(settings, session_factory, fakes) as c:
        page = c.get("/today/").text
    slot = _slot(page, "/wx/today")
    assert "outerHTML" not in slot
    assert "hx-on::response-error" not in page and "hx-on::send-error" not in page
    assert slot.count('hx-get="/wx/today"') == 2  # the load and its Retry, same URL
    assert 'hx-target="#' in slot and 'hx-swap="innerHTML"' in slot


def test_core_recent_activity_is_labelled(settings, session_factory):
    with _app(settings, session_factory, None) as c:
        page = c.get("/today/").text
    assert "Retry Recent activity" in _slot(page, "/activity/today")


def test_today_slots_keep_the_query_farm_over_the_cookie(settings, session_factory):
    fakes = [TODAY, Manifest(name="wx", version="0.1.0", widgets=(Widget("/wx/today", 20),))]
    with _app(settings, session_factory, fakes) as c:
        c.cookies.set(FARM_COOKIE, "1", domain="testserver.local")
        page = c.get("/today/", params={"farm": "7"}).text
    slot = _slot(page, "/wx/today?farm=7")
    assert slot.count('hx-get="/wx/today?farm=7"') == 2


def test_fragments_carry_the_owning_label(settings, session_factory):
    from farmcore.apps.today.routes import _paired

    labelled = Widget("/a", span="half", label="A")
    assert _paired([labelled])[0].label == "A"


def test_today_has_a_main_heading_even_before_widgets_load(client):
    page = client.get("/today/").text
    assert re.search(r"<h1\b[^>]*>Daily brief</h1>", page)


def test_field_home_is_a_daily_brief_with_reachable_secondary_widgets(client):
    field = client.get("/today/").text
    assert "<h1" in field and ">Daily brief</h1>" in field
    for url, label in (
        ("/fields/today?desktop=1", "Farm map"),
        ("/forecast/today", "Spray windows"),
    ):
        detail = re.search(
            r"<details\b[^>]*data-home-secondary[^>]*>(.*?)</details>",
            field[field.index(f">{label}</summary>") - 200 :],
            re.S,
        )
        assert detail is not None
        assert f'hx-get="{url}"' in detail.group(0)
        assert " open" not in detail.group(0).split(">")[0]
    assert "Retry Recent activity" in field
    client.cookies.set("expert", "1")
    expert = client.get("/today/").text
    assert "data-home-secondary" not in expert
    assert 'hx-get="/fields/today?desktop=1"' in expert


def test_field_home_keeps_calm_or_unknown_answer_before_weather_and_secondary_details(client):
    page = client.get("/today/").text
    assert page.index('hx-get="/advisor/today?view=calm"') < page.index(
        'hx-get="/stations/today?compact=1"'
    )
    assert page.index('hx-get="/activity/today"') < page.index('hx-get="/fields/today?desktop=1"')


def test_field_secondary_full_width_disclosures_do_not_leave_recent_activity_half_empty(client):
    field = client.get("/today/").text
    for url in ("/activity/today", "/fields/today?desktop=1", "/forecast/today"):
        assert 'data-span="full"' in _slot(field, url)
    client.cookies.set("expert", "1")
    expert = client.get("/today/").text
    assert 'data-span="half"' in _slot(expert, "/fields/today?desktop=1")

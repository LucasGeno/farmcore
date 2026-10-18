"""The shared shell's optional controls and farm-first navigation."""

from jinja2 import ChoiceLoader, DictLoader

from farmcore import web
from tests.core.test_nav import _app, _page


def test_home_shell_accepts_optional_bell_and_assistant(settings, session_factory, monkeypatch):
    app = _app(settings, session_factory)
    empty = _page(app, "/today/")
    assert ">Home</span>" in empty
    assert 'data-shell-slot="notifications"' in empty
    assert 'data-shell-slot="assistant"' in empty
    assert "Notifications contract" not in empty
    monkeypatch.setattr(
        web.templates.env,
        "loader",
        ChoiceLoader(
            [
                DictLoader(
                    {
                        "core/_notifications_slot.html": "<button>Notifications contract</button>",
                        "core/_assistant_slot.html": "<button>Assistant contract</button>",
                    }
                ),
                web.templates.env.loader,
            ]
        ),
    )
    filled = _page(app, "/today/")
    header = filled.split("<header", 1)[1].split("</header>", 1)[0]
    assert "Notifications contract" in header
    assert "Assistant contract" in filled


def test_home_declares_the_agreed_fragment_sequence(client):
    import re
    from html import unescape

    response = client.get("/today/?farm=1")
    assert response.status_code == 200
    urls = [
        unescape(url)
        for url in re.findall(r'data-fragment-body[^>]*hx-get="([^"]+)"', response.text)
    ]
    widgets = [
        url
        for url in urls
        if url.startswith(
            (
                "/advisor/today",
                "/stations/today",
                "/forecast/",
                "/fields/today",
                "/activity/today",
                "/data-sources/today",
            )
        )
    ]
    assert widgets == [
        "/advisor/today?view=attention&farm=1",
        "/advisor/today?view=calm&farm=1",
        "/stations/today?compact=1&farm=1",
        "/forecast/home-weather?farm=1",
        "/activity/today?farm=1",
        "/fields/today?desktop=1&farm=1",
        "/forecast/today?farm=1",
    ]  # Expert off: the data-source counts are an Expert detail
    client.cookies.set("expert", "1")
    expert = client.get("/today/?farm=1").text
    expert_widgets = [
        unescape(url)
        for url in re.findall(r'data-fragment-body[^>]*hx-get="([^"]+)"', expert)
        if url.startswith(
            (
                "/advisor/today",
                "/stations/today",
                "/forecast/",
                "/fields/today",
                "/activity/today",
                "/data-sources/today",
            )
        )
    ]
    assert expert_widgets == [
        "/advisor/today?view=attention&farm=1",
        "/stations/today?compact=1&farm=1",
        "/forecast/home-weather?farm=1",
        "/fields/today?desktop=1&farm=1",
        "/activity/today?farm=1",
        "/forecast/today?farm=1",
        "/advisor/today?view=calm&farm=1",
        "/data-sources/today?farm=1",
    ]


def test_daily_navigation_keeps_records_in_collapsed_groups(settings, session_factory):
    import re

    page = _page(_app(settings, session_factory), "/today/")
    bottom = page.split('id="bottom-nav"', 1)[1].split("</nav>", 1)[0]
    assert re.findall(r"<span>([^<]+)</span>", bottom) == [
        "Home",
        "Map",
        "Plan",
        "Activity",
        "More",
    ]
    assert 'href="/" aria-current="page"' in bottom
    assert "Register" not in bottom
    for group_id in ("nav-records", "more-records"):
        records = page.split(f'id="{group_id}"', 1)[1].split("</details>", 1)[0]
        assert not re.search(r"^[^>]*\bopen\b", records)
        assert "Records" in records
        for path in ("/operations/register", "/operations/", "/data-sources/"):
            assert f'href="{path}"' in records


def test_register_is_reachable_from_all_apps_with_farm_context(client):
    import re

    response = client.get("/apps?farm=2")
    assert response.status_code == 200
    records = response.text.split('data-app="operations"', 1)[1].split("</article>", 1)[0]
    assert re.search(r'href="/operations/register\?farm=2"[^>]*>\s*Register', records)


def test_data_sources_record_opens_a_full_page_and_keeps_its_fragment(client):
    response = client.get("/data-sources/?farm=2")
    assert response.status_code == 200
    assert "<h1" in response.text and "Data sources</h1>" in response.text
    assert 'hx-get="/data-sources/today?farm=2"' in response.text
    assert 'id="bottom-nav"' in response.text


def test_tour_shell_has_no_demo_admin_or_app_mutations(client):
    assert 'href="/demo/"' in client.get("/today/").text
    client.cookies.set("farmcore_tour", "1")
    assert 'href="/demo/"' not in client.get("/today/").text
    assert 'method="post"' not in client.get("/apps").text


def test_optional_daily_apps_disappear_from_navigation_and_home(client):
    assert client.post("/apps/forecast/uninstall").status_code == 200
    page = client.get("/today/").text
    assert 'href="/forecast/plan"' not in page
    assert 'hx-get="/forecast/' not in page
    assert client.post("/apps/activity/uninstall").status_code == 200
    bottom = client.get("/today/").text.split('id="bottom-nav"', 1)[1].split("</nav>", 1)[0]
    assert "Activity" not in bottom


def test_home_alias_redirect_preserves_explicit_farm(client):
    from farmcore.apps.today.manifest import MANIFEST
    from farmcore.core.nav import active_path, build_nav

    response = client.get("/?farm=2", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/today/?farm=2"
    home = build_nav([MANIFEST], is_admin=False)["farmer"]
    assert active_path(home, "/today/") == "/"
    assert active_path(home, "/") == "/"
    assert active_path(home, "/unknown") is None


def test_main_clears_launcher_and_spray_bar_on_phones():
    from pathlib import Path

    base = Path("src/farmcore/core/templates/core/base.html").read_text()
    main = base.split('<main id="main"', 1)[1].split(">", 1)[0]
    assert "pb-56" in main and "lg:pb-12" in main


def test_launcher_sits_just_above_bottom_nav_or_the_log_bar():
    from pathlib import Path

    css = Path("src/farmcore/core/static/farmcore.css").read_text()
    rule = css.split('[data-shell-slot="assistant"] {', 1)[1].split("}", 1)[0]
    assert "bottom: calc(64px" in rule
    assert 'body:has([data-log-bar]) [data-shell-slot="assistant"]' in css

"""Live-site guards: state-changing app routes and the chat audit page need the admin token."""

from fastapi.testclient import TestClient
from pydantic import SecretStr

from farmcore.core.app_manifest import MANIFEST as CORE
from farmcore.core.manifest import Manifest
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app

TOKEN = "tok-guard"


def _client(settings, session_factory, manifests) -> TestClient:
    with session_factory() as session:
        farm = ensure_default_farm(session, settings)
        farm_id = farm.id
        session.commit()
    app = create_app(
        settings.model_copy(
            update={"admin_token": SecretStr(TOKEN), "demo_mode": True, "demo_farm_ids": [farm_id]}
        ),
        manifests=manifests,
        session_factory=session_factory,
    )
    return TestClient(app)


def test_uninstall_and_install_need_the_admin_token(settings, session_factory) -> None:
    manifests = [CORE, Manifest(name="a", version="0.1.0")]
    with _client(settings, session_factory, manifests) as c:
        assert c.post("/apps/a/uninstall").status_code == 403
        assert c.post("/apps/a/install").status_code == 403
        ok = c.post("/apps/a/uninstall", headers={"X-Admin-Token": TOKEN})
        assert ok.status_code == 200


def test_chat_audit_page_is_denied_without_the_admin_token(settings, session_factory) -> None:
    with session_factory() as s:
        ensure_default_farm(s, settings)
        s.commit()
    with _client(settings, session_factory, None) as c:
        anonymous = c.get("/chat/")
        assert anonymous.status_code == 403
        assert "Full request body" not in anonymous.text
        c.cookies.set("admin_token", TOKEN)
        assert c.get("/chat/").status_code == 200


# --- What anonymous visitors see ------------------------------------------------


def test_anonymous_clock_shows_the_demo_time_only(settings, session_factory) -> None:
    with _client(settings, session_factory, None) as c:
        html = c.get("/clock").text
    assert "Demo clock" in html and "01 Apr 2024 06:00" in html
    assert "<details" in html and "<details open" not in html
    assert 'hx-post="/clock/step"' not in html
    assert 'type="datetime-local"' not in html
    assert "10 Apr 06:00" not in html and "15 May 06:00" not in html


def test_admin_clock_shows_the_step_form_and_presets(settings, session_factory) -> None:
    with _client(settings, session_factory, None) as c:
        c.cookies.set("admin_token", TOKEN)
        html = c.get("/clock").text
    assert "Demo clock" in html and "01 Apr 2024 06:00" in html
    assert 'type="datetime-local"' in html
    assert html.count('hx-post="/clock/step"') == 1 + 2


def test_anonymous_apps_page_has_no_install_or_uninstall_buttons(settings, session_factory) -> None:
    manifests = [CORE, Manifest(name="a", version="0.1.0")]
    with _client(settings, session_factory, manifests) as c:
        html = c.get("/apps").text
    assert ">A<" in html
    assert "/apps/a/uninstall" not in html and "/apps/a/install" not in html
    main = html.split('<main id="main"', 1)[1].split("</main>", 1)[0]  # the shell has a More button
    assert "<button" not in main


def test_admin_apps_page_has_the_uninstall_button(settings, session_factory) -> None:
    manifests = [CORE, Manifest(name="a", version="0.1.0")]
    with _client(settings, session_factory, manifests) as c:
        c.cookies.set("admin_token", TOKEN)
        html = c.get("/apps").text
    assert 'action="/apps/a/uninstall"' in html

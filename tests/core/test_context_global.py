"""Global-page navigation retains explicit farm context without changing app authority.

Two invented owned farms. Follow rendered HTTP links and form actions.
"""

from __future__ import annotations

import html
import re

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from farmcore.core.access import FARM_COOKIE, ensure_owner_grant
from farmcore.core.models.canonical import (
    Farm,
)
from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app
from tests.apps.operations.conftest import make_parcel

HTML = {"Accept": "text/html"}


def attr(markup, tag, name):
    match = re.search(rf"<{tag}\b[^>]*\b{name}=\"([^\"]+)\"", markup)
    assert match is not None, markup
    return html.unescape(match[1])


def tile(page, name):
    return page.split(f'data-app="{name}"', 1)[1].split("</article>", 1)[0]


def link(markup, label):
    match = re.search(rf'<a\b[^>]*href="([^"]+)"[^>]*>\s*{re.escape(label)}\s*</a>', markup)
    assert match is not None, label
    return html.unescape(match[1])


@pytest.fixture
def farms(client, session, settings):
    first = ensure_default_farm(session, settings)
    second = Farm(name="Context QA B")
    session.add(second)
    session.flush()
    ensure_owner_grant(session, second)
    make_parcel(session, first, 101, area="1", props={})
    make_parcel(session, second, 202, area="2", props={})
    session.commit()
    client.cookies.set(FARM_COOKIE, str(first.id), domain="testserver.local")
    return first, second


def test_app_tile_opens_the_query_farm_instead_of_the_cookie_farm(client, farms):
    first, second = farms
    page = client.get("/apps", params={"farm": second.id}).text

    response = client.get(attr(tile(page, "fields"), "a", "href"))

    assert response.status_code == 200
    assert response.url.path == "/fields/"
    assert response.url.params["farm"] == str(second.id)
    assert "Test parcel 202" in response.text
    assert "P101 · Test parcel 101" not in response.text
    assert client.cookies.get(FARM_COOKIE) == str(first.id)


def test_app_switch_forms_keep_navigation_context_but_change_global_state(client, farms):
    first, second = farms
    page = client.get("/apps", params={"farm": second.id}).text
    action = attr(tile(page, "share"), "form", "action")

    response = client.post(action, headers=HTML, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/apps?farm={second.id}"
    assert action == f"/apps/share/uninstall?farm={second.id}"
    redirected = client.get(response.headers["location"])
    off = tile(redirected.text, "share")
    assert 'data-status="off"' in off
    # The same instance-global state is visible from another farm, not a B-only preference.
    other_page = client.get("/apps", params={"farm": first.id})
    assert 'data-status="off"' in tile(other_page.text, "share")
    installed = client.post(attr(off, "form", "action"), headers=HTML, follow_redirects=False)
    assert installed.status_code == 303
    assert installed.headers["location"] == f"/apps?farm={second.id}"
    assert 'data-status="on"' in tile(client.get(installed.headers["location"]).text, "share")
    assert client.cookies.get(FARM_COOKIE) == str(first.id)


def test_error_back_link_recovers_to_the_query_farm(client, farms):
    first, second = farms
    error = client.get("/stations/999999", params={"farm": second.id}, headers=HTML)
    assert error.status_code == 404
    alert = error.text.split('role="alert"', 1)[1].split("</section>", 1)[0]

    recovery = client.get(attr(alert, "a", "href"), follow_redirects=False)

    assert recovery.status_code == 307
    assert recovery.headers["location"] == f"/today/?farm={second.id}"
    assert client.get(recovery.headers["location"]).status_code == 200
    assert client.cookies.get(FARM_COOKIE) == str(first.id)


def test_app_actions_still_require_admin_even_with_valid_farm_context(
    farms, settings, session_factory
):
    first, second = farms
    secured = settings.model_copy(
        update={
            "admin_token": SecretStr("qa-global-admin"),
            "demo_mode": True,
            "demo_farm_ids": [second.id],
        }
    )
    with TestClient(create_app(secured, session_factory=session_factory)) as browser:
        browser.cookies.set(FARM_COOKIE, str(first.id), domain="testserver.local")
        browser.cookies.set("admin_token", "qa-global-admin")
        for action, before, after in (("uninstall", "on", "off"), ("install", "off", "on")):
            page = browser.get("/apps", params={"farm": second.id}).text
            actual_action = attr(tile(page, "share"), "form", "action")
            assert actual_action == f"/apps/share/{action}?farm={second.id}"
            browser.cookies.delete("admin_token")

            denied = browser.post(actual_action, headers=HTML, follow_redirects=False)

            assert denied.status_code == 403
            readonly = browser.get("/apps", params={"farm": second.id}).text
            assert 'data-status="' + before + '"' in tile(readonly, "share")
            assert "<form" not in tile(readonly, "share")
            browser.cookies.set("admin_token", "qa-global-admin")
            changed = browser.post(actual_action, headers=HTML, follow_redirects=False)
            assert changed.status_code == 303
            assert changed.headers["location"] == f"/apps?farm={second.id}"
            assert f'data-status="{after}"' in tile(
                browser.get(changed.headers["location"]).text, "share"
            )
        assert browser.cookies.get(FARM_COOKIE) == str(first.id)


@pytest.mark.parametrize("name", ["fields", "stations"])
def test_app_refusal_keeps_scoped_navigation_and_global_constraints(client, farms, name):
    first, second = farms
    refused = client.post(f"/apps/{name}/uninstall?farm={second.id}", headers=HTML)
    assert refused.status_code == 409
    assert 'role="alert"' in refused.text
    assert attr(tile(refused.text, "fields"), "a", "href") == f"/fields/?farm={second.id}"
    assert attr(tile(refused.text, "share"), "form", "action").endswith(f"?farm={second.id}")
    states = {a["name"]: a["state"] for a in client.get("/apps.json").json()}
    assert states[name] == "installed"
    assert client.cookies.get(FARM_COOKIE) == str(first.id)


@pytest.mark.parametrize("selector", ["not-a-number", "-1", "999999"])
def test_global_app_actions_do_not_acquire_farm_scoped_authority(client, farms, selector):
    first, _ = farms
    response = client.post(
        "/apps/share/uninstall", params={"farm": selector}, headers=HTML, follow_redirects=False
    )
    assert response.status_code == 303  # An admin's global setting needs no farm repository.
    expected = "/apps?farm=999999" if selector == "999999" else "/apps"
    assert response.headers["location"] == expected
    restored = client.post("/apps/share/install", params={"farm": selector})
    assert restored.status_code == 200
    assert restored.json() == {
        "name": "share",
        "state": "installed",
        "applies": "at next start",
        "ui": "now",
    }
    assert client.cookies.get(FARM_COOKIE) == str(first.id)


def test_unowned_query_on_global_apps_page_cannot_grant_access_to_its_tile_target(
    client, farms, session
):
    first, _ = farms
    unowned = Farm(name="QA unowned farm")
    session.add(unowned)
    session.commit()
    page = client.get("/apps", params={"farm": unowned.id}).text

    target = client.get(attr(tile(page, "fields"), "a", "href"))

    assert target.status_code == 200
    assert target.url.path == "/fields/"
    assert "Test parcel 101" in target.text
    assert "Test parcel 202" not in target.text
    assert client.cookies.get(FARM_COOKIE) == str(first.id)


@pytest.mark.parametrize("selector", ["not-a-number", "999999"])
def test_error_recovery_uses_resolved_farm_after_invalid_query(client, farms, selector):
    _, second = farms
    client.cookies.set(FARM_COOKIE, str(second.id), domain="testserver.local")
    error = client.get("/stations/999999", params={"farm": selector}, headers=HTML)
    assert error.status_code == 404
    alert = error.text.split('role="alert"', 1)[1].split("</section>", 1)[0]
    recovered = client.get(attr(alert, "a", "href"))
    assert recovered.status_code == 200
    assert recovered.url.params["farm"] == str(second.id)
    assert client.cookies.get(FARM_COOKIE) == str(second.id)

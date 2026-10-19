"""`/admin/sign-in`: the administrator pastes ADMIN_TOKEN once; the server sets the admin cookie.

The cookie is `Secure`, so these clients use `https://testserver`: an http client (the default
`http://testserver`) would never send it back, which is the browser's behaviour too.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from farmcore.core.seed import ensure_default_farm
from farmcore.main import create_app

TOKEN = "s3cret-admin-token-7f2c9d"
BASE = "https://testserver"
ORIGIN = {"Origin": BASE}


@pytest.fixture(autouse=True)
def delays(monkeypatch) -> list[float]:
    """Failed attempts sleep `FAIL_DELAY_S`; recorded here, never actually waited."""
    from farmcore.core import routes_admin

    waited: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    monkeypatch.setattr(routes_admin, "_sleep", fake_sleep)
    return waited


@pytest.fixture
def admin_client(settings, session_factory, session) -> Iterator[TestClient]:
    ensure_default_farm(session, settings)
    session.commit()
    app = create_app(
        settings.model_copy(update={"admin_token": SecretStr(TOKEN)}),
        session_factory=session_factory,
    )
    with TestClient(app, base_url=BASE) as c:
        yield c


def _sign_in(c, token=TOKEN, headers=None, **kw):
    return c.post(
        "/admin/sign-in",
        data={"token": token},
        headers=headers or ORIGIN,
        follow_redirects=False,
        **kw,
    )


def test_sign_in_page_is_a_password_form_without_autocomplete(admin_client):
    page = admin_client.get("/admin/sign-in").text

    assert 'type="password"' in page and 'name="token"' in page
    assert 'autocomplete="off"' in page and 'method="post"' in page
    assert TOKEN not in page


def test_right_token_sets_a_strict_secure_cookie_and_opens_the_demo_page(admin_client):
    assert admin_client.get("/demo/").status_code == 403

    r = _sign_in(admin_client)

    assert r.status_code == 303 and r.headers["location"] == "/demo/"
    cookie = r.headers["set-cookie"]
    for attr in ("admin_token=", "HttpOnly", "Secure", "SameSite=strict", "Path=/"):
        assert attr.lower() in cookie.lower(), attr
    assert admin_client.get("/demo/").status_code == 200


def test_wrong_token_is_403_and_sets_nothing(admin_client):
    r = _sign_in(admin_client, token="wrong")

    assert r.status_code == 403
    assert "set-cookie" not in r.headers
    assert admin_client.get("/demo/").status_code == 403


def test_wrong_attempts_are_throttled_but_the_right_token_always_signs_in(admin_client):
    for _ in range(5):
        assert _sign_in(admin_client, token="wrong").status_code == 403

    limited = _sign_in(admin_client, token="wrong")
    assert limited.status_code == 429 and limited.headers["Retry-After"]
    assert "set-cookie" not in limited.headers

    ok = _sign_in(admin_client)  # the presenter is never locked out
    assert ok.status_code == 303 and "admin_token=" in ok.headers["set-cookie"]


def test_the_limiter_keys_on_the_forwarded_client_address(admin_client):
    a = {"CF-Connecting-IP": "203.0.113.7"}
    for _ in range(5):
        assert _sign_in(admin_client, token="wrong", headers={**ORIGIN, **a}).status_code == 403
    assert _sign_in(admin_client, token="wrong", headers={**ORIGIN, **a}).status_code == 429

    other = {"X-Forwarded-For": "198.51.100.9, 10.0.0.1"}
    assert _sign_in(admin_client, token="wrong", headers={**ORIGIN, **other}).status_code == 403
    assert _sign_in(admin_client, token="wrong").status_code == 403  # the direct client


def test_every_failed_attempt_is_delayed_and_success_is_not(admin_client, delays):
    from farmcore.core import routes_admin

    _sign_in(admin_client, token="wrong")
    _sign_in(admin_client, token="wrong")
    assert delays == [routes_admin.FAIL_DELAY_S] * 2 and routes_admin.FAIL_DELAY_S >= 0.5

    _sign_in(admin_client)
    assert len(delays) == 2


def test_tour_cookie_is_refused(admin_client):
    admin_client.cookies.set("farmcore_tour", "1")

    assert "disabled in tour mode" in admin_client.get("/admin/sign-in").text
    r = _sign_in(admin_client)

    assert r.status_code == 403 and "set-cookie" not in r.headers


def test_cross_site_post_is_refused(admin_client):
    r = admin_client.post(
        "/admin/sign-in",
        data={"token": TOKEN},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )

    assert r.status_code == 403 and "set-cookie" not in r.headers


def test_sign_out_clears_the_cookie(admin_client):
    _sign_in(admin_client)
    assert admin_client.get("/demo/").status_code == 200

    r = admin_client.post("/admin/sign-out", headers=ORIGIN, follow_redirects=False)

    assert r.status_code == 303
    assert "admin_token=" in r.headers["set-cookie"] and "Max-Age=0" in r.headers["set-cookie"]
    assert admin_client.get("/demo/").status_code == 403


def test_no_sign_in_link_for_anonymous_visitors(admin_client):
    page = admin_client.get("/today/").text

    assert "/admin/sign-in" not in page


def test_the_token_never_appears_in_responses_or_logs(admin_client, caplog):
    caplog.set_level(logging.DEBUG)
    bodies = [
        admin_client.get("/admin/sign-in"),
        _sign_in(admin_client, token=TOKEN + "x"),
        _sign_in(admin_client),
        admin_client.get("/demo/"),
        admin_client.get("/admin/sign-in"),
        admin_client.post("/admin/sign-out", headers=ORIGIN, follow_redirects=False),
    ]

    for r in bodies:
        assert TOKEN not in r.text
        assert TOKEN not in r.headers.get("location", "")
    # The Set-Cookie header necessarily carries the token once (that is the sign-in); nothing
    # else may: no URL, body or log line.
    assert TOKEN not in caplog.text


def test_without_a_configured_token_the_page_says_so(client, session, settings):
    client.app.state.settings = settings.model_copy(update={"admin_token": None})
    ensure_default_farm(session, settings)
    session.commit()
    assert client.app.state.settings.admin_token is None

    page = client.get("/admin/sign-in").text

    assert "No admin token is configured" in page
    r = client.post(
        "/admin/sign-in",
        data={"token": "anything"},
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert r.status_code == 409 and "set-cookie" not in r.headers

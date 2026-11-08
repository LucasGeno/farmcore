"""Coverage for core helpers: cookie scoping, plain errors, registry and sign-in paths."""

from __future__ import annotations

import asyncio

from sqlalchemy import text
from starlette.exceptions import HTTPException

from farmcore.core import errors, registry
from farmcore.core.cookie_scope import InstallationCookies, _scope_request_cookies
from farmcore.core.manifest import Manifest
from farmcore.core.routes_identity import return_path

INSTALLED = registry.INSTALLED


def test_cookie_headers_are_scoped_to_this_installation():
    prefix = "fc-abc123-"
    scoped = _scope_request_cookies(f"other=1; {prefix}farmcore_session=token; {prefix}", prefix)
    assert scoped == "farmcore_session=token"

    seen = {}

    async def app(scope, receive, send):
        seen["headers"] = scope["headers"]
        seen["state"] = scope["state"]
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"set-cookie", b"farmcore_session=x"),
                    (b"x-other", b"y"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    sent = []

    async def send(message):
        sent.append(message)

    middleware = InstallationCookies(app, "abc-123")
    asyncio.run(
        middleware(
            {
                "type": "http",
                "headers": [(b"cookie", b"other=1")],
                "state": {},
            },
            None,
            send,
        )
    )
    assert seen["headers"] == []
    assert seen["state"]["cookie_prefix"] == "fc-abc123-"
    assert sent[0]["headers"] == [
        (b"set-cookie", b"fc-abc123-farmcore_session=x"),
        (b"x-other", b"y"),
    ]

    async def non_http_app(scope, receive, send):
        seen["non_http"] = scope["type"]

    asyncio.run(InstallationCookies(non_http_app, "abc-123")({"type": "lifespan"}, None, send))
    assert seen["non_http"] == "lifespan"


def test_plain_auth_refusal_and_unknown_detail():
    assert errors._message(HTTPException(403, errors.AUTH_REQUIRED)) == (
        "Your account cannot open this page."
    )
    assert errors._message(HTTPException(418, "short and stout")) == ("418: short and stout")


def test_switch_refusal_names_two_missing_dependencies():
    app = Manifest(name="orchard", version="1", depends=("irrigation", "weather"))
    refusal = registry.switch_refusal(
        [registry.CORE, app],
        {"core": INSTALLED, "orchard": INSTALLED},
        app,
        INSTALLED,
    )
    assert refusal is not None
    assert "irrigation and weather" in refusal.detail
    assert refusal.extra == {"uninstalled_dependencies": ["irrigation", "weather"]}


def test_sign_in_next_never_points_back_to_login_or_logout():
    assert return_path("/login?next=/today") == ""
    assert return_path("/logout") == ""
    assert return_path("/today") == "/today"


def test_health_is_503_when_the_schema_is_not_ok(client, session):
    session.execute(text("UPDATE alembic_version SET version_num = 'deadbeef'"))
    session.flush()
    response = client.get("/health")
    assert response.status_code == 503
    assert response.json()["state"] != "ok"

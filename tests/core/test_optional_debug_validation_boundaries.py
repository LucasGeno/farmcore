"""HTTP error and pre-seed debug boundaries, with a configured admin credential.

The validation routes are synthetic ASGI consumers of the real registered error handlers;
they make missing and oversized public inputs observable without patching presentation code.
"""

from __future__ import annotations

import html
import re

from fastapi import Query
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from farmcore.core.models.canonical import Advice, Event, Farm, Log
from farmcore.main import create_app

TOKEN = "qa-admin"
HTML = {"accept": "text/html"}


def test_admin_debug_before_seed_has_clock_and_no_invented_farm(settings, session_factory, session):
    guarded = settings.model_copy(update={"admin_token": SecretStr(TOKEN)})
    with TestClient(create_app(guarded, session_factory=session_factory)) as client:
        anonymous = client.get("/debug/drawer", params={"debug": "1"})
        client.cookies.set("admin_token", TOKEN)
        response = client.get("/debug/drawer", params={"debug": "1", "path": "/fields/"})

    assert anonymous.status_code == 403
    assert "set-cookie" not in anonymous.headers
    assert response.status_code == 200
    assert "No farm seeded yet" in response.text
    assert 'data-clock-mode="replay"' in response.text
    assert "data-farm-id=" not in response.text
    assert 'id="debug-drawer"' in response.text
    assert response.headers["set-cookie"].startswith("farmcore_debug=1;")
    for model in (Farm, Log, Advice, Event):
        assert session.scalar(select(func.count()).select_from(model)) == 0


def test_validation_missing_and_long_input_get_plain_safe_html(settings, session_factory):
    app = create_app(settings, session_factory=session_factory)

    @app.get("/__synthetic__/integer")
    def integer(value: int = Query()):
        return {"value": value}

    with TestClient(
        app, headers={"X-Admin-Token": settings.admin_token.get_secret_value()}
    ) as client:
        missing = client.get("/__synthetic__/integer", headers=HTML)
        long = client.get("/__synthetic__/integer", params={"value": "x" * 80}, headers=HTML)
        json_error = client.get("/__synthetic__/integer", params={"value": "x" * 80})
        valid = client.get("/__synthetic__/integer", params={"value": "17"})

    assert missing.status_code == 422
    assert "The value is missing." in missing.text
    assert long.status_code == 422
    assert f"“{'x' * 40}…” is not a valid value: it should be a whole number." in html.unescape(
        long.text
    )
    alert = re.search(r'<section role="alert".*?</section>', long.text, re.DOTALL)
    assert alert is not None and "x" * 41 not in alert.group()
    assert "Traceback" not in long.text
    assert json_error.status_code == 422
    assert json_error.json()["detail"][0]["input"] == "x" * 80
    assert valid.status_code == 200 and valid.json() == {"value": 17}

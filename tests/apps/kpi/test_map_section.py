"""`GET /kpi/map/farm`, Farm today's one honest link to Season & impact: no figures, the
selected farm kept in the link (`?farm=` before the cookie)."""

from __future__ import annotations

import html
import re

from farmcore.apps.kpi.manifest import MANIFEST
from farmcore.core.access import FARM_COOKIE, ensure_owner_grant
from farmcore.core.models.canonical import Farm
from farmcore.core.seed import ensure_default_farm


def test_manifest_declares_the_farm_section() -> None:
    assert MANIFEST.farm_sections == ("/kpi/map/farm",)
    assert MANIFEST.parcel_sections == ()


def test_one_link_and_no_figures(client, session, settings) -> None:
    ensure_default_farm(session, settings)
    session.commit()
    resp = client.get("/kpi/map/farm")
    assert resp.status_code == 200
    assert resp.text.lstrip().startswith("<section") and "<html" not in resp.text
    links = re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', resp.text, re.S)
    shown = [(h, html.unescape(re.sub(r"<[^>]+>", "", t)).strip()) for h, t in links]
    assert shown == [("/kpi/", "Season & impact")]
    body = re.sub(r"<[^>]+>", " ", html.unescape(resp.text))
    assert not re.search(r"\d", body)


def test_the_link_keeps_the_query_farm(client, session, settings) -> None:
    first = ensure_default_farm(session, settings)
    second = Farm(name="Map QA B")
    session.add(second)
    session.flush()
    ensure_owner_grant(session, second)
    session.commit()
    client.cookies.set(FARM_COOKIE, str(first.id), domain="testserver.local")
    resp = client.get("/kpi/map/farm", params={"farm": second.id})
    assert resp.status_code == 200
    assert f'href="/kpi/?farm={second.id}"' in resp.text

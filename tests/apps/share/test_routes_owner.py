"""`/share/`: the grants table, the admin-only grant form and revoke buttons, plain-words 422."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from farmcore.core.models.canonical import AccessLog, DataGrant, Event

PARIS = ZoneInfo("Europe/Paris")


def _today() -> date:
    """Today in the route's display timezone (Europe/Paris), not the machine's."""
    return datetime.now(PARIS).date()


def _form(**over) -> dict[str, str]:
    data = {
        "grantee": "Co-op adviser",
        "recipient_login": "share-recipient",
        "preset": "adviser_logs",
        "purpose": "spray advice",
        "valid_to": (_today() + timedelta(days=30)).isoformat(),
    }
    return {**data, **over}


def _third_party_grants(session) -> list[DataGrant]:
    session.expire_all()
    return list(
        session.scalars(
            select(DataGrant).where(DataGrant.grantee != "owner").order_by(DataGrant.id)
        )
    )


def test_apps_list_and_navbar_show_share(admin, demo) -> None:
    names = [a["name"] for a in admin.get("/apps.json").json()]
    assert "share" in names
    page = admin.get("/share/")
    assert page.status_code == 200
    assert 'href="/share/"' in page.text


def test_admin_post_creates_one_grant_on_the_selected_farm_and_redirects(
    admin, session, demo
) -> None:
    until = _today() + timedelta(days=30)

    resp = admin.post(
        "/share/grants", data=_form(valid_to=until.isoformat()), follow_redirects=False
    )

    assert resp.status_code == 303
    assert resp.headers["location"] == "/share/"
    [grant] = _third_party_grants(session)
    assert grant.farm_id == demo.farm.id
    assert grant.scopes == ["logs:read", "register:read"]
    assert grant.valid_to.astimezone(PARIS).date() == until
    assert len(session.scalars(select(Event).where(Event.kind == "share.granted")).all()) == 1
    page = admin.get("/share/").text
    assert "Co-op adviser" in page and "spray advice" in page
    assert "spray logs (<code>logs:read</code>)" in page
    assert "spray register (<code>register:read</code>)" in page
    assert "active" in page
    assert f'href="/share/as/{grant.id}/"' in page


@pytest.mark.parametrize(
    "over",
    [
        {"grantee": ""},
        {"grantee": "OWNER"},
        {"preset": "everything"},
        {"valid_to": (_today() - timedelta(days=1)).isoformat()},
        {"valid_to": "not a date"},
    ],
)
def test_invalid_input_is_a_plain_words_422_and_writes_nothing(admin, session, demo, over) -> None:
    resp = admin.post("/share/grants", data=_form(**over), headers={"Accept": "text/html"})

    assert resp.status_code == 422
    assert "<html" in resp.text and "Traceback" not in resp.text
    assert _third_party_grants(session) == []


def test_anonymous_cannot_read_consent_ledger_or_post(anonymous, admin, session, demo) -> None:
    admin.post("/share/grants", data=_form(), follow_redirects=False)
    [grant] = _third_party_grants(session)

    page = anonymous.get("/share/")

    assert page.status_code == 403
    assert "Co-op adviser" not in page.text
    assert 'action="/share/grants"' not in page.text
    assert not re.search(r"<button\b[^>]*>\s*Revoke\s*</button>", page.text)
    assert "Access log" not in page.text
    assert anonymous.post("/share/grants", data=_form()).status_code == 403
    assert anonymous.post(f"/share/grants/{grant.id}/revoke").status_code == 403
    assert len(_third_party_grants(session)) == 1


def test_admin_page_shows_the_form_with_defaults(admin, demo) -> None:
    page = admin.get("/share/").text
    assert f'action="/share/grants?farm={demo.farm.id}"' in page
    assert 'value="Co-op adviser"' in page and 'value="spray advice"' in page
    assert (_today() + timedelta(days=30)).isoformat() in page
    assert "Co-op adviser — spray logs and register (read only)" in page


def test_owner_page_reads_under_audit_scope(admin, session, demo) -> None:
    last = session.scalar(select(AccessLog.id).order_by(AccessLog.id.desc()).limit(1)) or 0

    admin.get("/share/")

    session.expire_all()
    scopes = set(session.scalars(select(AccessLog.scope).where(AccessLog.id > last)))
    assert "audit:read" in scopes


def test_share_ngsi_export_downloads_without_forcing_attachment_headers(admin, demo):
    class ExportLink(HTMLParser):
        def __init__(self):
            super().__init__()
            self.attrs = None

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "a" and attrs.get("href") == "/share/ngsi-ld.json?season=2024":
                self.attrs = attrs

    parser = ExportLink()
    parser.feed(admin.get("/share/").text)
    assert parser.attrs is not None
    assert parser.attrs.get("download") == "farm-ngsi-ld-2024.json"
    assert "target" not in parser.attrs
    response = admin.get("/share/ngsi-ld.json")
    assert response.status_code == 200
    assert "attachment" not in response.headers.get("content-disposition", "")


def test_tour_hides_grantee_impersonation_links_even_with_admin_cookie(admin, demo):
    admin.post("/share/grants", data=_form(), follow_redirects=False)
    assert "/share/as/" in admin.get("/share/").text
    admin.get("/tour")
    page = admin.get("/share/")
    assert page.status_code == 200
    assert "/share/as/" not in page.text
    assert 'action="/share/grants"' not in page.text
    assert not re.search(r"<button\b[^>]*>\s*Revoke\s*</button>", page.text)

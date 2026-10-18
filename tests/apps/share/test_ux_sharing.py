"""Sharing UX: filters change only the displayed grants, never their permissions or audit reads."""

import re

from sqlalchemy import select

from farmcore.core.models.canonical import DataGrant
from tests.apps.share.test_routes_owner import _form


def _ids(text):
    return re.findall(r'data-grant="(\d+)"', text)


def test_scope_and_status_filters_keep_existing_grants_and_access_log(admin, session, demo):
    for grantee, preset in (
        ("Synthetic adviser", "adviser_logs"),
        ("Synthetic weather", "station"),
    ):
        assert (
            admin.post(
                "/share/grants", data=_form(grantee=grantee, preset=preset), follow_redirects=False
            ).status_code
            == 303
        )
    grants = session.scalars(
        select(DataGrant).where(DataGrant.grantee != "owner").order_by(DataGrant.id)
    ).all()
    adviser, weather = grants
    page = admin.get("/share/", params={"scope": "weather", "status": "active", "who": "grantees"})
    assert page.status_code == 200
    assert _ids(page.text) == [str(weather.id)]
    assert "Access log" in page.text
    assert "View as Synthetic weather" in page.text
    assert "who=grantees" in page.text
    assert (
        admin.post(f"/share/grants/{adviser.id}/revoke", follow_redirects=False).status_code == 303
    )
    page = admin.get("/share/", params={"scope": "sprays", "status": "revoked"})
    assert _ids(page.text) == [str(adviser.id)]
    session.expire_all()
    assert session.get(DataGrant, weather.id).scopes == ["stations:read"]
    assert session.get(DataGrant, adviser.id).scopes == ["logs:read", "register:read"]


def test_plain_sharing_sections_unsupported_choices_and_collapsed_audit(admin, anonymous, demo):
    page = admin.get("/share/").text
    headings = re.findall(r"<h2[^>]*>([^<]+)</h2>", page)
    assert headings[:2] == ["What", "With whom"]
    assert '<option value="parcels" disabled>' in page
    assert '<option value="kpis" disabled>' in page
    assert "No sharing preset for parcels" in page
    assert "No sharing preset for KPIs" in page
    details = re.search(r'<details id="access-log"([^>]*)>', page)
    assert details is not None and "open" not in details[1]
    assert 'download="farm-ngsi-ld-2024.json"' in page
    assert 'download="spray-register-2024.csv"' in page
    filtered = admin.get("/share/?scope=weather&status=active&who=grantees").text
    assert "open" in re.search(r'<details id="access-log"([^>]*)>', filtered)[1]
    assert "scope=weather" in filtered and "status=active" in filtered
    page = anonymous.get("/share/").text
    assert 'action="/share/grants"' not in page
    assert "No sharing preset for parcels" in page


def test_filters_and_forms_remain_farm_scoped_and_tour_read_only(admin, session, demo, other_farm):
    for farm, name in ((demo.farm, "Synthetic FR adviser"), (other_farm, "Synthetic ES adviser")):
        assert (
            admin.post(
                f"/share/grants?farm={farm.id}", data=_form(grantee=name), follow_redirects=False
            ).status_code
            == 303
        )
    page = admin.get(f"/share/?farm={other_farm.id}&scope=sprays&status=active&who=grantees").text
    assert "Synthetic ES adviser" in page and "Synthetic FR adviser" not in page
    assert f'name="farm" value="{other_farm.id}"' in page
    assert "scope=sprays" in page and "status=active" in page
    admin.cookies.set("farmcore_tour", "1")
    response = admin.get(f"/share/?farm={other_farm.id}&scope=sprays&status=active")
    assert response.status_code == 200 and "Synthetic ES adviser" in response.text
    assert "/share/as/" not in response.text
    assert not re.search(r'<form[^>]*method="post"', response.text)
    assert admin.post(f"/share/grants?farm={other_farm.id}", data=_form()).status_code == 403
    assert admin.get("/share/?scope=unsupported").status_code == 422
    assert admin.get("/share/?status=unsupported").status_code == 422

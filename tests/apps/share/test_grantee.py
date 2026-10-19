"""`/share/as/{id}/`: the grantee reads the grant's farm only through a grant-bound repository."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from farmcore.apps.share import service
from farmcore.core.access import FARM_COOKIE, OWNER, ensure_owner_grant
from farmcore.core.models.canonical import AccessLog, Asset, LlmAudit, Observation

OBS_VALUE = 271.828  # a synthetic, recognisable station value
AUDIT_SECRET = "synthetic-audit-body-7f3a"


@pytest.fixture
def grant(session, owner_repo):
    g = service.create_grant(
        owner_repo,
        grantee="Co-op adviser",
        preset="adviser_logs",
        purpose="spray advice",
        valid_to=datetime.now(UTC) + timedelta(days=30),
    )
    session.commit()
    return g


@pytest.fixture
def private_rows(session, demo):
    """A station observation and an llm_audit row the logs-only grant must never return."""
    station = Asset(farm_id=demo.farm.id, kind="station", name="Synthetic station")
    session.add(station)
    session.flush()
    session.add(
        Observation(
            asset_id=station.id,
            ts=datetime(2024, 4, 10, tzinfo=UTC),
            variable="temp_c",
            value=OBS_VALUE,
        )
    )
    session.add(
        LlmAudit(
            adapter="fake",
            model="fake",
            endpoint="fake",
            request_body={"contents": [{"role": "user", "parts": [{"text": AUDIT_SECRET}]}]},
            response_body={"text": AUDIT_SECRET},
            status="ok",
        )
    )
    session.commit()


def _since(session, last: int) -> list[AccessLog]:
    session.expire_all()
    return list(
        session.scalars(select(AccessLog).where(AccessLog.id > last).order_by(AccessLog.id))
    )


def _last(session) -> int:
    return session.scalar(select(AccessLog.id).order_by(AccessLog.id.desc()).limit(1)) or 0


def test_register_json_returns_the_grant_farm_rows_and_logs_each_read(
    anonymous, session, demo, spray, grant
) -> None:
    log = spray()
    last = _last(session)

    resp = anonymous.get(f"/share/as/{grant.id}/register.json")

    assert resp.status_code == 200
    rows = resp.json()
    assert {r["log_id"] for r in rows} == {log.id}
    assert {r["parcel_alias"] for r in rows} == {"P1", "P2"}
    reads = _since(session, last)
    assert reads
    for row in reads:
        assert (row.actor, row.grant_id, row.purpose, row.scope, row.farm_id) == (
            f"grant:{grant.id}",
            grant.id,
            "spray advice",
            "register:read",
            demo.farm.id,
        )


def test_html_page_shows_the_banner_and_the_register(anonymous, demo, spray, grant) -> None:
    spray()

    page = anonymous.get(f"/share/as/{grant.id}/")

    assert page.status_code == 200
    assert "You are viewing as" in page.text and "Co-op adviser" in page.text
    assert f"grant #{grant.id}" in page.text
    assert "BOUILLIE BORDELAISE" in page.text
    assert "Data Act" in page.text


def test_grantee_view_follows_the_grant_farm_not_the_farm_cookie(
    anonymous, admin, session, demo, other_farm, spray, grant
) -> None:
    log = spray()
    anonymous.cookies.set(FARM_COOKIE, str(other_farm.id))
    last = _last(session)

    assert anonymous.get(f"/share/as/{grant.id}/register.json").status_code == 403
    assert _since(session, last) == []
    admin.cookies.set(FARM_COOKIE, str(other_farm.id))
    response = admin.get(f"/share/as/{grant.id}/register.json")
    assert response.status_code == 200
    rows = response.json()

    assert {r["log_id"] for r in rows} == {log.id}
    assert {r.farm_id for r in _since(session, last)} == {demo.farm.id}


def test_owner_grant_and_unknown_id_are_404(anonymous, session, demo) -> None:
    owner = ensure_owner_grant(session, demo.farm)
    session.commit()
    assert owner.grantee == OWNER

    assert anonymous.get(f"/share/as/{owner.id}/").status_code == 404
    assert anonymous.get(f"/share/as/{owner.id}/register.json").status_code == 404
    assert anonymous.get("/share/as/999999/").status_code == 404
    assert anonymous.get("/share/as/999999/register.json").status_code == 404
    assert anonymous.get(f"/share/as/{2**64}/").status_code == 404
    assert anonymous.get("/share/as/0/").status_code == 404


def test_station_and_audit_reads_are_403_and_log_nothing(
    anonymous, session, demo, private_rows, grant
) -> None:
    last = _last(session)

    obs = anonymous.get(f"/share/as/{grant.id}/observations.json")
    audit = anonymous.get(f"/share/as/{grant.id}/audit.json")

    assert obs.status_code == 403
    assert "weather station data" in obs.json()["detail"]
    assert audit.status_code == 403
    assert audit.json()["detail"] == "admin token required"
    for resp in (obs, audit):
        assert str(OBS_VALUE) not in resp.text and AUDIT_SECRET not in resp.text
    assert _since(session, last) == []


def test_a_station_grant_reads_observations_but_not_the_register(
    anonymous, session, owner_repo, demo, private_rows
) -> None:
    g = service.create_grant(
        owner_repo,
        grantee="Weather service",
        preset="station",
        purpose="forecast",
        valid_to=datetime.now(UTC) + timedelta(days=30),
    )
    session.commit()

    obs = anonymous.get(f"/share/as/{g.id}/observations.json")
    page = anonymous.get(f"/share/as/{g.id}/").text

    assert obs.status_code == 200 and [o["value"] for o in obs.json()] == [OBS_VALUE]
    assert anonymous.get(f"/share/as/{g.id}/register.json").status_code == 403
    assert "Shared: this grant includes weather station data." in page
    assert "Not shared: this grant does not include the spray register." in page
    assert AUDIT_SECRET not in page


def test_html_page_shows_both_not_shared_panels_and_no_private_data(
    anonymous, session, demo, spray, private_rows, grant
) -> None:
    spray()
    last = _last(session)

    page = anonymous.get(f"/share/as/{grant.id}/").text

    assert "Not shared: this grant does not include weather station data." in page
    assert "Not shared: this grant does not include the model-call audit." in page
    assert str(OBS_VALUE) not in page and AUDIT_SECRET not in page
    assert {r.scope for r in _since(session, last)} == {"register:read"}


def test_phone_register_cells_keep_their_column_labels(anonymous, spray, grant):
    from html import unescape
    from html.parser import HTMLParser

    class RegisterCells(HTMLParser):
        def __init__(self):
            super().__init__()
            self.labels = []
            self.headings = []
            self.in_heading = False

        def handle_starttag(self, tag, attrs):
            if tag == "td":
                self.labels.append(dict(attrs).get("data-label"))
            self.in_heading = tag == "th"

        def handle_endtag(self, tag):
            if tag == "th":
                self.in_heading = False

        def handle_data(self, data):
            if self.in_heading:
                self.headings.append(unescape(data).strip())

    spray()
    page = anonymous.get(f"/share/as/{grant.id}/").text
    parser = RegisterCells()
    parser.feed(page)
    assert parser.headings
    assert parser.labels == parser.headings * 2

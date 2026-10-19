"""Revoke, expiry and not-yet-valid grants: status on `/share/`, 403 on every grantee route."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from farmcore.apps.share import service
from farmcore.core.access import ensure_owner_grant
from farmcore.core.models.canonical import DataGrant, Event

GRANTEE_PATHS = ("", "register.json", "observations.json", "audit.json", "ngsi-ld.json")


def _grant(owner_repo, session, **kw) -> DataGrant:
    g = service.create_grant(
        owner_repo,
        grantee="Co-op adviser",
        preset="adviser_logs",
        purpose="spray advice",
        valid_to=datetime.now(UTC) + timedelta(days=30),
    )
    for k, v in kw.items():
        setattr(g, k, v)
    session.commit()
    return g


def _revoked_events(session) -> list[Event]:
    session.expire_all()
    return list(session.scalars(select(Event).where(Event.kind == "share.revoked")))


def test_revoke_grant_sets_revoked_at_once_and_posts_one_event(session, owner_repo) -> None:
    g = _grant(owner_repo, session)

    revoked = service.revoke_grant(owner_repo, g.id)
    first = revoked.revoked_at
    again = service.revoke_grant(owner_repo, g.id)

    assert first is not None and abs(datetime.now(UTC) - first) < timedelta(seconds=5)
    assert again.revoked_at == first
    assert service.grant_status(again, datetime.now(UTC)) == "revoked"
    [event] = _revoked_events(session)
    assert (event.res_model, event.res_id, event.actor, event.source_app) == (
        "data_grant",
        g.id,
        "owner",
        "share",
    )


def test_revoke_rejects_the_owner_grant_and_another_farms_grant(
    session, owner_repo, demo, other_farm
) -> None:
    owner = ensure_owner_grant(session, demo.farm)
    foreign = DataGrant(
        farm_id=other_farm.id, grantee="Other adviser", scopes=["logs:read"], purpose="p"
    )
    session.add(foreign)
    session.flush()

    for grant_id in (owner.id, foreign.id, 999999):
        with pytest.raises(LookupError):
            service.revoke_grant(owner_repo, grant_id)
    assert owner.revoked_at is None and foreign.revoked_at is None


def test_admin_revoke_then_the_next_grantee_request_is_a_revoked_403(
    admin, grantee_client, session, owner_repo, spray
) -> None:
    spray()
    g = _grant(owner_repo, session, recipient_identity_id=grantee_client.identity_id)
    assert grantee_client.get(f"/share/as/{g.id}/register.json").status_code == 200

    resp = admin.post(f"/share/grants/{g.id}/revoke", follow_redirects=False)

    assert resp.status_code == 303 and resp.headers["location"] == "/share/"
    page = admin.get("/share/").text
    assert f'data-grant="{g.id}" data-status="revoked"' in page
    assert f'href="/share/as/{g.id}/"' not in page
    html = grantee_client.get(f"/share/as/{g.id}/", headers={"Accept": "text/html"})
    assert html.status_code == 403
    assert "Shared access is unavailable" in html.text
    assert "Co-op adviser" not in html.text
    assert "spray advice" not in html.text
    assert "This access was revoked on" in grantee_client.get(f"/share/as/{g.id}/").json()["detail"]
    assert "BOUILLIE" not in html.text
    for path in GRANTEE_PATHS:
        assert grantee_client.get(f"/share/as/{g.id}/{path}").status_code == 403, path
    assert len(_revoked_events(session)) == 1
    assert admin.post(f"/share/grants/{g.id}/revoke", follow_redirects=False).status_code == 303
    assert len(_revoked_events(session)) == 1


def test_admin_revoke_of_owner_or_foreign_grant_is_404(admin, session, demo, other_farm) -> None:
    owner = ensure_owner_grant(session, demo.farm)
    foreign = DataGrant(
        farm_id=other_farm.id, grantee="Other adviser", scopes=["logs:read"], purpose="p"
    )
    session.add(foreign)
    session.commit()

    assert admin.post(f"/share/grants/{owner.id}/revoke").status_code == 404
    assert admin.post(f"/share/grants/{foreign.id}/revoke").status_code == 404
    session.expire_all()
    assert session.get(DataGrant, owner.id).revoked_at is None


@pytest.mark.parametrize(
    ("over", "status", "words"),
    [
        ({"valid_to": datetime.now(UTC) - timedelta(days=1)}, "expired", "This access expired on"),
        (
            {"valid_from": datetime.now(UTC) + timedelta(days=2)},
            "scheduled",
            "This access starts on",
        ),
    ],
)
def test_expired_and_scheduled_grants_are_403_with_the_right_status(
    admin, grantee_client, session, owner_repo, over, status, words
) -> None:
    g = _grant(owner_repo, session, recipient_identity_id=grantee_client.identity_id, **over)

    assert f'data-grant="{g.id}" data-status="{status}"' in admin.get("/share/").text
    html = grantee_client.get(f"/share/as/{g.id}/", headers={"Accept": "text/html"})
    assert html.status_code == 403 and "Shared access is unavailable" in html.text
    assert "Co-op adviser" not in html.text and "spray advice" not in html.text
    assert words in grantee_client.get(f"/share/as/{g.id}/").json()["detail"]
    for path in GRANTEE_PATHS:
        assert grantee_client.get(f"/share/as/{g.id}/{path}").status_code == 403, path

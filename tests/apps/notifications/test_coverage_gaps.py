"""Coverage for review items in the notification bell."""

from __future__ import annotations

from datetime import timedelta

from farmcore.apps.notifications import service
from farmcore.apps.review import recommendations
from farmcore.core.access import DataGrant, ScopedRepository, ensure_owner_grant
from tests.apps.planning.conftest import NOW

TZ = "Europe/Paris"


def _repo(session, farm):
    return ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "test", "test")


def test_review_items_are_hidden_without_advice_read(session, farm):
    grant = DataGrant(farm_id=farm.id, grantee="narrow", scopes=["logs:read"], purpose="x")
    session.add(grant)
    session.flush()
    repo = ScopedRepository(session, farm.id, grant, "test", "test")
    assert service.review_items(repo, NOW, TZ) == []


def test_a_response_older_than_the_window_is_not_shown(session, farm, parcels, advice):
    repo = _repo(session, farm)
    old = NOW - timedelta(days=10)
    state = recommendations.request_check(
        repo, old, advice, parcels[0], note="Old check", requested_by="owner"
    )
    recommendations.respond(
        repo,
        old + timedelta(hours=1),
        advice,
        parcels[0],
        state.request.id,
        opinion="agree",
        note="Old answer",
        reviewer="expert",
    )
    assert service.review_items(repo, NOW, TZ) == []

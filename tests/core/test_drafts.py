"""Drafts: every proposed write waits in one `draft` table until the farmer confirms it.

Failure model (one owning test each): another farm's or principal's draft (404), an agent's
draft reviewed by the owner who issued its token, retried and concurrent confirms (one record,
one stock delta), expired and stale-revision drafts (refused, with the reason), provenance kept on
the confirmed record, cancel after confirm, a payload naming another farm's parcel, a retried
propose.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from farmcore.core import drafts, registry
from farmcore.core.access import ensure_owner_grant
from farmcore.core.models.canonical import DataGrant, Farm, Log, LogSource, Parcel
from farmcore.core.models.drafts import Draft
from farmcore.core.models.identity import AccessToken
from farmcore.main import create_app
from tests.apps.chat.test_handlers import COPPER, NOW_LOCAL, SQUARE, seed_farm
from tests.core.test_member_sessions import _account, _login

MANIFESTS = registry.discover()
SHA = "ab" * 32


def _spray(ids, *, dose="2"):
    return {
        "parcel_ids": [ids["P1"]],
        "ts_start": NOW_LOCAL.replace(hour=5).isoformat(),
        "doses": [{"product_id": ids[COPPER], "value": dose, "unit": "kg/ha"}],
    }


def _propose(session, farm_id, ids, *, actor="operator", key="k1", **payload_kw):
    draft, _ = drafts.propose(
        session,
        MANIFESTS,
        farm_id=farm_id,
        actor=actor,
        kind="spray",
        payload=_spray(ids, **payload_kw),
        provenance={"source_type": "file", "file_name": "sprays.csv", "file_sha256": SHA},
        request_key=key,
    )
    return draft


def _farm_id(session, ids):
    return session.get(Parcel, ids["P1"]).farm_id


@pytest.fixture
def case(settings, committed_session_factory):
    """A committed demo farm, an operator browser, and 20 kg of copper bought."""
    from types import SimpleNamespace

    from farmcore.apps.inventory.models import InventoryStock

    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = _farm_id(session, ids)
        session.add(
            InventoryStock(
                farm_id=farm_id, product_id=ids[COPPER], season=2024, unit="kg", purchased=20
            )
        )
        session.commit()
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
        headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
    ) as browser:
        yield SimpleNamespace(browser=browser, ids=ids, farm_id=farm_id)


def _stock(c):
    page = c.browser.get(f"/inventory/?farm={c.farm_id}")
    assert page.status_code == 200
    card = re.search(rf'<article data-stock="{c.ids[COPPER]}".*?</article>', page.text, re.S)[0]
    return re.search(r">(\d+)</span>", card)[1]


def test_concurrent_and_retried_confirms_record_once(case, committed_session_factory):
    """Eight real sessions confirm at once behind a barrier, then the farmer's page retries."""
    workers = 8
    with committed_session_factory() as session:
        draft = _propose(session, case.farm_id, case.ids)
        session.commit()
    assert _stock(case) == "20"
    barrier = threading.Barrier(workers)

    def confirm(_):
        with committed_session_factory() as session:
            barrier.wait()
            _, recorded = drafts.confirm(
                session,
                MANIFESTS,
                farm_id=case.farm_id,
                draft_id=draft.id,
                actor="operator",
                revision=1,
                now=NOW_LOCAL,
            )
            session.commit()
            return recorded

    with ThreadPoolExecutor(max_workers=workers) as pool:
        recorded = list(pool.map(confirm, range(workers)))
    retried = case.browser.post(
        f"/drafts/{draft.id}/confirm?farm={case.farm_id}", data={"revision": "1"}
    )
    assert (recorded.count(True), retried.status_code) == (1, 200)
    assert _stock(case) == "18"  # 20 kg - 2 kg/ha x 1 ha, once
    with committed_session_factory() as session:
        row = session.get(Draft, draft.id)
        assert (row.status, row.result_model) == ("confirmed", "log")
        assert session.scalar(select(func.count(Log.id)).where(Log.status == "done")) == 1
        assert session.get(Log, row.result_id).status == "done"


def test_a_stale_revision_cannot_be_confirmed(case, committed_session_factory):
    with committed_session_factory() as session:
        draft = _propose(session, case.farm_id, case.ids)
        drafts.revise(
            session,
            MANIFESTS,
            farm_id=case.farm_id,
            draft_id=draft.id,
            actor="operator",
            payload=_spray(case.ids, dose="1"),
            revision=1,
        )
        session.commit()
    reply = case.browser.post(
        f"/drafts/{draft.id}/confirm?farm={case.farm_id}", data={"revision": "1"}
    )
    assert reply.status_code == 409
    assert "changed since you opened it" in reply.text
    with committed_session_factory() as session:
        assert session.get(Draft, draft.id).status == "open"
        assert session.scalar(select(func.count(Log.id))) == 0


def test_an_expired_draft_cannot_be_confirmed_and_the_job_expires_only_due_drafts(
    case, committed_session_factory, settings
):
    from farmcore.core.manifest import JobContext

    with committed_session_factory() as session:
        due = _propose(session, case.farm_id, case.ids, key="due")
        live = _propose(session, case.farm_id, case.ids, key="live")
        # Expiry is wall time, never the replay clock (a restored copy refuses the same way).
        session.execute(
            update(Draft)
            .where(Draft.id == due.id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        session.commit()
    reply = case.browser.post(
        f"/drafts/{due.id}/confirm?farm={case.farm_id}", data={"revision": "1"}
    )
    assert reply.status_code == 409
    assert "expired" in reply.text
    with committed_session_factory() as session:
        assert session.scalar(select(func.count(Log.id))) == 0
        drafts.expire_job(JobContext(session=session, now=NOW_LOCAL, settings=settings))
        session.commit()
        assert session.get(Draft, due.id).status == "expired"
        assert session.get(Draft, live.id).status == "open"


def test_another_farms_or_principals_draft_is_a_404(settings, committed_session_factory):
    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = _farm_id(session, ids)
        other = Farm(name="Neighbour")
        session.add(other)
        session.flush()
        ensure_owner_grant(session, other)  # a selectable farm of its own
        session.commit()
        other_id = other.id
    owner_id, _ = _account(committed_session_factory, login="owner", farm_id=farm_id)
    _account(committed_session_factory, login="partner", farm_id=farm_id)
    with committed_session_factory() as session:
        mine = _propose(session, farm_id, ids, actor=f"identity:{owner_id}")
        session.commit()
    paths = (
        f"/drafts/{mine.id}",
        f"/operations/drafts/review?draft={mine.id}",
    )
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser, login="owner").status_code == 303
        page = browser.get(f"/drafts/{mine.id}?farm={farm_id}")
        assert page.status_code == 200
        assert f"/operations/drafts/review?draft={mine.id}" in page.text
        assert "P1" in browser.get(f"{paths[1]}&farm={farm_id}").text
        browser.cookies.clear()
        assert _login(browser, login="partner").status_code == 303
        browser.headers.update(
            {"Origin": "https://testserver", "X-CSRF-Token": browser.cookies["farmcore_csrf"]}
        )
        for path in paths:
            sep = "&" if "?" in path else "?"
            assert browser.get(f"{path}{sep}farm={farm_id}").status_code == 404
        confirm = browser.post(f"/drafts/{mine.id}/confirm?farm={farm_id}", data={"revision": 1})
        assert confirm.status_code == 404
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
        headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
    ) as operator:
        with committed_session_factory() as session:
            theirs = _propose(session, farm_id, ids, key="operator")
            session.commit()
        assert operator.get(f"/drafts/{theirs.id}?farm={farm_id}").status_code == 200
        assert operator.get(f"/drafts/{theirs.id}?farm={other_id}").status_code == 404
        reply = operator.post(f"/drafts/{theirs.id}/confirm?farm={other_id}", data={"revision": 1})
        assert reply.status_code == 404
    with committed_session_factory() as session:
        assert session.scalar(select(func.count(Log.id))) == 0


def _agent_token(session, farm_id):
    """An owner-issued agent token (`token:<id>`) and the ids of its issuer and another person."""
    from farmcore.core.models.identity import LocalIdentity

    issuer, other = (LocalIdentity(login=n, password_hash="x") for n in ("issuer", "other"))
    grant = DataGrant(farm_id=farm_id, grantee="agent", scopes=["drafts:propose"], purpose="mcp")
    session.add_all([issuer, other, grant])
    session.flush()
    token = AccessToken(
        token_hash="c" * 64, kind="personal", grant_id=grant.id, created_by_identity_id=issuer.id
    )
    session.add(token)
    session.flush()
    return f"token:{token.id}", issuer.id, other.id


def test_a_token_draft_is_reviewed_by_the_owner_who_issued_the_token(session, settings):
    ids = seed_farm(session, settings)
    farm_id = _farm_id(session, ids)
    agent, issuer, other = _agent_token(session, farm_id)
    draft = _propose(session, farm_id, ids, actor=agent)

    def opens(actor):
        try:
            drafts.get(session, farm_id=farm_id, draft_id=draft.id, actor=actor)
        except drafts.DraftNotFound:
            return False
        return True

    def confirms(actor):
        try:
            drafts.confirm(
                session,
                MANIFESTS,
                farm_id=farm_id,
                draft_id=draft.id,
                actor=actor,
                revision=1,
                now=NOW_LOCAL,
            )
        except (drafts.DraftNotFound, drafts.DraftForbidden):
            return False
        return True

    # The agent reads its own draft but never confirms it; only its issuer does.
    actors = (agent, f"identity:{other}", "operator", f"identity:{issuer}")
    assert [(opens(a), confirms(a)) for a in actors] == [
        (True, False),
        (False, False),
        (False, False),
        (True, True),
    ]
    # A token whose grant is on another farm never stamps its issuer as a reviewer here.
    neighbour = Farm(name="Neighbour")
    session.add(neighbour)
    session.flush()
    their_parcel = Parcel(farm_id=neighbour.id, alias="N1", name="N", area_ha=1, geom=SQUARE)
    session.add(their_parcel)
    session.flush()
    with pytest.raises(drafts.DraftInvalid):  # a valid payload there: only the token is refused
        _propose(session, neighbour.id, {**ids, "P1": their_parcel.id}, actor=agent, key="n")


def test_confirm_keeps_draft_provenance_on_the_record(session, settings):
    ids = seed_farm(session, settings)
    farm_id = _farm_id(session, ids)
    agent, issuer, _ = _agent_token(session, farm_id)
    draft, _ = drafts.propose(
        session,
        MANIFESTS,
        farm_id=farm_id,
        actor=agent,
        kind="spray",
        payload=_spray(ids),
        provenance={
            "source_type": "file",
            "file_name": "telepac.zip",
            "file_sha256": SHA,
            "registry_ref": "RPG:2024:123",
        },
        request_key="p",
    )
    drafts.confirm(
        session,
        MANIFESTS,
        farm_id=farm_id,
        draft_id=draft.id,
        actor=f"identity:{issuer}",
        revision=1,
        now=NOW_LOCAL,
    )
    [source] = session.scalars(select(LogSource).where(LogSource.log_id == draft.result_id)).all()
    assert source.source_type == "file"
    assert {
        k: source.raw_payload[k]
        for k in ("draft_id", "proposed_by", "confirmed_by", "file_name", "file_sha256")
    } == {
        "draft_id": draft.id,
        "proposed_by": agent,
        "confirmed_by": f"identity:{issuer}",
        "file_name": "telepac.zip",
        "file_sha256": SHA,
    }
    assert source.raw_payload["registry_ref"] == "RPG:2024:123"


def test_cancel_is_idempotent_and_final(session, settings):
    ids = seed_farm(session, settings)
    farm_id = _farm_id(session, ids)
    kw = {"farm_id": farm_id, "actor": "operator"}
    cancelled = _propose(session, farm_id, ids, key="c")
    drafts.cancel(session, draft_id=cancelled.id, **kw)
    drafts.cancel(session, draft_id=cancelled.id, **kw)
    with pytest.raises(drafts.DraftClosed):
        drafts.confirm(session, MANIFESTS, draft_id=cancelled.id, revision=1, now=NOW_LOCAL, **kw)
    confirmed = _propose(session, farm_id, ids, key="d")
    drafts.confirm(session, MANIFESTS, draft_id=confirmed.id, revision=1, now=NOW_LOCAL, **kw)
    with pytest.raises(drafts.DraftClosed):
        drafts.cancel(session, draft_id=confirmed.id, **kw)
    assert session.scalar(select(func.count(Log.id))) == 1


def test_a_draft_naming_another_farms_parcel_is_refused(session, settings):
    ids = seed_farm(session, settings)
    other = Farm(name="Neighbour")
    session.add(other)
    session.flush()
    with pytest.raises(drafts.DraftInvalid):
        _propose(session, other.id, ids)
    assert session.scalar(select(func.count(Draft.id))) == 0


def test_proposing_twice_with_one_key_returns_one_draft(session, settings):
    ids = seed_farm(session, settings)
    farm_id = _farm_id(session, ids)
    first = _propose(session, farm_id, ids)
    assert _propose(session, farm_id, ids).id == first.id
    with pytest.raises(drafts.DraftConflict):
        _propose(session, farm_id, ids, dose="3")


@pytest.mark.parametrize(
    ("reviewer", "allowed"),
    [("owner", True), ("advisor", False), ("revoked owner", False), ("other farm owner", False)],
)
def test_a_chat_draft_is_reviewed_by_an_active_owner_of_its_farm(
    session_factory, session, settings, reviewer, allowed
):
    """A Telegram chat is linked to its farm, not to a person: the farm's owners review its
    draft on the web, checked live (a revoked owner loses it), and nobody else."""
    from farmcore.core.models.identity import FarmMembership

    ids = seed_farm(session, settings)
    farm_id = _farm_id(session, ids)
    draft = _propose(session, farm_id, ids, actor="chat:42")
    session.commit()
    role = "advisor" if reviewer == "advisor" else "owner"
    identity_id, _ = _account(
        session_factory,
        login="member",
        role=role,
        farm_id=None if reviewer == "other farm owner" else farm_id,
    )
    if reviewer == "revoked owner":
        session.scalar(
            select(FarmMembership).where(FarmMembership.identity_id == identity_id)
        ).revoked_at = datetime.now(UTC)
        session.flush()
    confirm = dict(farm_id=farm_id, draft_id=draft.id, actor=f"identity:{identity_id}", revision=1)

    if allowed:
        confirmed, recorded = drafts.confirm(session, MANIFESTS, **confirm, now=NOW_LOCAL)
        assert (confirmed.status, recorded) == ("confirmed", True)
    else:
        with pytest.raises(drafts.DraftNotFound):
            drafts.confirm(session, MANIFESTS, **confirm, now=NOW_LOCAL)

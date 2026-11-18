"""Personal access tokens: an owner issues a scoped, expiring token for an agent; it is a grant.

Failure model (one owning test each; restore replay is owned by tests/instances/test_restore.py):
- the token leaks: shown more than once, stored in clear, written to logs, events or audit;
- a forged or mangled token authenticates, or the refusal echoes what was sent;
- a token holds a scope its creator lacks (`*`, audit, writes), or a non-owner issues one;
- a token reads another farm;
- a revoked or expired token, or one whose creator lost ownership, still works on the next call.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from farmcore.apps.share import service
from farmcore.core.access import (
    ScopedRepository,
    TokenRejected,
    authenticate_token,
    ensure_owner_grant,
)
from farmcore.core.identity import hash_password
from farmcore.core.models.canonical import AccessLog, DataGrant, Parcel
from farmcore.core.models.identity import AccessToken, FarmMembership, LocalIdentity
from farmcore.main import create_app

PASSWORD = "synthetic-token-owner-password"  # gauntlet:allow-secret synthetic credential
SHOWN = re.compile(r"fc_pat_[A-Za-z0-9_-]{43}")


@pytest.fixture(scope="module")
def credential_hash():
    return hash_password(PASSWORD)


def _member(session, farm, login, credential_hash, role="owner") -> LocalIdentity:
    identity = LocalIdentity(login=login, password_hash=credential_hash)
    session.add(identity)
    session.flush()
    session.add(FarmMembership(identity_id=identity.id, farm_id=farm.id, role=role))
    session.commit()
    return identity


@pytest.fixture
def owner(session, demo, credential_hash) -> LocalIdentity:
    return _member(session, demo.farm, "token-owner", credential_hash)


@pytest.fixture
def owner_browser(settings, session_factory, owner):
    with TestClient(
        create_app(settings, session_factory=session_factory), base_url="https://testserver"
    ) as browser:
        page = browser.get("/login")
        nonce = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        signed = browser.post(
            "/login",
            data={"login": owner.login, "password": PASSWORD, "csrf_token": nonce},
            headers={"Origin": "https://testserver"},
            follow_redirects=False,
        )
        assert signed.status_code == 303
        yield browser


def _post(browser, path, data):
    return browser.post(
        path,
        data={**data, "csrf_token": browser.cookies.get("farmcore_csrf", "")},
        headers={"Origin": str(browser.base_url).rstrip("/")},
        follow_redirects=False,
    )


def _form(**over):
    until = (datetime.now(UTC) + timedelta(days=30)).date().isoformat()
    return {
        "name": "Claude Desktop",
        "scope": ["register:read", "drafts:propose"],
        "purpose": "my own agent",
        "valid_to": until,
        **over,
    }


def _issue(session, farm, creator, *, scopes=("register:read",), days=30):
    repo = ScopedRepository(
        session, farm.id, ensure_owner_grant(session, farm), f"user:{creator.id}", "test"
    )
    grant, raw = service.create_token(
        repo,
        creator_id=creator.id,
        name="Claude Desktop",
        scopes=list(scopes),
        purpose="my own agent",
        valid_to=datetime.now(UTC) + timedelta(days=days),
    )
    session.commit()
    return grant, raw


def test_a_token_is_shown_once_stored_hashed_and_never_logged(
    owner_browser, session, demo, caplog
) -> None:
    caplog.set_level(logging.DEBUG)

    created = _post(owner_browser, f"/share/tokens?farm={demo.farm.id}", _form())

    assert created.status_code == 201
    assert created.headers["Cache-Control"] == "no-store"
    [raw] = set(SHOWN.findall(created.text))
    session.expire_all()
    [token] = session.scalars(select(AccessToken)).all()
    assert token.token_hash == hashlib.sha256(raw.encode()).hexdigest()
    grant = session.get(DataGrant, token.grant_id)
    assert (grant.farm_id, grant.grantee) == (demo.farm.id, "Claude Desktop")
    assert set(grant.scopes) == {"register:read", "drafts:propose"}
    # The working token is the one shown, and nothing keeps or prints it afterwards.
    authenticate_token(session, raw).list(Parcel, scope="register:read")
    session.commit()
    assert raw not in owner_browser.get(f"/share/?farm={demo.farm.id}").text
    for table in ("access_token", "data_grant", "event", "access_log"):
        dumped = session.execute(text(f"SELECT to_jsonb(t)::text FROM {table} t")).scalars()
        assert not any(raw in row or raw[7:] in row for row in dumped), table
    assert raw[7:] not in caplog.text


def test_sharing_lists_the_token_with_last_use_and_reads_and_revoke_ends_the_next_call(
    owner_browser, session, demo, owner
) -> None:
    grant, raw = _issue(session, demo.farm, owner)
    authenticate_token(session, raw).list(Parcel, scope="register:read")
    session.commit()

    page = owner_browser.get(f"/share/?farm={demo.farm.id}").text
    row = page.split(f'data-grant="{grant.id}"', 1)[1].split("</tr>", 1)[0]
    assert "Agent token" in row and "Last used" in row and "never" not in row
    assert "Claude Desktop (agent token)" in page.split('id="access-log"', 1)[1]

    revoked = _post(owner_browser, f"/share/grants/{grant.id}/revoke?farm={demo.farm.id}", {})

    assert revoked.status_code == 303
    with pytest.raises(TokenRejected):
        authenticate_token(session, raw)


def _expire(session, grant, creator):
    grant.valid_to = datetime.now(UTC) - timedelta(seconds=1)


def _demote(session, grant, creator):
    m = session.scalar(select(FarmMembership).where(FarmMembership.identity_id == creator.id))
    m.role = "advisor"


def _remove(session, grant, creator):
    m = session.scalar(select(FarmMembership).where(FarmMembership.identity_id == creator.id))
    m.revoked_at = datetime.now(UTC)


def _disable(session, grant, creator):
    creator.disabled_at = datetime.now(UTC)


@pytest.mark.parametrize("change", [_expire, _demote, _remove, _disable])
def test_the_next_call_fails_once_the_token_or_its_creator_loses_authority(
    session, demo, owner, change
) -> None:
    grant, raw = _issue(session, demo.farm, owner)
    authenticate_token(session, raw)
    session.commit()

    change(session, grant, owner)
    session.commit()

    with pytest.raises(TokenRejected):
        authenticate_token(session, raw)


@pytest.mark.parametrize(
    ("scopes", "role"),
    [
        (["*"], "owner"),
        (["audit:read"], "owner"),
        (["logs:write"], "owner"),
        ([], "owner"),
        (["register:read"], "advisor"),
    ],
)
def test_a_token_never_holds_a_scope_its_creator_lacks(
    session, demo, credential_hash, scopes, role
) -> None:
    creator = _member(session, demo.farm, "token-creator", credential_hash, role=role)

    with pytest.raises((service.InvalidGrant, PermissionError)):
        _issue(session, demo.farm, creator, scopes=scopes)

    session.rollback()
    assert session.scalars(select(AccessToken)).all() == []


def test_the_installation_operator_cannot_issue_a_token(admin, session, demo) -> None:
    response = admin.post(f"/share/tokens?farm={demo.farm.id}", data=_form())

    assert response.status_code == 403
    assert session.scalars(select(AccessToken)).all() == []


def test_a_token_reads_only_its_own_farm(session, demo, owner, other_farm) -> None:
    _grant, raw = _issue(session, demo.farm, owner)

    with pytest.raises(TokenRejected):
        authenticate_token(session, raw, farm_id=other_farm.id)
    repo = authenticate_token(session, raw, farm_id=demo.farm.id)
    farms = {p.farm_id for p in repo.list(Parcel, scope="register:read")}
    assert farms == {demo.farm.id}
    assert (
        session.scalars(select(AccessLog.actor).where(AccessLog.farm_id == other_farm.id)).all()
        == []
    )


def _other_char(c: str) -> str:
    return "A" if c != "A" else "B"


@pytest.mark.parametrize(
    "forge",
    [
        lambda raw: "fc_pt_" + raw[7:],  # wrong prefix
        lambda raw: raw[:20] + _other_char(raw[20]) + raw[21:],  # one character changed
        lambda raw: raw[:-1],  # truncated
        lambda raw: raw + "A",  # suffix appended
    ],
    ids=["wrong-prefix", "one-char-changed", "truncated", "suffix-appended"],
)
def test_forged_or_mangled_tokens_never_authenticate_and_refusals_say_nothing(
    session, demo, owner, forge
) -> None:
    _grant, raw = _issue(session, demo.farm, owner)

    with pytest.raises(TokenRejected) as refused:
        authenticate_token(session, forge(raw))

    # One refusal for every cause: nothing echoed, nothing that tells causes apart.
    assert str(refused.value) == str(TokenRejected())

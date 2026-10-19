"""Private grants require explicit recipient identity; real HTTP and migrated PostGIS."""

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from farmcore.core.identity import hash_password
from farmcore.core.models.canonical import AccessLog, DataGrant
from farmcore.core.models.identity import LocalIdentity
from farmcore.main import create_app

PASSWORD = "synthetic-sharing-test-password"  # gauntlet:allow-secret synthetic credential


@pytest.fixture(scope="module")
def credential_hash():
    return hash_password(PASSWORD)


@pytest.fixture
def recipient(session, credential_hash):
    identity = LocalIdentity(login="recipient", password_hash=credential_hash)
    session.add(identity)
    session.commit()
    return identity


def sign_in(browser, login="recipient"):
    page = browser.get("/login")
    nonce = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    return browser.post(
        "/login",
        data={"login": login, "password": PASSWORD, "csrf_token": nonce},
        headers={"Origin": "https://testserver"},
        follow_redirects=False,
    )


@pytest.fixture
def recipient_browser(settings, session_factory, recipient):
    with TestClient(
        create_app(settings, session_factory=session_factory), base_url="https://testserver"
    ) as browser:
        assert sign_in(browser).status_code == 303
        yield browser


@pytest.fixture
def bound_grant(session, demo, recipient):
    grant = DataGrant(
        farm_id=demo.farm.id,
        grantee="Named recipient",
        recipient_identity_id=recipient.id,
        scopes=["logs:read", "register:read"],
        purpose="Independent recipient review",
        valid_to=datetime.now(UTC) + timedelta(days=1),
    )
    session.add(grant)
    session.commit()
    return grant


def test_bound_recipient_without_memberships_reads_grant_with_real_actor(
    recipient_browser, session, bound_grant, spray, demo, recipient
):
    log = spray()
    response = recipient_browser.get(f"/share/as/{bound_grant.id}/register.json")
    assert response.status_code == 200
    assert {row["log_id"] for row in response.json()} == {log.id}
    reads = session.scalars(select(AccessLog).where(AccessLog.grant_id == bound_grant.id)).all()
    assert reads
    assert {(r.actor, r.farm_id, r.purpose) for r in reads} == {
        (f"user:{recipient.id}", demo.farm.id, "Independent recipient review")
    }


@pytest.fixture
def owner(session, demo, credential_hash):
    from farmcore.core.models.identity import FarmMembership

    identity = LocalIdentity(login="farm-owner", password_hash=credential_hash)
    session.add(identity)
    session.flush()
    session.add(FarmMembership(identity_id=identity.id, farm_id=demo.farm.id, role="owner"))
    session.commit()
    return identity


@pytest.fixture
def owner_browser(settings, session_factory, owner):
    with TestClient(
        create_app(settings, session_factory=session_factory), base_url="https://testserver"
    ) as browser:
        assert sign_in(browser, "farm-owner").status_code == 303
        yield browser


def mutation(browser, path, **data):
    return browser.post(
        path,
        data={**data, "csrf_token": browser.cookies.get("farmcore_csrf")},
        headers={"Origin": "https://testserver"},
        follow_redirects=False,
    )


def test_owner_browser_binds_existing_login_and_revokes_next_recipient_read(
    owner_browser, recipient_browser, recipient, owner, demo, session
):
    from farmcore.core.models.canonical import Event

    page = owner_browser.get(f"/share/?farm={demo.farm.id}")
    assert page.status_code == 200
    assert 'name="recipient_login"' in page.text
    response = mutation(
        owner_browser,
        f"/share/grants?farm={demo.farm.id}",
        recipient_login="recipient",
        grantee="Explicit recipient",
        preset="adviser_logs",
        purpose="Bound consent",
        valid_to=(datetime.now(UTC) + timedelta(days=2)).date().isoformat(),
    )
    assert response.status_code == 303
    grant = session.scalar(select(DataGrant).where(DataGrant.purpose == "Bound consent"))
    assert grant.recipient_identity_id == recipient.id
    assert grant.farm_id == demo.farm.id
    event = session.scalar(select(Event).where(Event.kind == "share.granted"))
    assert event.actor == f"user:{owner.id}"
    assert recipient_browser.get(f"/share/as/{grant.id}/register.json").status_code == 200
    assert (
        mutation(owner_browser, f"/share/grants/{grant.id}/revoke?farm={demo.farm.id}").status_code
        == 303
    )
    denied = recipient_browser.get(f"/share/as/{grant.id}/register.json")
    assert denied.status_code == 403
    assert "revoked" in denied.text


def test_recipient_login_lands_on_own_shared_access_without_farm_shell(
    recipient_browser, bound_grant, session, recipient, demo, settings, session_factory
):
    with TestClient(
        create_app(settings, session_factory=session_factory), base_url="https://testserver"
    ) as browser:
        response = sign_in(browser)
        assert response.headers["location"] == "/share/received"
        landing = browser.get(response.headers["location"])
        assert landing.status_code == 200
        assert f'href="/share/as/{bound_grant.id}/"' in landing.text
        assert 'action="/logout"' in landing.text
        assert 'name="csrf_token"' in landing.text
        assert "/farms/switcher" not in landing.text
        assert "/notifications" not in landing.text
        assert demo.farm.name not in landing.text
        page = browser.get(f"/share/as/{bound_grant.id}/")
        assert page.status_code == 200
        assert "/farms/switcher" not in page.text
        assert "/notifications" not in page.text
        assert "recipient" in page.text
        assert mutation(browser, "/logout").status_code == 303
        assert browser.get(f"/share/as/{bound_grant.id}/register.json").status_code == 403


def test_owner_previews_own_grant_with_scope_and_real_actor_but_foreign_owner_cannot(
    owner_browser, session, bound_grant, owner, demo, other_farm
):
    response = owner_browser.get(f"/share/as/{bound_grant.id}/register.json")
    assert response.status_code == 200
    assert owner_browser.get(f"/share/as/{bound_grant.id}/observations.json").status_code == 403
    foreign = DataGrant(
        farm_id=other_farm.id,
        recipient_identity_id=None,
        grantee="Unrelated recipient",
        scopes=["register:read"],
        purpose="Foreign private grant",
    )
    session.add(foreign)
    session.commit()
    assert owner_browser.get(f"/share/as/{foreign.id}/register.json").status_code == 404
    reads = session.scalars(select(AccessLog).where(AccessLog.grant_id == bound_grant.id)).all()
    assert reads and {r.actor for r in reads} == {f"user:{owner.id}"}


@pytest.mark.parametrize("demo_enabled", [False, True])
def test_numeric_urls_never_authorize_anonymous_even_public_demo(
    settings, session_factory, session, bound_grant, demo, demo_enabled
):
    with TestClient(
        create_app(
            settings.model_copy(
                update={"demo_mode": demo_enabled, "demo_farm_ids": [demo.farm.id]}
            ),
            session_factory=session_factory,
        ),
        base_url="https://testserver",
    ) as browser:
        for suffix in ("", "register.json", "observations.json", "ngsi-ld.json", "audit.json"):
            response = browser.get(f"/share/as/{bound_grant.id}/{suffix}")
            assert response.status_code in {403, 404}
            assert "Named recipient" not in response.text
            assert "Independent recipient review" not in response.text
        assert browser.get("/share/received").status_code == 403
    assert session.scalar(select(AccessLog.id).where(AccessLog.grant_id == bound_grant.id)) is None


@pytest.mark.parametrize("change", ["revoked", "expired", "scheduled", "disabled", "session"])
def test_next_request_rechecks_wall_validity_and_identity(
    recipient_browser, session, bound_grant, recipient, change
):
    from farmcore.core.models.identity import LocalSession

    url = f"/share/as/{bound_grant.id}/register.json"
    assert recipient_browser.get(url).status_code == 200
    last = session.scalar(select(AccessLog.id).order_by(AccessLog.id.desc()).limit(1))
    if change == "revoked":
        bound_grant.revoked_at = datetime.now(UTC)
    elif change == "expired":
        bound_grant.valid_to = datetime.now(UTC) - timedelta(seconds=1)
    elif change == "scheduled":
        bound_grant.valid_from = datetime.now(UTC) + timedelta(days=2)
    elif change == "disabled":
        recipient.disabled_at = datetime.now(UTC)
    else:
        session.scalar(select(LocalSession)).revoked_at = datetime.now(UTC)
    session.commit()
    response = recipient_browser.get(url)
    assert response.status_code == 403
    assert "log_id" not in response.text
    assert session.scalar(select(AccessLog.id).where(AccessLog.id > last)) is None


def test_recipient_cannot_use_foreign_unbound_grants_or_standard_farm_surfaces(
    recipient_browser, session, bound_grant, demo, other_farm
):
    unbound = DataGrant(
        farm_id=demo.farm.id, grantee="recipient", scopes=["register:read"], purpose="Legacy name"
    )
    session.add(unbound)
    session.commit()
    for gid in (unbound.id, 2**100, 2**63, -(2**63) - 1, 999999):
        assert recipient_browser.get(f"/share/as/{gid}/register.json").status_code == 404
    for url in (
        "/",
        "/share/",
        "/farms/switcher",
        "/fields/",
        "/operations/register.json",
        "/assistant/ask?q=show+records",
        "/chat/",
        "/assistant/voice",
        "/apps.json",
        "/share/received/extra",
        "/share/as/1/unknown",
        "/share/as/1/audit.json/extra",
    ):
        response = recipient_browser.get(url)
        assert response.status_code == 403, url
        assert demo.farm.name not in response.text
    # A grant context is explicit and cannot become the farm-cookie/query context.
    recipient_browser.cookies.set("farmcore_farm", str(other_farm.id))
    assert (
        recipient_browser.get(
            f"/share/as/{bound_grant.id}/register.json?farm={other_farm.id}"
        ).status_code
        == 200
    )
    assert recipient_browser.get("/share/received?farm=999999").status_code == 200


@pytest.mark.parametrize("role", ["advisor", "foreign_owner"])
def test_advisor_and_foreign_owner_cannot_administer_grants(
    settings, session_factory, session, credential_hash, demo, other_farm, role, bound_grant
):
    from farmcore.core.models.identity import FarmMembership

    user = LocalIdentity(login="unprivileged", password_hash=credential_hash)
    session.add(user)
    session.flush()
    session.add(
        FarmMembership(
            identity_id=user.id,
            farm_id=demo.farm.id if role == "advisor" else other_farm.id,
            role="advisor" if role == "advisor" else "owner",
        )
    )
    session.commit()
    with TestClient(
        create_app(settings, session_factory=session_factory), base_url="https://testserver"
    ) as browser:
        assert sign_in(browser, "unprivileged").status_code == 303
        assert browser.get(f"/share/?farm={demo.farm.id}").status_code == 403
        assert mutation(browser, f"/share/grants?farm={demo.farm.id}").status_code == 403
        assert (
            mutation(
                browser, f"/share/grants/{bound_grant.id}/revoke?farm={demo.farm.id}"
            ).status_code
            == 403
        )
        assert browser.get(f"/share/as/{bound_grant.id}/register.json").status_code == 404
    session.refresh(bound_grant)
    assert bound_grant.revoked_at is None


def test_advice_only_grant_exports_stored_whitelist_without_weather_or_private_provenance(
    recipient_browser, bound_grant, session, demo, other_farm
):
    from farmcore.core.models.canonical import Advice, Parcel
    from tests.apps.share.synth import NOW

    secret = "private-parameter-inputs-and-reviewer-notes"
    bound_grant.scopes = ["advice:read"]
    parcel = session.scalar(select(Parcel).where(Parcel.farm_id == demo.farm.id))
    row = Advice(
        farm_id=demo.farm.id,
        parcel_id=parcel.id,
        evaluated_at=NOW,
        decision="wait",
        props={
            "headline": "Stored evaluation",
            "flags": ["limited"],
            "mm_since_spray": 2.5,
            "incubation_pct": 12.0,
            "passes": 1,
            "treatments": 2,
            "assessment_status": "limited",
            "config_version": {
                "id": 7,
                "hash": "abc123",
                "scope": "farm",
                "status": "active",
                "accuracy_claim": True,
                "reviewer": secret,
                "input_snapshot": secret,
                "review": {
                    "id": 8,
                    "verdict": "approved",
                    "status": "reviewed",
                    "notes": secret,
                    "reviewer_identity": secret,
                },
            },
            "input_snapshot": {"weather": secret},
            "effective_config": {"secret": secret},
            "review_notes": secret,
        },
    )
    session.add(row)
    session.commit()
    response = recipient_browser.get(f"/share/as/{bound_grant.id}/advice.json")
    assert response.status_code == 200
    [data] = response.json()
    assert data["headline"] == "Stored evaluation"
    assert data["flags"] == ["limited"]
    assert data["config_version"] == {
        "id": 7,
        "hash": "abc123",
        "scope": "farm",
        "status": "active",
        "accuracy_claim": False,
        "review": {"id": 8, "verdict": "approved", "status": "reviewed"},
    }
    assert data["assessment_status"] == "limited"
    assert data["current_inputs_status"] == "not_authorized"
    assert secret not in response.text
    assert "props" not in data and "input_snapshot" not in data and "effective_config" not in data
    reads = session.scalars(select(AccessLog).where(AccessLog.grant_id == bound_grant.id)).all()
    assert [(r.scope, r.resource) for r in reads] == [("advice:read", "advice")]
    assert recipient_browser.get(f"/share/as/{bound_grant.id}/observations.json").status_code == 403
    assert recipient_browser.get(f"/share/as/{bound_grant.id}/register.json").status_code == 403
    assert recipient_browser.get(f"/share/as/{bound_grant.id}/audit.json").status_code == 403


def test_owner_can_choose_advice_only_preset_and_recipient_opens_it(
    owner_browser, recipient_browser, recipient, demo, session
):
    response = mutation(
        owner_browser,
        f"/share/grants?farm={demo.farm.id}",
        recipient_login=recipient.login,
        grantee="Advice reviewer",
        preset="advice",
        purpose="Advice consent only",
        valid_to=(datetime.now(UTC) + timedelta(days=2)).date().isoformat(),
    )
    assert response.status_code == 303
    grant = session.scalar(select(DataGrant).where(DataGrant.purpose == "Advice consent only"))
    assert grant.scopes == ["advice:read"]
    page = recipient_browser.get(f"/share/as/{grant.id}/")
    assert page.status_code == 200
    assert f'href="/share/as/{grant.id}/advice.json"' in page.text
    assert "Current inputs are not authorized" in page.text
    assert "Not shared: this grant does not include the spray register" in page.text


def test_scoped_grant_repository_denies_unlisted_app_resources_and_private_audit(
    session, bound_grant
):
    from farmcore.apps.share.service import grant_repo
    from farmcore.core.access import ScopeDenied
    from farmcore.core.models.canonical import Advice, LlmAudit
    from farmcore.core.models.kernel import AppModule

    bound_grant.scopes = ["advice:read"]
    repo = grant_repo(session, bound_grant, actor="user:123")
    assert repo.list(Advice, scope="advice:read") == []
    with pytest.raises(ScopeDenied):
        repo.execute(select(AppModule), scope="advice:read", resource="app_module")
    bound_grant.scopes = ["advice:read", "audit:read", "logs:write"]
    with pytest.raises(ScopeDenied):
        repo.list(Advice, scope="advice:read")
    with pytest.raises(ScopeDenied):
        repo.execute(select(LlmAudit), scope="audit:read", resource="llm_audit")


def test_owner_shell_exposes_sharing_navigation(owner_browser, demo):
    from html.parser import HTMLParser
    from urllib.parse import urlsplit

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.path = None
            self.labels = []

        def handle_starttag(self, tag, attrs):
            if tag == "a":
                self.path = urlsplit(dict(attrs).get("href", "")).path

        def handle_endtag(self, tag):
            if tag == "a":
                self.path = None

        def handle_data(self, data):
            if self.path == "/share/":
                self.labels.append(data.strip())

    page = owner_browser.get("/share/")
    parser = Links()
    parser.feed(page.text)
    assert "Sharing" in parser.labels


def test_grant_forms_bind_resolved_farm_even_without_query_selector(
    owner_browser, demo, bound_grant
):
    from html.parser import HTMLParser
    from urllib.parse import parse_qs, urlsplit

    class Forms(HTMLParser):
        def __init__(self):
            super().__init__()
            self.actions = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "form" and attrs.get("method") == "post":
                self.actions.append(urlsplit(attrs.get("action", "")))

    page = owner_browser.get("/share/")
    parser = Forms()
    parser.feed(page.text)
    sharing = [a for a in parser.actions if a.path.startswith("/share/grants")]
    assert {a.path for a in sharing} == {"/share/grants", f"/share/grants/{bound_grant.id}/revoke"}
    assert all(parse_qs(a.query) == {"farm": [str(demo.farm.id)]} for a in sharing)


@pytest.mark.parametrize("status", ["revoked", "expired", "scheduled"])
def test_recipient_landing_omits_inactive_grant_metadata(
    recipient_browser, bound_grant, session, status
):
    bound_grant.grantee = "Previously private grant label"
    bound_grant.purpose = "Previously private grant purpose"
    if status == "revoked":
        bound_grant.revoked_at = datetime.now(UTC)
    elif status == "expired":
        bound_grant.valid_to = datetime.now(UTC) - timedelta(seconds=1)
    else:
        bound_grant.valid_from = datetime.now(UTC) + timedelta(days=2)
    session.commit()
    landing = recipient_browser.get("/share/received")
    assert landing.status_code == 200
    assert "Previously private grant label" not in landing.text
    assert "Previously private grant purpose" not in landing.text
    assert f'href="/share/as/{bound_grant.id}/"' not in landing.text


@pytest.mark.parametrize(
    "sensitive", ["audit:read", "logs:write", "parameters:read", "corrections:read", "*"]
)
def test_mixed_sensitive_legacy_grant_fails_closed_as_a_whole(
    recipient_browser, owner_browser, admin, bound_grant, session, sensitive
):
    bound_grant.scopes = ["register:read", sensitive]
    bound_grant.grantee = "Broad legacy grant"
    session.commit()
    assert "Broad legacy grant" not in recipient_browser.get("/share/received").text
    for browser in (recipient_browser, owner_browser, admin):
        for suffix in ("", "register.json", "ngsi-ld.json", "advice.json", "observations.json"):
            assert browser.get(f"/share/as/{bound_grant.id}/{suffix}").status_code == 404
    assert session.scalar(select(AccessLog.id).where(AccessLog.grant_id == bound_grant.id)) is None


def test_generated_hostile_grant_ids_never_read_or_audit(recipient_browser, session, bound_grant):
    from hypothesis import given
    from hypothesis import settings as hypothesis_settings
    from hypothesis import strategies as st

    assert recipient_browser.get(f"/share/as/{bound_grant.id}/register.json").status_code == 200
    last = session.scalar(select(AccessLog.id).order_by(AccessLog.id.desc()).limit(1))

    @hypothesis_settings(max_examples=32, deadline=None)
    @given(st.one_of(st.integers(max_value=-1), st.integers(min_value=2**63)))
    def hostile_id(value):
        for suffix in ("register.json", "advice.json"):
            response = recipient_browser.get(f"/share/as/{value}/{suffix}")
            assert response.status_code == 404
            assert "Named recipient" not in response.text
            assert "Independent recipient review" not in response.text
            assert "log_id" not in response.text
        assert session.scalar(select(AccessLog.id).where(AccessLog.id > last)) is None

    hostile_id()
    session.refresh(bound_grant)
    assert bound_grant.recipient_identity_id is not None
    assert bound_grant.scopes == ["logs:read", "register:read"]


def test_ordinary_grant_exports_exclude_pending_capture_and_raw_transcripts(
    recipient_browser, bound_grant, spray
):
    from farmcore.core.logs import RANK_VOICE, SourceInput

    secret = "private-raw-transcript-never-shared"
    done = spray(
        source=SourceInput("voice", "synthetic:confirmed", {"transcript": secret}, 0.9, RANK_VOICE)
    )
    pending = spray(
        status="pending",
        source=SourceInput(
            "voice", "synthetic:pending", {"transcript": secret + "-pending"}, 0.9, RANK_VOICE
        ),
    )
    for suffix in ("", "register.json", "ngsi-ld.json"):
        response = recipient_browser.get(f"/share/as/{bound_grant.id}/{suffix}")
        assert response.status_code == 200
        assert secret not in response.text
        assert "raw_payload" not in response.text
        assert "transcript" not in response.text
    rows = recipient_browser.get(f"/share/as/{bound_grant.id}/register.json").json()
    assert {r["log_id"] for r in rows} == {done.id}
    assert pending.id != done.id


@pytest.mark.parametrize("login", ["", "missing-recipient", "disabled-recipient"])
def test_owner_must_bind_existing_enabled_login_instead_of_a_matching_label(
    owner_browser, session, recipient, credential_hash, demo, login
):
    disabled = LocalIdentity(
        login="disabled-recipient", password_hash=credential_hash, disabled_at=datetime.now(UTC)
    )
    session.add(disabled)
    session.commit()
    response = mutation(
        owner_browser,
        f"/share/grants?farm={demo.farm.id}",
        grantee=recipient.login,
        recipient_login=login,
        preset="adviser_logs",
        purpose="Must not infer identity",
        valid_to=(datetime.now(UTC) + timedelta(days=2)).date().isoformat(),
    )
    assert response.status_code == 422
    assert (
        session.scalar(select(DataGrant.id).where(DataGrant.purpose == "Must not infer identity"))
        is None
    )


@pytest.mark.parametrize("value", [2**100, 2**63, -(2**63) - 1])
def test_owner_revoke_bigint_overflow_is_private_not_database_error(
    owner_browser, bound_grant, demo, session, value
):
    response = mutation(owner_browser, f"/share/grants/{value}/revoke?farm={demo.farm.id}")
    assert response.status_code == 404
    session.refresh(bound_grant)
    assert bound_grant.revoked_at is None


def test_concurrent_owner_revocations_commit_one_event_and_deny_next_read(
    settings, committed_session_factory, credential_hash
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from farmcore.core.models.canonical import Event, Farm
    from farmcore.core.models.identity import FarmMembership

    with committed_session_factory.begin() as session:
        farm = Farm(name="Synthetic concurrent consent farm")
        owner = LocalIdentity(login="race-owner", password_hash=credential_hash)
        recipient = LocalIdentity(login="race-recipient", password_hash=credential_hash)
        session.add_all([farm, owner, recipient])
        session.flush()
        session.add(FarmMembership(identity_id=owner.id, farm_id=farm.id, role="owner"))
        grant = DataGrant(
            farm_id=farm.id,
            grantee="Race recipient",
            recipient_identity_id=recipient.id,
            scopes=["register:read"],
            purpose="Concurrent consent",
        )
        session.add(grant)
        session.flush()
        farm_id, grant_id, owner_id = farm.id, grant.id, owner.id
    app = create_app(settings, session_factory=committed_session_factory)
    barrier = Barrier(2)
    with TestClient(app, base_url="https://testserver") as viewer:
        assert sign_in(viewer, "race-recipient").status_code == 303
        url = f"/share/as/{grant_id}/register.json"
        assert viewer.get(url).status_code == 200

        def revoke(_):
            with TestClient(app, base_url="https://testserver") as browser:
                assert sign_in(browser, "race-owner").status_code == 303
                barrier.wait(timeout=15)
                return mutation(
                    browser, f"/share/grants/{grant_id}/revoke?farm={farm_id}"
                ).status_code

        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(revoke, range(2))) == [303, 303]
        assert viewer.get(url).status_code == 403
    with committed_session_factory() as session:
        assert session.get(DataGrant, grant_id).revoked_at is not None
        rows = session.scalars(select(Event).where(Event.kind == "share.revoked")).all()
        assert [(r.farm_id, r.res_id, r.actor) for r in rows] == [
            (farm_id, grant_id, f"user:{owner_id}")
        ]

"""Identity, session and farm membership contracts through CLI and HTTP."""

from contextlib import suppress

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from farmcore.cli import cli
from farmcore.core.models.canonical import Farm
from farmcore.settings import get_settings

PASSWORD = "long-local-test-password-32-chars"  # gauntlet:allow-secret synthetic credential


def test_local_bootstrap_creates_only_explicit_owner_and_salted_credential(
    committed_session_factory, settings, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        with committed_session_factory() as session:
            farm = Farm(name="Private family farm")
            session.add(farm)
            session.commit()
            farm_id = farm.id
        result = CliRunner().invoke(
            cli,
            ["users", "create", "farmer", "--farm-id", str(farm_id), "--password-stdin"],
            input=PASSWORD + "\n",
        )
        assert result.exit_code == 0, result.output
        assert PASSWORD not in result.output
        with committed_session_factory() as session:
            row = session.execute(
                text(
                    "SELECT u.login,u.password_hash,m.role,m.farm_id FROM local_identity u "
                    "JOIN farm_membership m ON m.identity_id=u.id"
                )
            ).one()
            assert row.login == "farmer"
            assert row.role == "owner"
            assert row.farm_id == farm_id
            assert row.password_hash.startswith("scrypt$")
            assert PASSWORD not in row.password_hash
    finally:
        get_settings.cache_clear()


def test_login_page_issues_post_only_csrf_form_without_farm_metadata(settings, session_factory):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    with TestClient(
        create_app(settings, session_factory=session_factory), base_url="https://testserver"
    ) as browser:
        response = browser.get("/login")
        assert response.status_code == 200
        assert 'name="csrf_token"' in response.text
        assert 'name="password"' in response.text
        assert response.headers["cache-control"] == "no-store"
        assert browser.cookies.get("farmcore_login_csrf")
        assert "Private family farm" not in response.text


def _account(factory, *, login="farmer", role="owner", farm_id=None):
    from farmcore.core.identity import hash_password
    from farmcore.core.models.identity import FarmMembership, LocalIdentity

    with factory() as session:
        if farm_id is None:
            farm = Farm(name=login + " private farm")
            session.add(farm)
            session.flush()
            farm_id = farm.id
        identity = LocalIdentity(login=login, password_hash=hash_password(PASSWORD))
        session.add(identity)
        session.flush()
        session.add(FarmMembership(identity_id=identity.id, farm_id=farm_id, role=role))
        session.commit()
        return identity.id, farm_id


def _login(browser, *, login="farmer", password=PASSWORD, next=None):
    import re

    page = browser.get("/login")
    nonce = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    data = {"login": login, "password": password, "csrf_token": nonce}
    if next is not None:
        data["next"] = next
    return browser.post(
        "/login",
        data=data,
        headers={"Origin": "https://testserver"},
        follow_redirects=False,
    )


def test_login_stores_only_hashed_opaque_session_with_wall_time_expiry(
    settings, committed_session_factory
):
    from datetime import UTC, datetime

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from farmcore.core.models.identity import LocalSession
    from farmcore.main import create_app

    identity_id, _ = _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        response = _login(browser)
        assert response.status_code == 303
        token = browser.cookies.get("farmcore_session")
        assert token and len(token) >= 43
        assert token not in response.headers["location"]
        assert PASSWORD not in response.text
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "Secure" in response.headers["set-cookie"]
        with committed_session_factory() as session:
            saved = session.scalar(select(LocalSession))
            assert saved.identity_id == identity_id
            assert saved.token_hash != token
            assert len(saved.token_hash) == 64
            assert saved.expires_at > datetime.now(UTC)
            assert (saved.expires_at - saved.created_at).total_seconds() <= 12 * 3600


def test_login_rejects_missing_csrf_and_cross_origin_before_credentials(settings, session_factory):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    with TestClient(
        create_app(settings, session_factory=session_factory), base_url="https://testserver"
    ) as browser:
        response = browser.post("/login", data={"login": "farmer", "password": PASSWORD})
        assert response.status_code == 403
        assert not browser.cookies.get("farmcore_session")
        browser.get("/login")
        response = browser.post(
            "/login",
            data={
                "login": "farmer",
                "password": PASSWORD,
                "csrf_token": browser.cookies.get("farmcore_login_csrf"),
            },
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403
        assert not browser.cookies.get("farmcore_session")


def test_failed_login_budget_survives_request_rollback_and_app_restart(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from farmcore.core.models.identity import LoginBucket
    from farmcore.main import create_app

    for attempt in range(6):
        # A fresh application cannot reset persisted throttling; each 401 is a completed txn.
        with TestClient(
            create_app(settings, session_factory=committed_session_factory),
            base_url="https://testserver",
        ) as browser:
            response = _login(browser, login="unknown", password="wrong-password")
            assert response.status_code == (401 if attempt < 5 else 429)
            assert not browser.cookies.get("farmcore_session")
    with committed_session_factory() as session:
        rows = list(session.scalars(select(LoginBucket)))
        assert len(rows) == 3
        assert all(row.attempts <= 100 for row in rows)
        assert response.headers["retry-after"]


def test_membership_reads_and_switcher_do_not_infer_legacy_owner_grant_identity(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.core.models.canonical import DataGrant
    from farmcore.main import create_app

    _, own_farm = _account(committed_session_factory)
    with committed_session_factory() as session:
        foreign = Farm(name="Secret other holding")
        session.add(foreign)
        session.flush()
        foreign_id = foreign.id
        session.add(
            DataGrant(farm_id=foreign_id, grantee="farmer", scopes=["*"], purpose="legacy name")
        )
        session.commit()
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        page = browser.get("/fields/", params={"farm": own_farm})
        assert page.status_code == 200
        assert browser.get("/operations/register.csv", params={"farm": own_farm}).status_code == 200
        switcher = browser.get("/farms/switcher")
        assert switcher.status_code == 200
        assert "Secret other holding" not in switcher.text
        for endpoint in (
            "/fields/",
            "/operations/register.csv",
            "/operations/register.json",
            "/farms/select",
            "/assistant/ask?q=list+parcels",
        ):
            denied = browser.get(endpoint, params={"farm": foreign_id})
            assert denied.status_code == 403, (endpoint, denied.text)
            assert "Secret other holding" not in denied.text


def test_member_cannot_read_installation_state_or_shared_capture_history(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        for path in (
            "/apps",
            "/apps/",
            "/apps.json",
            "/clock",
            "/demo/",
            "/chat/",
            "/assistant/pending",
            "/assistant/history",
            "/assistant/draft",
        ):
            response = browser.get(path)
            assert response.status_code == 403, (path, response.status_code)
        safe = browser.get("/assistant/ask", params={"q": "list parcels"})
        assert safe.status_code == 200
        assert safe.json()["via"] == "keywords"


@pytest.mark.parametrize("revoke", ["session", "expiry", "identity", "membership", "forged"])
def test_revocation_and_disabled_identity_deny_the_next_request_even_in_demo(
    settings, committed_session_factory, revoke
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    identity_id, farm_id = _account(committed_session_factory)
    demo_settings = settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [farm_id]})
    with TestClient(
        create_app(demo_settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        assert browser.get("/fields/", params={"farm": farm_id}).status_code == 200
        with committed_session_factory.begin() as session:
            if revoke == "session":
                session.execute(text("UPDATE local_session SET revoked_at=now()"))
            elif revoke == "expiry":
                session.execute(
                    text(
                        "UPDATE local_session SET created_at=now()-interval '13 hours',"
                        "expires_at=now()-interval '1 second'"
                    )
                )
            elif revoke == "identity":
                session.execute(
                    text("UPDATE local_identity SET disabled_at=now() WHERE id=:id"),
                    {"id": identity_id},
                )
            elif revoke == "membership":
                session.execute(
                    text("UPDATE farm_membership SET revoked_at=now() WHERE identity_id=:id"),
                    {"id": identity_id},
                )
        if revoke == "forged":
            browser.cookies.clear()
            browser.cookies.set("farmcore_session", "forged-opaque-token")
        denied = browser.get("/fields/", params={"farm": farm_id})
        assert denied.status_code == 403
        assert "farmer private farm" not in denied.text


def test_logout_requires_csrf_revokes_server_session_and_lands_on_public_html(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        old_token = browser.cookies.get("farmcore_session")
        assert browser.post("/logout").status_code == 403
        assert browser.get("/fields/").status_code == 200
        response = browser.post(
            "/logout",
            headers={
                "Origin": "https://testserver",
                "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/login"
        assert not browser.cookies.get("farmcore_session")
        assert browser.get("/login").status_code == 200
        browser.cookies.set("farmcore_session", old_token)
        assert browser.get("/fields/").status_code == 403
        with committed_session_factory() as session:
            assert session.scalar(text("SELECT revoked_at FROM local_session")) is not None


def test_operator_cookie_mutations_require_origin_but_explicit_header_still_works(
    settings, session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    with TestClient(
        create_app(settings, session_factory=session_factory), base_url="https://testserver"
    ) as browser:
        browser.cookies.set("admin_token", settings.admin_token.get_secret_value())
        assert browser.post("/apps/missing/install").status_code == 403
        assert (
            browser.post(
                "/apps/missing/install", headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert (
            browser.post(
                "/apps/missing/install", headers={"Origin": "https://testserver"}
            ).status_code
            == 404
        )
        assert (
            browser.post(
                "/apps/missing/install",
                headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
            ).status_code
            == 404
        )


@pytest.mark.parametrize("role", ["owner", "advisor"])
def test_only_owner_can_mutate_assigned_farm_with_form_or_header_csrf(
    settings, committed_session_factory, role
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    identity_id, farm_id = _account(committed_session_factory, role=role)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        assert (
            browser.post(
                "/operations/production", params={"farm": farm_id}, data={"mode": "organic"}
            ).status_code
            == 403
        )
        response = browser.post(
            "/operations/production",
            params={"farm": farm_id},
            data={"mode": "organic", "csrf_token": browser.cookies.get("farmcore_csrf")},
            headers={"Origin": "https://testserver"},
            follow_redirects=False,
        )
        assert response.status_code == (303 if role == "owner" else 403)
        with committed_session_factory() as session:
            farm = session.get(Farm, farm_id)
            assert farm.props.get("production") == ("organic" if role == "owner" else None)
            if role == "owner":
                assert (
                    session.scalar(
                        text(
                            "SELECT actor FROM access_log WHERE scope='logs:write' "
                            "ORDER BY id DESC LIMIT 1"
                        )
                    )
                    == f"user:{identity_id}"
                )
        assert (
            browser.post(
                "/clock/step",
                data={"to": "2024-04-11T10:00"},
                headers={
                    "Origin": "https://testserver",
                    "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
                },
            ).status_code
            == 403
        )


@pytest.mark.parametrize("role", ["owner", "advisor"])
def test_member_import_is_farm_bound_preserves_multipart_and_records_real_actor(
    settings, committed_session_factory, role
):
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from farmcore.apps.operations.isoxml_demo import demo_taskdata
    from farmcore.core import clock
    from farmcore.core.models.canonical import Event, Log
    from farmcore.main import create_app
    from tests.apps.operations.isoxml_farm import CLOCK, local, make_farm

    with committed_session_factory.begin() as session:
        farm = make_farm(session, settings)
        farm_id = farm.repo.farm_id
        clock.set_replay(session, local(CLOCK), settings)
    identity_id, _ = _account(committed_session_factory, farm_id=farm_id, role=role)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        response = browser.post(
            "/operations/isoxml",
            params={"farm": farm_id},
            files={"file": ("TASKDATA.XML", demo_taskdata(), "application/xml")},
            data={"csrf_token": browser.cookies.get("farmcore_csrf")},
            headers={"Origin": "https://testserver", "HX-Request": "true"},
        )
        assert response.status_code == (200 if role == "owner" else 403), response.text
        with committed_session_factory() as session:
            rows = list(session.scalars(select(Log)))
            assert len(rows) == (2 if role == "owner" else 0)
            if role == "owner":
                assert {row.farm_id for row in rows} == {farm_id}
                assert set(
                    session.scalars(select(Event.actor).where(Event.kind.like("log.%")))
                ) == {f"user:{identity_id}"}


def test_owner_assigns_and_revokes_memberships_with_next_request_denial(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _, farm_id = _account(committed_session_factory)
    advisor_id, _ = _account(committed_session_factory, login="advisor")
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner).status_code == 303
        assert _login(advisor, login="advisor").status_code == 303
        page = owner.get("/farms/members", params={"farm": farm_id})
        assert page.status_code == 200
        assert 'name="csrf_token"' in page.text
        assert advisor.get("/fields/", params={"farm": farm_id}).status_code == 403
        headers = {
            "Origin": "https://testserver",
            "X-CSRF-Token": owner.cookies.get("farmcore_csrf"),
        }
        granted = owner.post(
            "/farms/members",
            params={"farm": farm_id},
            data={"login": "advisor", "role": "advisor"},
            headers=headers,
            follow_redirects=False,
        )
        assert granted.status_code == 303, granted.text
        assert advisor.get("/fields/", params={"farm": farm_id}).status_code == 200
        assert advisor.get("/farms/members", params={"farm": farm_id}).status_code == 403
        with committed_session_factory() as session:
            membership_id = session.scalar(
                text(
                    "SELECT id FROM farm_membership WHERE farm_id=:farm AND identity_id=:identity"
                ),
                {"farm": farm_id, "identity": advisor_id},
            )
        revoked = owner.post(
            f"/farms/members/{membership_id}/revoke",
            params={"farm": farm_id},
            headers=headers,
            follow_redirects=False,
        )
        assert revoked.status_code == 303
        assert advisor.get("/fields/", params={"farm": farm_id}).status_code == 403


@pytest.mark.parametrize("action", ["revoke", "demote"])
def test_last_enabled_owner_cannot_be_removed(settings, committed_session_factory, action):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    identity_id, farm_id = _account(committed_session_factory)
    with committed_session_factory() as session:
        membership_id = session.scalar(
            text("SELECT id FROM farm_membership WHERE identity_id=:id"), {"id": identity_id}
        )
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        headers = {
            "Origin": "https://testserver",
            "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
        }
        path = f"/farms/members/{membership_id}/revoke" if action == "revoke" else "/farms/members"
        response = browser.post(
            path,
            params={"farm": farm_id},
            data={"login": "farmer", "role": "advisor"},
            headers=headers,
            follow_redirects=False,
        )
        assert response.status_code == 409
        assert "at least one enabled owner" in response.text.lower()
        assert browser.get("/farms/members", params={"farm": farm_id}).status_code == 200


@pytest.mark.parametrize("command", ["disable", "revoke-sessions"])
def test_local_operator_can_invalidate_an_existing_identity_session(
    settings, committed_session_factory, monkeypatch, tmp_path, command
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _account(committed_session_factory)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        with TestClient(
            create_app(settings, session_factory=committed_session_factory),
            base_url="https://testserver",
        ) as browser:
            assert _login(browser).status_code == 303
            result = CliRunner().invoke(cli, ["users", command, "farmer"])
            assert result.exit_code == 0, result.output
            assert PASSWORD not in result.output
            assert browser.get("/fields/").status_code == 403
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("role", ["owner", "advisor"])
def test_inventory_threshold_writes_use_current_membership_and_farm(
    settings, committed_session_factory, role
):
    from decimal import Decimal

    from fastapi.testclient import TestClient

    from farmcore.apps.inventory.models import InventoryStock
    from farmcore.apps.operations.seed import ensure_products
    from farmcore.main import create_app

    _, farm_id = _account(committed_session_factory, role=role)
    with committed_session_factory.begin() as session:
        product = next(iter(ensure_products(session).values()))
        stock = InventoryStock(
            farm_id=farm_id, product_id=product.id, season=2024, purchased=20, unit="kg"
        )
        session.add(stock)
        session.flush()
        stock_id = stock.id
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        response = browser.post(
            f"/inventory/{stock_id}/low-stock",
            params={"farm": farm_id},
            data={"low_stock": "2.5"},
            headers={
                "Origin": "https://testserver",
                "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
            },
            follow_redirects=False,
        )
        assert response.status_code == (303 if role == "owner" else 403)
        with committed_session_factory() as session:
            assert session.get(InventoryStock, stock_id).low_stock == (
                Decimal("2.5") if role == "owner" else None
            )


@pytest.mark.parametrize("role", ["owner", "advisor"])
def test_member_shell_exposes_role_logout_and_only_authorized_farm_forms(
    settings, committed_session_factory, role
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _, farm_id = _account(committed_session_factory, role=role)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        page = browser.get("/operations/", params={"farm": farm_id})
        assert page.status_code == 200
        assert 'action="/logout"' in page.text
        assert f"farmer · {role}" in page.text
        assert 'name="csrf-token"' in page.text
        assert ('enctype="multipart/form-data"' in page.text) == (role == "owner")
        assert ('href="/farms/members?farm=' in page.text) == (role == "owner")
        assert 'hx-get="/clock"' not in page.text


def test_member_activity_hides_unbound_capture_drafts_but_preserves_completed_facts(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.core import clock
    from farmcore.core.models.canonical import Event
    from farmcore.main import create_app
    from tests.apps.operations.isoxml_farm import local

    _, farm_id = _account(committed_session_factory)
    with committed_session_factory.begin() as session:
        clock.set_replay(session, local("2024-04-10 10:00"), settings)
        for status, body in (
            ("pending", "Secret shared voice draft"),
            ("done", "Confirmed operation"),
        ):
            session.add(
                Event(
                    farm_id=farm_id,
                    kind="log.created",
                    res_model="farm",
                    res_id=farm_id,
                    occurred_at=local("2024-04-10 09:00"),
                    source_app="core",
                    actor="web:farmer",
                    body=body,
                    payload={"status": status, "type": "spray"},
                )
            )
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        for path in ("/activity/", "/activity/today"):
            page = browser.get(path, params={"farm": farm_id})
            assert page.status_code == 200
            assert "waiting for confirmation" not in page.text
            assert "Spray logged" in page.text
        operator = browser.get(
            "/activity/",
            params={"farm": farm_id},
            headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
        )
        assert "waiting for confirmation" in operator.text


def test_read_only_assistant_context_is_principal_bound_across_account_switch(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _, farm_id = _account(committed_session_factory)
    advisor_id, _ = _account(
        committed_session_factory, login="advisor", farm_id=farm_id, role="advisor"
    )
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        params = {"q": "list parcels", "conversation": "same-browser-tab", "farm": farm_id}
        assert browser.get("/assistant/ask", params=params).json()["fresh_conversation"]
        assert not browser.get("/assistant/ask", params=params).json()["fresh_conversation"]
        assert (
            browser.post(
                "/logout",
                headers={
                    "Origin": "https://testserver",
                    "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
                },
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert _login(browser, login="advisor").status_code == 303
        assert browser.get("/assistant/ask", params=params).json()["fresh_conversation"]
        with committed_session_factory() as session:
            actor = session.scalar(
                text(
                    "SELECT actor FROM access_log WHERE purpose='assistant' "
                    "ORDER BY id DESC LIMIT 1"
                )
            )
            assert actor == f"user:{advisor_id}"


def test_login_aliases_share_one_persisted_account_budget(settings, committed_session_factory):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        for attempt in range(6):
            response = _login(browser, login=" " * attempt + "FARMER", password="wrong-password")
            assert response.status_code == (401 if attempt < 5 else 429)


def test_login_unicode_csrf_is_a_denial_not_an_application_error(settings, session_factory):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    with TestClient(
        create_app(settings, session_factory=session_factory), base_url="https://testserver"
    ) as browser:
        browser.get("/login")
        response = browser.post(
            "/login",
            data={"login": "farmer", "password": PASSWORD, "csrf_token": "𝒇𝒐𝒓𝒈𝒆𝒅"},
            headers={"Origin": "https://testserver"},
        )
        assert response.status_code == 403


def test_concurrent_login_reservations_cannot_overrun_committed_account_budget(
    settings, committed_session_factory
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    app = create_app(settings, session_factory=committed_session_factory)
    barrier = Barrier(8)

    def attempt(_):
        with TestClient(app, base_url="https://testserver") as browser:
            barrier.wait(timeout=10)
            return _login(browser, login="unknown", password="wrong-password").status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(attempt, range(8)))
    assert statuses.count(401) == 5
    assert statuses.count(429) == 3
    with committed_session_factory() as session:
        assert (
            session.scalar(text("SELECT attempts FROM login_bucket WHERE dimension='account'")) == 5
        )
        assert session.scalar(text("SELECT count(*) FROM login_bucket")) == 3


def test_concurrent_owner_revocations_preserve_one_enabled_owner(
    settings, committed_session_factory
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    first, farm_id = _account(committed_session_factory)
    second, _ = _account(committed_session_factory, login="second", farm_id=farm_id)
    with committed_session_factory() as session:
        ids = dict(
            session.execute(
                text("SELECT identity_id,id FROM farm_membership WHERE farm_id=:farm"),
                {"farm": farm_id},
            ).all()
        )
    app = create_app(settings, session_factory=committed_session_factory)
    barrier = Barrier(2)

    def revoke(other):
        login = "farmer" if other == second else "second"
        with TestClient(app, base_url="https://testserver") as browser:
            assert _login(browser, login=login).status_code == 303
            barrier.wait(timeout=10)
            return browser.post(
                f"/farms/members/{ids[other]}/revoke",
                params={"farm": farm_id},
                headers={
                    "Origin": "https://testserver",
                    "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
                },
                follow_redirects=False,
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(revoke, [first, second]))
    assert sorted(statuses) in ([303, 403], [303, 409])
    with committed_session_factory() as session:
        assert (
            session.scalar(
                text(
                    "SELECT count(*) FROM farm_membership WHERE farm_id=:farm "
                    "AND role='owner' AND revoked_at IS NULL"
                ),
                {"farm": farm_id},
            )
            == 1
        )


def test_hostile_farm_selectors_never_bypass_membership(settings, committed_session_factory):
    from fastapi.testclient import TestClient
    from hypothesis import assume, given
    from hypothesis import settings as hypothesis_settings
    from hypothesis import strategies as st

    from farmcore.main import create_app

    _, farm_id = _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303

        @hypothesis_settings(max_examples=40, deadline=None)
        @given(
            st.one_of(
                st.integers(min_value=-(2**100), max_value=2**100).map(str),
                st.text(min_size=1, max_size=200),
            )
        )
        def check(selector):
            with suppress(ValueError):
                assume(int(selector) != farm_id)
            response = browser.get("/operations/register.json", params={"farm": selector})
            assert response.status_code == 403
            assert "farmer private farm" not in response.text

        check()


def test_member_write_takes_canonical_operation_lock_before_farm_row_lock(
    settings, committed_session_factory
):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from farmcore.apps.operations.isoxml_demo import demo_taskdata
    from farmcore.core import clock
    from farmcore.core.logs import lock_farm
    from farmcore.main import create_app
    from tests.apps.operations.isoxml_farm import CLOCK, local, make_farm

    with committed_session_factory.begin() as seeded:
        farm = make_farm(seeded, settings)
        farm_id = farm.repo.farm_id
        clock.set_replay(seeded, local(CLOCK), settings)
    _account(committed_session_factory, farm_id=farm_id)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        csrf = browser.cookies.get("farmcore_csrf")
        with ThreadPoolExecutor(max_workers=1) as pool, committed_session_factory() as canonical:
            lock_farm(canonical, farm_id)
            future = pool.submit(
                browser.post,
                "/operations/isoxml",
                params={"farm": farm_id},
                files={"file": ("TASKDATA.XML", demo_taskdata(), "application/xml")},
                headers={"Origin": "https://testserver", "X-CSRF-Token": csrf},
                follow_redirects=False,
            )
            try:
                deadline = time.monotonic() + 5
                while True:
                    with committed_session_factory() as observer:
                        waiting = observer.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname=current_database() "
                                "AND wait_event='advisory' AND pid<>pg_backend_pid()"
                            )
                        )
                    if waiting:
                        break
                    assert time.monotonic() < deadline, "Member write never reached operation lock"
                    time.sleep(0.01)
                # A writer already holding the canonical advisory lock must still take the row.
                canonical.execute(
                    select(Farm.id).where(Farm.id == farm_id).with_for_update(nowait=True)
                )
            finally:
                canonical.rollback()
                response = future.result(timeout=10)
            assert response.status_code == 200


def test_local_password_recovery_revokes_old_sessions_and_reenables_identity(
    settings, committed_session_factory, monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    identity_id, _ = _account(committed_session_factory)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    replacement = "replacement-local-password-123"  # gauntlet:allow-secret synthetic credential
    try:
        with TestClient(
            create_app(settings, session_factory=committed_session_factory),
            base_url="https://testserver",
        ) as browser:
            assert _login(browser).status_code == 303
            with committed_session_factory.begin() as session:
                session.execute(
                    text("UPDATE local_identity SET disabled_at=now() WHERE id=:id"),
                    {"id": identity_id},
                )
            result = CliRunner().invoke(
                cli,
                ["users", "set-password", "farmer", "--password-stdin"],
                input=replacement + "\n",
            )
            assert result.exit_code == 0, result.output
            assert replacement not in result.output
            assert browser.get("/fields/").status_code == 403
            assert _login(browser, password=PASSWORD).status_code == 401
            assert _login(browser, password=replacement).status_code == 303
            assert browser.get("/fields/").status_code == 200
    finally:
        get_settings.cache_clear()


def test_member_history_demo_marker_only_uses_completed_records(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.core import clock
    from farmcore.core.models.canonical import Log, LogSource
    from farmcore.main import create_app
    from tests.apps.operations.isoxml_farm import CLOCK, local, make_farm

    with committed_session_factory.begin() as session:
        farm = make_farm(session, settings)
        farm_id = farm.repo.farm_id
        parcel = farm.parcels["P5"]
        parcel_id = parcel.id
        clock.set_replay(session, local(CLOCK), settings)
        draft = Log(
            farm_id=farm_id,
            type="spray",
            status="pending",
            name="Shared pending draft",
            ts_start=local("2024-04-10 08:00"),
            parcels=[parcel],
        )
        session.add(draft)
        session.flush()
        log_id = draft.id
        session.add(
            LogSource(
                log_id=log_id,
                source_type="voice",
                external_key="shared-draft",
                raw_payload={"scene": "season-2024"},
                precision_rank=1,
            )
        )
    _account(committed_session_factory, farm_id=farm_id)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        path = f"/advisor/parcels/{parcel_id}/history"
        pending = browser.get(path, params={"farm": farm_id})
        assert pending.status_code == 200
        assert "data-not-recorded" not in pending.text
        assert "Shared pending draft" not in pending.text
        with committed_session_factory.begin() as session:
            session.get(Log, log_id).status = "done"
        confirmed = browser.get(path, params={"farm": farm_id})
        assert confirmed.status_code == 200
        assert "data-not-recorded" in confirmed.text


def test_member_foreign_selector_is_denied_on_shell_and_metadata_routes(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        for path in ("/", "/data-sources/", "/assistant/voice", "/prefs/expert"):
            response = browser.get(path, params={"farm": 999999}, follow_redirects=False)
            assert response.status_code == 403, (path, response.status_code)


def test_new_login_rotates_credentials_and_clears_stale_farm_selection(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.core.access import FARM_COOKIE
    from farmcore.main import create_app

    _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        # A previous identity's remembered farm must not trap a newly authenticated owner.
        browser.cookies.set(FARM_COOKIE, "999999", domain="testserver.local", path="/")
        assert _login(browser).status_code == 303
        assert browser.cookies.get(FARM_COOKIE) is None
        first = browser.cookies.get("farmcore_session")
        assert browser.get("/fields/").status_code == 200
        assert _login(browser).status_code == 303
        assert browser.cookies.get("farmcore_session") != first


@pytest.mark.parametrize("nonce", ["wrong", "", "𝒏𝒐𝒑𝒆"])
def test_member_mutation_with_valid_origin_still_requires_session_bound_nonce(
    settings, committed_session_factory, nonce
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _, farm_id = _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        response = browser.post(
            "/operations/production",
            data={"mode": "organic", "csrf_token": nonce},
            headers={"Origin": "https://testserver"},
        )
        assert response.status_code == 403
        with committed_session_factory() as session:
            assert session.get(Farm, farm_id).props.get("production") is None


def test_persisted_session_survives_application_restart_and_long_forgery_denies(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        token = browser.cookies.get("farmcore_session")
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as restarted:
        restarted.cookies.set("farmcore_session", token)
        assert restarted.get("/fields/").status_code == 200
        restarted.cookies.clear()
        restarted.cookies.set("farmcore_session", "z" * 512)
        assert restarted.get("/fields/").status_code == 403


@pytest.mark.parametrize("encoded", ["invalid", "other$131072$8$1$00$00", "scrypt$bad$8$1$00$00"])
def test_unreadable_local_credentials_fail_closed_with_generic_error(
    settings, committed_session_factory, encoded
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    identity_id, _ = _account(committed_session_factory)
    with committed_session_factory.begin() as session:
        session.execute(
            text("UPDATE local_identity SET password_hash=:encoded WHERE id=:id"),
            {"id": identity_id, "encoded": encoded},
        )
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        response = _login(browser)
        assert response.status_code == 401
        assert "Sign-in failed." in response.text
        assert PASSWORD not in response.text
        assert not browser.cookies.get("farmcore_session")


def test_login_rate_budget_renews_only_after_its_wall_time_window(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        for _ in range(5):
            assert _login(browser, login="unknown", password="wrong-password").status_code == 401
        assert _login(browser, login="unknown", password="wrong-password").status_code == 429
        with committed_session_factory.begin() as session:
            session.execute(
                text("UPDATE login_bucket SET window_start=now()-interval '11 minutes'")
            )
        assert _login(browser, login="unknown", password="wrong-password").status_code == 401
        with committed_session_factory() as session:
            assert (
                session.scalar(text("SELECT attempts FROM login_bucket WHERE dimension='account'"))
                == 1
            )


@pytest.mark.parametrize(
    ("login", "password", "farm_argument", "message"),
    [
        ("bad login", PASSWORD, None, "Use a login"),
        ("newuser", "short", None, "Use a password"),
        ("newuser", PASSWORD, -1, "No such farm"),
        ("farmer", PASSWORD, None, "Login already exists"),
    ],
)
def test_invalid_local_bootstrap_rolls_back_without_printing_credentials(
    settings,
    committed_session_factory,
    monkeypatch,
    tmp_path,
    login,
    password,
    farm_argument,
    message,
):
    _account(committed_session_factory)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        args = ["users", "create", login, "--password-stdin"]
        if farm_argument is not None:
            args += ["--farm-id", str(farm_argument)]
        result = CliRunner().invoke(cli, args, input=password + "\n")
        assert result.exit_code != 0
        assert message in result.output
        assert PASSWORD not in result.output
        with committed_session_factory() as session:
            assert session.scalar(text("SELECT count(*) FROM local_identity")) == 1
            assert session.scalar(text("SELECT count(*) FROM farm_membership")) == 1
    finally:
        get_settings.cache_clear()


def test_local_hidden_prompt_uses_independent_salts_without_echoing_password(
    settings, committed_session_factory, monkeypatch, tmp_path
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        for login in ("first", "second"):
            result = CliRunner().invoke(
                cli, ["users", "create", login], input=f"{PASSWORD}\n{PASSWORD}\n"
            )
            assert result.exit_code == 0, result.output
            assert PASSWORD not in result.output
        with committed_session_factory() as session:
            hashes = list(
                session.scalars(text("SELECT password_hash FROM local_identity ORDER BY login"))
            )
            assert len(hashes) == 2
            assert hashes[0].split("$")[4] != hashes[1].split("$")[4]
            assert all(len(h.split("$")[4]) == 32 for h in hashes)
            assert session.scalar(text("SELECT count(*) FROM farm_membership")) == 0
    finally:
        get_settings.cache_clear()


def test_owner_membership_edits_are_scoped_and_revoked_assignments_can_be_restored(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _, farm_id = _account(committed_session_factory)
    other_id, other_farm = _account(committed_session_factory, login="other")
    with committed_session_factory() as session:
        foreign_id = session.scalar(
            text("SELECT id FROM farm_membership WHERE identity_id=:id"), {"id": other_id}
        )
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        headers = {
            "Origin": "https://testserver",
            "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
        }
        assert (
            browser.post(
                f"/farms/members/{foreign_id}/revoke", params={"farm": farm_id}, headers=headers
            ).status_code
            == 404
        )
        for login, role in (
            ("unknown", "advisor"),
            ("bad login", "advisor"),
            ("other", "operator"),
        ):
            assert (
                browser.post(
                    "/farms/members",
                    params={"farm": farm_id},
                    data={"login": login, "role": role},
                    headers=headers,
                ).status_code
                == 422
            )
        for role in ("advisor", "owner", "advisor"):
            response = browser.post(
                "/farms/members",
                params={"farm": farm_id},
                data={"login": "other", "role": role},
                headers=headers,
                follow_redirects=False,
            )
            assert response.status_code == 303
        with committed_session_factory() as session:
            row = session.execute(
                text("SELECT id,role FROM farm_membership WHERE identity_id=:id AND farm_id=:farm"),
                {"id": other_id, "farm": farm_id},
            ).one()
            assert row.role == "advisor"
            membership_id = row.id
        assert (
            browser.post(
                f"/farms/members/{membership_id}/revoke",
                params={"farm": farm_id},
                headers=headers,
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            browser.post(
                f"/farms/members/{membership_id}/revoke",
                params={"farm": farm_id},
                headers=headers,
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert (
            browser.post(
                "/farms/members",
                params={"farm": farm_id},
                data={"login": "other", "role": "advisor"},
                headers=headers,
                follow_redirects=False,
            ).status_code
            == 303
        )
        with committed_session_factory() as session:
            assert (
                session.scalar(
                    text("SELECT revoked_at FROM farm_membership WHERE id=:id"),
                    {"id": membership_id},
                )
                is None
            )
            assert (
                session.scalar(
                    text("SELECT count(*) FROM farm_membership WHERE farm_id=:farm"),
                    {"farm": other_farm},
                )
                == 1
            )


@pytest.mark.parametrize("path,field", [("/login", "password"), ("/admin/sign-in", "token")])
@pytest.mark.parametrize("accept", ["application/json", "text/html"])
def test_credential_validation_never_echoes_secrets_or_renders_private_shell(
    settings, session_factory, path, field, accept
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    secret = (
        "synthetic-invalid-credential-" * 30
    )  # gauntlet:allow-secret synthetic validation input
    with TestClient(
        create_app(settings, session_factory=session_factory), base_url="https://testserver"
    ) as browser:
        response = browser.post(
            path,
            data={"login": "farmer", field: secret},
            headers={"Accept": accept, "Origin": "https://testserver"},
        )
        assert response.status_code == 422
        assert "synthetic-invalid-credential" not in response.text
        assert "csrf_token" not in response.text or path == "/login"
        assert "/notifications" not in response.text
        assert "/farms/switcher" not in response.text
        assert response.headers["Cache-Control"] == "no-store"


def test_html_login_csrf_denial_uses_public_layout(settings, session_factory):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    with TestClient(
        create_app(settings, session_factory=session_factory), base_url="https://testserver"
    ) as browser:
        response = browser.post(
            "/login",
            data={"login": "farmer", "password": PASSWORD, "csrf_token": "wrong"},
            headers={"Accept": "text/html", "Origin": "https://testserver"},
        )
        assert response.status_code == 403
        assert "/notifications" not in response.text
        assert "/farms/switcher" not in response.text
        assert 'href="/login"' in response.text


def test_enabled_owner_can_revoke_disabled_co_owner_without_removing_last_enabled_owner(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _, farm_id = _account(committed_session_factory)
    disabled_id, _ = _account(committed_session_factory, login="disabled", farm_id=farm_id)
    with committed_session_factory.begin() as session:
        session.execute(
            text("UPDATE local_identity SET disabled_at=now() WHERE id=:id"), {"id": disabled_id}
        )
        disabled_membership = session.scalar(
            text("SELECT id FROM farm_membership WHERE identity_id=:id"), {"id": disabled_id}
        )
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        headers = {
            "Origin": "https://testserver",
            "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
        }
        # Account activation remains local-operator authority; owners only edit enabled targets.
        assert (
            browser.post(
                "/farms/members",
                params={"farm": farm_id},
                data={"login": "disabled", "role": "advisor"},
                headers=headers,
            ).status_code
            == 422
        )
        response = browser.post(
            f"/farms/members/{disabled_membership}/revoke",
            params={"farm": farm_id},
            headers=headers,
            follow_redirects=False,
        )
        assert response.status_code == 303
        with committed_session_factory() as session:
            assert (
                session.scalar(
                    text("SELECT revoked_at FROM farm_membership WHERE id=:id"),
                    {"id": disabled_membership},
                )
                is not None
            )
            assert (
                session.scalar(
                    text(
                        "SELECT count(*) FROM farm_membership m JOIN local_identity u "
                        "ON u.id=m.identity_id WHERE m.farm_id=:farm AND m.role='owner' "
                        "AND m.revoked_at IS NULL AND u.disabled_at IS NULL"
                    ),
                    {"farm": farm_id},
                )
                == 1
            )
        assert browser.get("/farms/members", params={"farm": farm_id}).status_code == 200


@pytest.mark.parametrize("membership_id", [0, -1, 2**63 - 1, 2**63, -(2**63) - 1, 2**100])
def test_membership_revoke_out_of_range_or_unknown_ids_are_generic_404(
    settings, committed_session_factory, membership_id
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    identity_id, farm_id = _account(committed_session_factory)
    with committed_session_factory() as session:
        own_membership = session.scalar(
            text("SELECT id FROM farm_membership WHERE identity_id=:id"), {"id": identity_id}
        )
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
        raise_server_exceptions=False,
    ) as browser:
        assert _login(browser).status_code == 303
        headers = {
            "Origin": "https://testserver",
            "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
        }
        response = browser.post(
            f"/farms/members/{membership_id}/revoke",
            params={"farm": farm_id},
            headers=headers,
            follow_redirects=False,
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "Membership not found."}
        with committed_session_factory() as session:
            assert (
                session.scalar(
                    text("SELECT count(*) FROM farm_membership WHERE revoked_at IS NULL")
                )
                == 1
            )
        # Invalid targets neither abort the next request nor evade last-owner protection.
        assert (
            browser.post(
                f"/farms/members/{own_membership}/revoke",
                params={"farm": farm_id},
                headers=headers,
                follow_redirects=False,
            ).status_code
            == 409
        )


@pytest.mark.parametrize("command", ["disable", "revoke-sessions", "set-password"])
def test_local_unknown_identity_recovery_fails_without_mutating_existing_account(
    settings, committed_session_factory, monkeypatch, tmp_path, command
):
    identity_id, _ = _account(committed_session_factory)
    with committed_session_factory() as session:
        before = session.execute(
            text("SELECT password_hash,disabled_at FROM local_identity WHERE id=:id"),
            {"id": identity_id},
        ).one()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        args = ["users", command, "unknown"]
        if command == "set-password":
            args.append("--password-stdin")
        result = CliRunner().invoke(cli, args, input=PASSWORD + "\n")
        assert result.exit_code != 0
        assert "No such local identity" in result.output
        assert PASSWORD not in result.output
        assert "scrypt$" not in result.output
        with committed_session_factory() as session:
            assert (
                session.execute(
                    text("SELECT password_hash,disabled_at FROM local_identity WHERE id=:id"),
                    {"id": identity_id},
                ).one()
                == before
            )
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    ("login", "password", "message"),
    [
        ("farmer", "short", "Use a password"),
        ("bad login", PASSWORD, "Use a login"),
    ],
)
def test_local_password_recovery_rejects_bad_input_without_replacing_credentials(
    settings, committed_session_factory, monkeypatch, tmp_path, login, password, message
):
    _account(committed_session_factory)
    with committed_session_factory() as session:
        before = session.scalar(text("SELECT password_hash FROM local_identity"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(
            cli, ["users", "set-password", login, "--password-stdin"], input=password + "\n"
        )
        assert result.exit_code != 0
        assert message in result.output
        assert PASSWORD not in result.output
        with committed_session_factory() as session:
            assert session.scalar(text("SELECT password_hash FROM local_identity")) == before
    finally:
        get_settings.cache_clear()


def test_malformed_canonical_login_fails_generically_without_creating_session(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        response = _login(browser, login="bad login")
        assert response.status_code == 401
        assert "Sign-in failed." in response.text
        assert not browser.cookies.get("farmcore_session")
        with committed_session_factory() as session:
            assert session.scalar(text("SELECT count(*) FROM local_session")) == 0


def test_enabled_identity_without_memberships_has_no_farm_access_but_can_logout(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    identity_id, _ = _account(committed_session_factory)
    with committed_session_factory.begin() as session:
        session.execute(
            text("DELETE FROM farm_membership WHERE identity_id=:id"), {"id": identity_id}
        )
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        denied = browser.get("/fields/")
        assert denied.status_code == 403
        assert "farmer private farm" not in denied.text
        assert browser.get("/farms/switcher").status_code == 403
        assert (
            browser.post(
                "/logout",
                headers={
                    "Origin": "https://testserver",
                    "X-CSRF-Token": browser.cookies.get("farmcore_csrf"),
                },
                follow_redirects=False,
            ).status_code
            == 303
        )


def test_operator_credential_cannot_impersonate_member_session_logout(settings, session_factory):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    with TestClient(
        create_app(settings, session_factory=session_factory),
        base_url="https://testserver",
        headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
    ) as browser:
        response = browser.post("/logout")
        assert response.status_code == 403
        assert response.json() == {"detail": "Authentication required."}


def test_member_farm_select_sets_only_authorized_farm_cookie_and_rejects_empty_selector(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.core.access import FARM_COOKIE
    from farmcore.main import create_app

    _, farm_id = _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        response = browser.get("/farms/select", params={"farm": farm_id}, follow_redirects=False)
        assert response.status_code == 303
        assert browser.cookies.get(FARM_COOKIE) == str(farm_id)
        for invalid in ("", "bad", str(farm_id + 1)):
            denied = browser.get("/farms/select", params={"farm": invalid}, follow_redirects=False)
            assert denied.status_code == 403
            assert browser.cookies.get(FARM_COOKIE) == str(farm_id)


def test_explicit_demo_read_never_grants_membership_administration(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.core.access import ensure_owner_grant
    from farmcore.main import create_app

    _, farm_id = _account(committed_session_factory)
    with committed_session_factory.begin() as session:
        ensure_owner_grant(session, session.get(Farm, farm_id))
    demo = settings.model_copy(update={"demo_mode": True, "demo_farm_ids": [farm_id]})
    with TestClient(
        create_app(demo, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert browser.get("/fields/", params={"farm": farm_id}).status_code == 200
        denied = browser.get("/farms/members", params={"farm": farm_id})
        assert denied.status_code == 403
        assert "farmer" not in denied.text


@pytest.mark.parametrize("explicit_selector", [True, False])
def test_farm_removed_between_session_resolution_and_read_denies_without_metadata(
    settings, committed_session_factory, explicit_selector
):
    import time
    from concurrent.futures import ThreadPoolExecutor

    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _, farm_id = _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
        raise_server_exceptions=False,
    ) as browser:
        assert _login(browser).status_code == 303
        with ThreadPoolExecutor(max_workers=1) as pool, committed_session_factory() as holder:
            holder.execute(text("LOCK TABLE farm IN ACCESS EXCLUSIVE MODE"))
            pending = pool.submit(
                browser.get, "/fields/", params={"farm": farm_id} if explicit_selector else {}
            )
            try:
                deadline = time.monotonic() + 5
                while True:
                    with committed_session_factory() as observer:
                        blocked = observer.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname=current_database() AND wait_event_type='Lock' "
                                "AND query ILIKE '%FROM farm%' AND pid<>pg_backend_pid()"
                            )
                        )
                    if blocked:
                        break
                    assert time.monotonic() < deadline, "Authorized farm lookup did not block"
                    time.sleep(0.01)
                # A real independent transaction removes the membership and its farm while
                # the HTTP request already holds its authenticated membership snapshot.
                holder.execute(
                    text("DELETE FROM farm_membership WHERE farm_id=:id"), {"id": farm_id}
                )
                holder.execute(text("DELETE FROM farm WHERE id=:id"), {"id": farm_id})
                holder.commit()
            finally:
                holder.rollback()
                response = pending.result(timeout=10)
            assert response.status_code == 403
            assert "farmer private farm" not in response.text
        assert browser.get("/fields/").status_code == 403


HTML = {"Accept": "text/html,application/xhtml+xml"}


def test_signed_out_browser_is_sent_to_login_and_returned_to_the_page(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        page = browser.get("/today/?view=week", headers=HTML, follow_redirects=False)
        assert page.status_code == 303
        assert page.headers["location"] == "/login?next=%2Ftoday%2F%3Fview%3Dweek"
        api = browser.get("/today/", follow_redirects=False)
        assert api.status_code == 403
        assert api.json() == {"detail": "Authentication required."}

        login = browser.get(page.headers["location"])
        assert 'name="next" value="/today/?view=week"' in login.text
        signed_in = _login(browser, next="/today/?view=week")
        assert signed_in.status_code == 303
        assert signed_in.headers["location"] == "/today/?view=week"


@pytest.mark.parametrize(
    "next_value",
    ["//evil.example/today/", "https://evil.example/", "/\\evil.example", "/\t/evil.example"],
)
def test_login_next_never_leaves_this_site(settings, committed_session_factory, next_value):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert 'name="next"' not in browser.get("/login", params={"next": next_value}).text
        response = _login(browser, next=next_value)
        assert response.status_code == 303
        assert response.headers["location"] == "/"


@pytest.mark.parametrize(
    ("path", "detail", "phrase"),
    [
        ("/today/?farm=999999", "Farm access denied.", "no access to that farm"),
        ("/apps", "Installation operator required.", "Only the installation operator"),
    ],
)
def test_member_refusal_is_a_page_for_a_browser_and_json_for_a_client(
    settings, committed_session_factory, path, detail, phrase
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _account(committed_session_factory, role="advisor")
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        page = browser.get(path, headers=HTML, follow_redirects=False)
        assert page.status_code == 403
        assert phrase in page.text
        assert 'href="/"' in page.text  # home, without the refused farm
        api = browser.get(path, follow_redirects=False)
        assert api.status_code == 403
        assert api.json() == {"detail": detail}


def test_signed_in_member_in_tour_gets_the_read_only_page_not_sign_in(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient

    from farmcore.main import create_app

    _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        browser.cookies.set("farmcore_tour", "1")
        page = browser.post("/logout", headers=HTML, follow_redirects=False)
        assert page.status_code == 403
        assert "read-only" in page.text
        api = browser.post("/logout", follow_redirects=False)
        assert api.status_code == 403
        assert api.json() == {"detail": "Tour mode is read-only."}


def test_share_grant_with_a_wrong_csrf_token_is_refused_and_writes_nothing(
    settings, committed_session_factory
):
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from farmcore.core.models.canonical import DataGrant
    from farmcore.main import create_app

    _, farm_id = _account(committed_session_factory)
    with TestClient(
        create_app(settings, session_factory=committed_session_factory),
        base_url="https://testserver",
    ) as browser:
        assert _login(browser).status_code == 303
        response = browser.post(
            f"/share/grants?farm={farm_id}",
            data={"csrf_token": "not-the-session-nonce", "recipient_login": "farmer"},
            headers={"Origin": "https://testserver"},
            follow_redirects=False,
        )
        assert response.status_code == 403
    with committed_session_factory() as session:
        count = session.scalar(
            select(func.count()).select_from(DataGrant).where(DataGrant.grantee != "owner")
        )
        assert count == 0

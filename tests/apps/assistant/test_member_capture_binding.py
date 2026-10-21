"""Authenticated original spray capture stays bound to the selected farm and principal."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from farmcore.apps.chat.handlers import ChatDeps
from farmcore.core.models.canonical import Log, LogSource, Parcel
from farmcore.main import create_app
from tests.apps.chat.test_handlers import fake, seed_farm
from tests.core.test_member_sessions import _account, _login


def member_browser(app, login):
    browser = TestClient(app, base_url="https://testserver")
    assert _login(browser, login=login).status_code == 303
    browser.headers.update(
        {"Origin": "https://testserver", "X-CSRF-Token": browser.cookies["farmcore_csrf"]}
    )
    return browser


def test_owners_capture_and_confirm_only_their_own_spray_drafts(
    settings, committed_session_factory
):
    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = session.get(Parcel, ids["P1"]).farm_id
    alice, _ = _account(committed_session_factory, login="alice", farm_id=farm_id)
    _account(committed_session_factory, login="bob", farm_id=farm_id)
    app = create_app(settings, session_factory=committed_session_factory)
    app.state.assistant_chat_deps = ChatDeps(committed_session_factory, fake(), settings)
    with member_browser(app, "alice") as owner, member_browser(app, "bob") as other:
        reply = owner.post(
            f"/assistant/message?farm={farm_id}",
            data={"q": "I sprayed P1 copper 2 kg this morning"},
        )
        assert reply.status_code == 200, reply.text
        draft = reply.json()
        assert draft["kind"] == "draft"
        action = draft["actions"][0]["action"]
        denied = other.post(f"/assistant/confirm?farm={farm_id}", data={"action": action})
        assert denied.status_code == 200
        assert denied.json()["kind"] == "reply"
        with committed_session_factory() as session:
            [pending] = session.scalars(select(Log)).all()
            assert pending.status == "pending"
        confirmed = owner.post(f"/assistant/confirm?farm={farm_id}", data={"action": action})
        assert confirmed.status_code == 200 and confirmed.json()["kind"] == "done"
        assert (
            owner.post(f"/assistant/confirm?farm={farm_id}", data={"action": action}).status_code
            == 200
        )
        with committed_session_factory() as session:
            [saved] = session.scalars(select(Log)).all()
            assert saved.status == "done" and saved.farm_id == farm_id
            [source] = session.scalars(select(LogSource)).all()
            assert source.external_key.startswith(f"web:farm:{farm_id}:identity:{alice}:")


def test_selected_private_farm_and_clarification_are_not_shared(
    settings, committed_session_factory
):
    from farmcore.apps.chat.extractor import RegexExtractor
    from farmcore.core.models.canonical import Farm
    from farmcore.core.models.identity import FarmMembership
    from tests.apps.chat.test_handlers import SQUARE

    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        default_id = session.get(Parcel, ids["P1"]).farm_id
    alice_id, _ = _account(committed_session_factory, login="alice", farm_id=default_id)
    _account(committed_session_factory, login="bob", farm_id=default_id)
    with committed_session_factory() as session:
        private = Farm(name="Alice second farm")
        session.add(private)
        session.flush()
        private_id = private.id
        session.add_all(
            [
                Parcel(farm_id=private_id, alias="P1", name="Private P1", area_ha=1, geom=SQUARE),
                FarmMembership(farm_id=private_id, identity_id=alice_id, role="owner"),
            ]
        )
        session.commit()
    app = create_app(settings, session_factory=committed_session_factory)
    app.state.assistant_chat_deps = ChatDeps(committed_session_factory, RegexExtractor(), settings)
    with member_browser(app, "alice") as alice, member_browser(app, "bob") as bob:
        first = alice.post(
            f"/assistant/message?farm={private_id}",
            data={"q": "I sprayed copper 2 kg this morning"},
        )
        assert first.status_code == 200 and first.json()["kind"] == "clarify"
        assert (
            bob.post(f"/assistant/message?farm={default_id}", data={"q": "P1"}).json().get("kind")
            != "draft"
        )
        assert (
            alice.post(f"/assistant/message?farm={default_id}", data={"q": "P1"}).json().get("kind")
            != "draft"
        )
        draft = alice.post(f"/assistant/message?farm={private_id}", data={"q": "P1"})
        assert draft.status_code == 200 and draft.json()["kind"] == "draft"
        assert "Private P1" in draft.json()["text"]
        action = draft.json()["actions"][0]["action"]
        assert (
            alice.post(f"/assistant/confirm?farm={default_id}", data={"action": action}).json()[
                "kind"
            ]
            == "reply"
        )
        assert (
            alice.post(f"/assistant/confirm?farm={private_id}", data={"action": action}).json()[
                "kind"
            ]
            == "done"
        )
        with committed_session_factory() as session:
            [saved] = session.scalars(select(Log)).all()
            assert saved.farm_id == private_id and saved.status == "done"


@pytest.mark.parametrize(
    # Revoked memberships and disabled identities share the session check's locked query; the
    # corrections race test covers each of them.
    "deny",
    ["advisor", "missing-csrf", "session", "foreign", "tour"],
)
def test_member_capture_and_confirmation_fail_closed(settings, committed_session_factory, deny):
    from datetime import UTC, datetime

    from sqlalchemy import update

    from farmcore.core.models.identity import LocalSession

    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = session.get(Parcel, ids["P1"]).farm_id
    identity_id, _ = _account(
        committed_session_factory,
        login="alice",
        farm_id=None if deny == "foreign" else farm_id,
        role="advisor" if deny == "advisor" else "owner",
    )
    app = create_app(settings, session_factory=committed_session_factory)
    app.state.assistant_chat_deps = ChatDeps(committed_session_factory, fake(), settings)
    with member_browser(app, "alice") as browser:
        if deny == "missing-csrf":
            del browser.headers["X-CSRF-Token"]
        elif deny == "tour":
            browser.cookies.set("farmcore_tour", "1")
        elif deny == "session":
            with committed_session_factory() as session:
                session.execute(
                    update(LocalSession)
                    .where(LocalSession.identity_id == identity_id)
                    .values(revoked_at=datetime.now(UTC))
                )
                session.commit()
        assert (
            browser.post(
                f"/assistant/message?farm={farm_id}",
                data={"q": "I sprayed P1 copper 2 kg this morning"},
            ).status_code
            == 403
        )
        assert (
            browser.post(
                f"/assistant/confirm?farm={farm_id}", data={"action": "confirm:1"}
            ).status_code
            == 403
        )
        assert browser.post(f"/assistant/transcribe?farm={farm_id}").status_code == 403
        with committed_session_factory() as session:
            assert session.scalars(select(Log)).all() == []


@pytest.mark.parametrize("unclear", [False, True])
def test_session_revoked_during_external_extraction_cannot_persist_capture(
    settings, committed_session_factory, unclear
):
    from concurrent.futures import ThreadPoolExecutor
    from datetime import UTC, datetime
    from threading import Event

    from sqlalchemy import update

    from farmcore.core.models.identity import LocalSession

    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = session.get(Parcel, ids["P1"]).farm_id
    identity_id, _ = _account(committed_session_factory, login="alice", farm_id=farm_id)
    entered, release = Event(), Event()
    reader = fake(dose_value=None, dose_unit=None) if unclear else fake()

    class ExternalReader:
        def extract(self, request):
            entered.set()
            assert release.wait(8), "Test failed to release external extractor boundary"
            return reader.extract(request)

    app = create_app(settings, session_factory=committed_session_factory)
    app.state.assistant_chat_deps = ChatDeps(committed_session_factory, ExternalReader(), settings)
    with member_browser(app, "alice") as browser, ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            browser.post,
            f"/assistant/message?farm={farm_id}",
            data={"q": "I sprayed P1 copper this morning"},
        )
        assert entered.wait(8), "HTTP capture never reached external extraction"
        with committed_session_factory() as session:
            session.execute(
                update(LocalSession)
                .where(LocalSession.identity_id == identity_id)
                .values(revoked_at=datetime.now(UTC))
            )
            session.commit()
        release.set()
        assert pending.result(timeout=10).status_code == 403
    with committed_session_factory() as session:
        assert session.scalars(select(Log)).all() == []
        assert session.scalars(select(LogSource)).all() == []


def test_member_cancel_and_concurrent_confirm_keep_each_principals_pending_ownership(
    settings, committed_session_factory
):
    from concurrent.futures import ThreadPoolExecutor

    from farmcore.core.models.canonical import Event, Quantity

    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = session.get(Parcel, ids["P1"]).farm_id
    _account(committed_session_factory, login="alice", farm_id=farm_id)
    bob_id, _ = _account(committed_session_factory, login="bob", farm_id=farm_id)
    app = create_app(settings, session_factory=committed_session_factory)
    app.state.assistant_chat_deps = ChatDeps(committed_session_factory, fake(), settings)
    with member_browser(app, "alice") as alice, member_browser(app, "bob") as bob:
        for browser in (alice, bob):
            reply = browser.post(
                f"/assistant/message?farm={farm_id}",
                data={"q": "I sprayed P1 copper 2 kg this morning"},
            )
            assert reply.status_code == 200 and reply.json()["kind"] == "draft"
        bob_action = reply.json()["actions"][0]["action"]
        assert (
            alice.post(f"/assistant/message?farm={farm_id}", data={"q": "cancel"}).json()["kind"]
            == "done"
        )
        with committed_session_factory() as session:
            [pending] = session.scalars(select(Log)).all()
            assert pending.status == "pending"
            assert pending.sources[0].external_key.startswith(
                f"web:farm:{farm_id}:identity:{bob_id}:"
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(
                pool.map(
                    lambda _: bob.post(
                        f"/assistant/confirm?farm={farm_id}", data={"action": bob_action}
                    ),
                    range(2),
                )
            )
        assert [r.status_code for r in replies] == [200, 200]
        with committed_session_factory() as session:
            [saved] = session.scalars(select(Log)).all()
            assert saved.status == "done" and saved.farm_id == farm_id
            assert (
                len(session.scalars(select(Quantity)).all()) == 2
            )  # original copper + sulphur exact multiplicity
            assert (
                len(session.scalars(select(Event).where(Event.kind == "log.confirmed")).all()) == 1
            )


@pytest.mark.parametrize(
    "viewer,path,capture",
    [
        ("owner", "/operations/", "default"),
        ("owner", "/operations/?farm={other}", "other"),
        ("advisor", "/operations/", None),
        ("owner-on-tour", "/operations/", None),
        ("operator", "/apps?farm={default}", None),  # a page that resolves no farm
    ],
)
def test_capture_controls_appear_only_for_an_owner_on_the_selected_farm(
    settings, committed_session_factory, viewer, path, capture
):
    from farmcore.core.models.canonical import Farm
    from farmcore.core.models.identity import FarmMembership

    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = session.get(Parcel, ids["P1"]).farm_id
    role = "advisor" if viewer == "advisor" else "owner"
    identity_id, _ = _account(committed_session_factory, login="alice", farm_id=farm_id, role=role)
    with committed_session_factory() as session:
        other = Farm(name="Second owned farm")
        session.add(other)
        session.flush()
        other_id = other.id
        session.add(FarmMembership(identity_id=identity_id, farm_id=other_id, role="owner"))
        session.commit()
    app = create_app(settings, session_factory=committed_session_factory)
    if viewer == "operator":
        browser = TestClient(
            app,
            base_url="https://testserver",
            headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
        )
    else:
        browser = member_browser(app, "alice")
    with browser:
        if viewer == "owner-on-tour":
            browser.cookies.set("farmcore_tour", "1")
        page = browser.get(path.format(default=farm_id, other=other_id))
    assert page.status_code == 200
    if capture is None:
        assert "data-message-url=" not in page.text and "data-confirm-url=" not in page.text
    else:
        selected = farm_id if capture == "default" else other_id
        assert f'data-message-url="/assistant/message?farm={selected}"' in page.text
        assert f'data-confirm-url="/assistant/confirm?farm={selected}"' in page.text


@pytest.mark.parametrize("action", ["confirm-button", "confirm-message"])
def test_default_farm_operator_recovers_original_shared_web_draft(
    settings, committed_session_factory, action
):
    from farmcore.apps.chat.dto import Incoming
    from farmcore.apps.chat.handlers import WEB_CHAT_ID, on_message

    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = session.get(Parcel, ids["P1"]).farm_id
    deps = ChatDeps(committed_session_factory, fake(), settings)
    old = on_message(
        deps, Incoming(WEB_CHAT_ID, "I sprayed P1 copper 2 kg this morning", None, 123)
    )
    app = create_app(settings, session_factory=committed_session_factory)
    app.state.assistant_chat_deps = deps
    with TestClient(
        app,
        base_url="https://testserver",
        headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
    ) as operator:
        if action.endswith("button"):
            button = old.buttons[0 if action.startswith("confirm") else 1][1]
            reply = operator.post(f"/assistant/confirm?farm={farm_id}", data={"action": button})
        else:
            reply = operator.post(
                f"/assistant/message?farm={farm_id}",
                data={"q": "/confirm" if action.startswith("confirm") else "cancel"},
            )
        assert reply.status_code == 200 and reply.json()["kind"] == "done", reply.text
    with committed_session_factory() as session:
        saved = session.scalars(select(Log)).all()
        if action.startswith("confirm"):
            assert len(saved) == 1 and saved[0].status == "done"
            assert saved[0].sources[0].external_key == "web:farmer:123"
        else:
            assert saved == []


@pytest.mark.parametrize("claimant", ["member-on-default-farm", "operator-on-other-farm"])
def test_original_shared_web_draft_is_claimable_only_by_the_default_farm_operator(
    settings, committed_session_factory, claimant
):
    from farmcore.apps.chat.dto import Incoming
    from farmcore.apps.chat.handlers import WEB_CHAT_ID, on_message
    from farmcore.core.models.canonical import Farm

    with committed_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = session.get(Parcel, ids["P1"]).farm_id
    deps = ChatDeps(committed_session_factory, fake(), settings)
    old = on_message(
        deps, Incoming(WEB_CHAT_ID, "I sprayed P1 copper 2 kg this morning", None, 123)
    )
    app = create_app(settings, session_factory=committed_session_factory)
    app.state.assistant_chat_deps = deps
    if claimant == "member-on-default-farm":
        _account(committed_session_factory, login="alice", farm_id=farm_id)
        browser = member_browser(app, "alice")
    else:
        with committed_session_factory() as session:
            other = Farm(name="Other farm")
            session.add(other)
            session.flush()
            [pending] = session.scalars(select(Log)).all()
            pending.farm_id = farm_id = other.id
            session.commit()
        browser = TestClient(
            app,
            base_url="https://testserver",
            headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
        )
    with browser:
        for _, action in old.buttons:
            reply = browser.post(f"/assistant/confirm?farm={farm_id}", data={"action": action})
            assert reply.status_code == 200 and reply.json()["kind"] == "reply"
    with committed_session_factory() as session:
        [pending] = session.scalars(select(Log)).all()
        assert pending.status == "pending" and pending.farm_id == farm_id
        assert pending.sources[0].external_key == "web:farmer:123"


def test_no_model_selected_farm_uses_local_capture_without_mutating_enabled_extractor(
    settings, committed_session_factory, monkeypatch
):
    import httpx
    from pydantic import SecretStr

    from farmcore.core.models.canonical import Farm
    from tests.apps.chat.test_handlers import SQUARE

    factory = committed_session_factory
    with factory() as s:
        ids = seed_farm(s, settings)
        default = s.get(Parcel, ids["P1"]).farm_id
        private = Farm(name="ProtectedPrivateFarm", props={"no_model": True})
        s.add(private)
        s.flush()
        fid = private.id
        from farmcore.core.access import ensure_owner_grant

        ensure_owner_grant(s, private)
        s.add(
            Parcel(farm_id=fid, alias="P1", name="ProtectedPlotSentinel913", area_ha=1, geom=SQUARE)
        )
        s.commit()
    _account(factory, login="protected-owner", farm_id=fid)
    config = settings.model_copy(
        update={
            "extractor": "vertex",
            "vertex_project": "qa-synthetic-project",
            "vertex_auth": "token",
            "vertex_access_token": SecretStr("synthetic-no-real-provider-key"),
        }
    )
    sent = []

    def external_transport(self, request):
        sent.append({"url": str(request.url), "payload": request.content.decode()})
        return httpx.Response(
            503, json={"error": "controlled external provider unavailable"}, request=request
        )

    # Official external HTTP transport boundary only; real app, handlers, extractors and DB intact.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", external_transport)
    app = create_app(config, session_factory=factory)
    with TestClient(app, base_url="https://testserver") as operator:
        operator.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        control = operator.post(
            f"/assistant/message?farm={default}",
            data={"q": "I sprayed P1 copper 2 kg this morning"},
        )
        assert control.status_code == 200, control.text
        assert len(sent) == 1, "External adapter positive control did not run"
    sent.clear()
    with TestClient(app, base_url="https://testserver") as member:
        assert _login(member, login="protected-owner").status_code == 303
        member.headers.update(
            {"Origin": "https://testserver", "X-CSRF-Token": member.cookies["farmcore_csrf"]}
        )
        first = member.post(
            f"/assistant/message?farm={fid}", data={"q": "I sprayed copper 2 kg this morning"}
        )
        assert first.status_code == 200 and first.json()["kind"] == "clarify"
        assert sent == [], "Clarification sent protected context externally"
        r = member.post(f"/assistant/message?farm={fid}", data={"q": "P1"})
        assert r.status_code == 200, r.text
        restricted = list(sent)
        assert r.json()["kind"] == "draft", r.text
        action = r.json()["actions"][0]["action"]
        confirmed = member.post(f"/assistant/confirm?farm={fid}", data={"action": action})
        assert confirmed.status_code == 200 and confirmed.json()["kind"] == "done", confirmed.text
        assert sent == restricted, "Confirmation unexpectedly called extractor"
    sent.clear()
    with TestClient(app, base_url="https://testserver") as operator:
        operator.headers["X-Admin-Token"] = settings.admin_token.get_secret_value()
        control = operator.post(
            f"/assistant/message?farm={default}",
            data={"q": "I sprayed P1 copper 2 kg this morning"},
        )
        assert control.status_code == 200 and len(sent) == 1, (
            "Enabled farm adapter failed after restricted-farm capture"
        )
    assert restricted == []


def test_selected_farm_deleted_during_external_extraction_cannot_write(
    settings, committed_session_factory
):
    from sqlalchemy import text

    from farmcore.core.models.canonical import Farm
    from tests.apps.chat.test_handlers import SQUARE

    with committed_session_factory() as session:
        seed_farm(session, settings)
        private = Farm(name="Removed during extraction")
        session.add(private)
        session.flush()
        farm_id = private.id
        session.add(Parcel(farm_id=farm_id, alias="P1", name="Private P1", area_ha=1, geom=SQUARE))
        session.commit()
    _account(committed_session_factory, login="alice", farm_id=farm_id)
    reader = fake()

    class ExternalReader:
        def extract(self, request):
            # Actual external-state race after the handler's read transaction closed.
            with committed_session_factory.begin() as session:
                for table in ("access_log", "data_grant", "parcel", "farm_membership", "farm"):
                    column = "id" if table == "farm" else "farm_id"
                    session.execute(
                        text(f"DELETE FROM {table} WHERE {column}=:id"), {"id": farm_id}
                    )
            return reader.extract(request)

    app = create_app(settings, session_factory=committed_session_factory)
    app.state.assistant_chat_deps = ChatDeps(committed_session_factory, ExternalReader(), settings)
    with member_browser(app, "alice") as browser:
        reply = browser.post(
            f"/assistant/message?farm={farm_id}",
            data={"q": "I sprayed P1 copper 2 kg this morning"},
        )
        assert reply.status_code == 403
    with committed_session_factory() as session:
        assert session.get(Farm, farm_id) is None
        assert session.scalars(select(Log)).all() == []
        assert session.scalars(select(LogSource)).all() == []

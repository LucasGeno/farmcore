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


def test_owners_cancel_and_confirm_only_their_own_drafts_and_a_double_confirm_logs_once(
    settings, pooled_session_factory
):
    from concurrent.futures import ThreadPoolExecutor

    from farmcore.core.models.canonical import Event

    with pooled_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = session.get(Parcel, ids["P1"]).farm_id
    _account(pooled_session_factory, login="alice", farm_id=farm_id)
    bob_id, _ = _account(pooled_session_factory, login="bob", farm_id=farm_id)
    app = create_app(settings, session_factory=pooled_session_factory)
    app.state.assistant_chat_deps = ChatDeps(pooled_session_factory, fake(), settings)
    with member_browser(app, "alice") as alice, member_browser(app, "bob") as bob:
        actions = {}
        for name, browser in (("alice", alice), ("bob", bob)):
            reply = browser.post(
                f"/assistant/message?farm={farm_id}",
                data={"q": "I sprayed P1 copper 2 kg this morning"},
            )
            assert reply.status_code == 200 and reply.json()["kind"] == "draft", reply.text
            actions[name] = reply.json()["actions"][0]["action"]
        denied = bob.post(f"/assistant/confirm?farm={farm_id}", data={"action": actions["alice"]})
        assert denied.status_code == 200 and denied.json()["kind"] == "reply"
        cancelled = alice.post(f"/assistant/message?farm={farm_id}", data={"q": "cancel"})
        assert cancelled.json()["kind"] == "done"
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(
                pool.map(
                    lambda _: bob.post(
                        f"/assistant/confirm?farm={farm_id}", data={"action": actions["bob"]}
                    ),
                    range(2),
                )
            )
        assert [r.status_code for r in replies] == [200, 200]
    with pooled_session_factory() as session:
        [saved] = session.scalars(select(Log)).all()  # alice's draft is gone, bob's is logged
        assert saved.status == "done" and saved.farm_id == farm_id
        assert saved.sources[0].external_key.startswith(f"web:farm:{farm_id}:identity:{bob_id}:")
        assert len(session.scalars(select(Event).where(Event.kind == "log.confirmed")).all()) == 1


def test_selected_private_farm_and_clarification_are_not_shared(settings, pooled_session_factory):
    from farmcore.apps.chat.extractor import RegexExtractor
    from farmcore.core.models.canonical import Farm
    from farmcore.core.models.identity import FarmMembership
    from tests.apps.chat.test_handlers import SQUARE

    with pooled_session_factory() as session:
        ids = seed_farm(session, settings)
        default_id = session.get(Parcel, ids["P1"]).farm_id
    alice_id, _ = _account(pooled_session_factory, login="alice", farm_id=default_id)
    _account(pooled_session_factory, login="bob", farm_id=default_id)
    with pooled_session_factory() as session:
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
    app = create_app(settings, session_factory=pooled_session_factory)
    app.state.assistant_chat_deps = ChatDeps(pooled_session_factory, RegexExtractor(), settings)
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
        with pooled_session_factory() as session:
            [saved] = session.scalars(select(Log)).all()
            assert saved.farm_id == private_id and saved.status == "done"


@pytest.mark.parametrize(
    # Revoked memberships and disabled identities share the session check's locked query; the
    # corrections race test covers each of them.
    "deny",
    ["advisor", "missing-csrf", "session", "foreign", "tour"],
)
def test_member_capture_and_confirmation_fail_closed(settings, pooled_session_factory, deny):
    from datetime import UTC, datetime

    from sqlalchemy import update

    from farmcore.core.models.identity import LocalSession

    with pooled_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = session.get(Parcel, ids["P1"]).farm_id
    identity_id, _ = _account(
        pooled_session_factory,
        login="alice",
        farm_id=None if deny == "foreign" else farm_id,
        role="advisor" if deny == "advisor" else "owner",
    )
    app = create_app(settings, session_factory=pooled_session_factory)
    app.state.assistant_chat_deps = ChatDeps(pooled_session_factory, fake(), settings)
    with member_browser(app, "alice") as browser:
        if deny == "missing-csrf":
            del browser.headers["X-CSRF-Token"]
        elif deny == "tour":
            browser.cookies.set("farmcore_tour", "1")
        elif deny == "session":
            with pooled_session_factory() as session:
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
        with pooled_session_factory() as session:
            assert session.scalars(select(Log)).all() == []


@pytest.mark.parametrize("race", ["session-revoked", "session-revoked-on-clarify", "farm-deleted"])
def test_authority_lost_during_external_extraction_writes_nothing(
    settings, pooled_session_factory, race
):
    """The handler closes its read transaction before the model call; whatever changes while
    the model runs is rechecked under the farm lock before anything is written."""
    from datetime import UTC, datetime

    from sqlalchemy import text, update

    from farmcore.core.models.canonical import Farm
    from farmcore.core.models.identity import LocalSession
    from tests.apps.chat.test_handlers import SQUARE

    with pooled_session_factory() as session:
        seed_farm(session, settings)
        private = Farm(name="Alice's farm")
        session.add(private)
        session.flush()
        farm_id = private.id
        session.add(Parcel(farm_id=farm_id, alias="P1", name="Private P1", area_ha=1, geom=SQUARE))
        session.commit()
    identity_id, _ = _account(pooled_session_factory, login="alice", farm_id=farm_id)
    reader = fake(dose_value=None, dose_unit=None) if race.endswith("clarify") else fake()

    class ExternalReader:
        def extract(self, request):
            with pooled_session_factory.begin() as session:
                if race == "farm-deleted":
                    for table in ("access_log", "data_grant", "parcel", "farm_membership", "farm"):
                        column = "id" if table == "farm" else "farm_id"
                        session.execute(
                            text(f"DELETE FROM {table} WHERE {column}=:id"), {"id": farm_id}
                        )
                else:
                    session.execute(
                        update(LocalSession)
                        .where(LocalSession.identity_id == identity_id)
                        .values(revoked_at=datetime.now(UTC))
                    )
            return reader.extract(request)

    app = create_app(settings, session_factory=pooled_session_factory)
    app.state.assistant_chat_deps = ChatDeps(pooled_session_factory, ExternalReader(), settings)
    with member_browser(app, "alice") as browser:
        reply = browser.post(
            f"/assistant/message?farm={farm_id}", data={"q": "I sprayed P1 copper this morning"}
        )
        assert reply.status_code == 403
    with pooled_session_factory() as session:
        assert session.scalars(select(Log)).all() == []
        assert session.scalars(select(LogSource)).all() == []


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
    settings, pooled_session_factory, viewer, path, capture
):
    from farmcore.core.models.canonical import Farm
    from farmcore.core.models.identity import FarmMembership

    with pooled_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = session.get(Parcel, ids["P1"]).farm_id
    role = "advisor" if viewer == "advisor" else "owner"
    identity_id, _ = _account(pooled_session_factory, login="alice", farm_id=farm_id, role=role)
    with pooled_session_factory() as session:
        other = Farm(name="Second owned farm")
        session.add(other)
        session.flush()
        other_id = other.id
        session.add(FarmMembership(identity_id=identity_id, farm_id=other_id, role="owner"))
        session.commit()
    app = create_app(settings, session_factory=pooled_session_factory)
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


@pytest.mark.parametrize(
    "claimant,via,claimed",
    [
        ("operator", "button", True),
        ("operator", "message", True),
        ("member", "button", False),
        ("operator-on-other-farm", "button", False),
    ],
)
def test_original_shared_web_draft_is_claimable_only_by_the_default_farm_operator(
    settings, pooled_session_factory, claimant, via, claimed
):
    from farmcore.apps.chat.dto import Incoming
    from farmcore.apps.chat.handlers import WEB_CHAT_ID, on_message
    from farmcore.core.models.canonical import Farm

    with pooled_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = session.get(Parcel, ids["P1"]).farm_id
    deps = ChatDeps(pooled_session_factory, fake(), settings)
    old = on_message(
        deps, Incoming(WEB_CHAT_ID, "I sprayed P1 copper 2 kg this morning", None, 123)
    )
    if claimant == "operator-on-other-farm":
        with pooled_session_factory() as session:
            other = Farm(name="Other farm")
            session.add(other)
            session.flush()
            [pending] = session.scalars(select(Log)).all()
            pending.farm_id = farm_id = other.id
            session.commit()
    app = create_app(settings, session_factory=pooled_session_factory)
    app.state.assistant_chat_deps = deps
    if claimant == "member":
        _account(pooled_session_factory, login="alice", farm_id=farm_id)
        browser = member_browser(app, "alice")
    else:
        token = settings.admin_token.get_secret_value()
        browser = TestClient(app, base_url="https://testserver", headers={"X-Admin-Token": token})
    with browser:
        if via == "message":
            reply = browser.post(f"/assistant/message?farm={farm_id}", data={"q": "/confirm"})
        else:
            action = old.buttons[0][1]
            reply = browser.post(f"/assistant/confirm?farm={farm_id}", data={"action": action})
        assert reply.status_code == 200, reply.text
        assert reply.json()["kind"] == ("done" if claimed else "reply")
    with pooled_session_factory() as session:
        [draft] = session.scalars(select(Log)).all()
        assert draft.status == ("done" if claimed else "pending") and draft.farm_id == farm_id
        assert draft.sources[0].external_key == "web:farmer:123"


def test_no_model_selected_farm_uses_local_capture_without_mutating_enabled_extractor(
    settings, pooled_session_factory, monkeypatch
):
    import httpx
    from pydantic import SecretStr

    from farmcore.core.models.canonical import Farm
    from tests.apps.chat.test_handlers import SQUARE

    factory = pooled_session_factory
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


def test_signed_in_quota_follows_the_identity_not_the_browser_cookie(
    settings, pooled_session_factory
):
    """Members' questions and captures can reach the external extractor: clearing the cookie
    must not reset their quota."""
    with pooled_session_factory() as session:
        ids = seed_farm(session, settings)
        farm_id = session.get(Parcel, ids["P1"]).farm_id
    _account(pooled_session_factory, login="alice", farm_id=farm_id)
    app = create_app(settings, session_factory=pooled_session_factory)
    with member_browser(app, "alice") as browser:
        for _ in range(10):
            assert browser.get(f"/assistant/ask?q=hello&farm={farm_id}").status_code == 200
        browser.cookies.delete("farmcore_assistant_browser")
        limited = browser.get(f"/assistant/ask?q=hello&farm={farm_id}")
        assert limited.status_code == 429

"""A farmer asks an assigned advisor about one exact stored assessment, through real HTTP.

The review is an opinion about a saved row: it carries the reviewer and the farm clock time,
and it never edits the advice row or follows a newer evaluation.
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from farmcore.core.clock import set_replay
from farmcore.core.models.canonical import Advice, Farm
from farmcore.main import create_app
from tests.apps.advisor.review_fixtures import complete_station
from tests.apps.share.conftest import make_parcel
from tests.core.test_member_sessions import _account, _login

NOW = datetime(2024, 4, 10, 4, tzinfo=UTC)


def _registered(browser, farm_id):
    return {"Origin": "https://testserver", "X-CSRF-Token": browser.cookies.get("farmcore_csrf")}


def _farm_with_assessment(
    factory, *, alias="P1", decision="wait", props=None, owner="owner", advisor="expert"
):
    """A farm whose only saved assessment is at `NOW` (the replay clock)."""
    _, farm_id = _account(factory, login=owner)
    _account(factory, login=advisor, role="advisor", farm_id=farm_id)
    with factory.begin() as session:
        farm = session.get(Farm, farm_id)
        complete_station(session, farm_id, NOW)
        parcel = make_parcel(session, farm, 1)
        parcel.alias = alias
        row = Advice(
            farm_id=farm_id,
            parcel_id=parcel.id,
            evaluated_at=NOW,
            decision=decision,
            reasons=[],
            products=[],
            props=props or {"headline": "Stored check", "assessment_status": "evaluable"},
        )
        session.add(row)
        session.flush()
        set_replay(session, NOW)
        return farm_id, parcel.id, row.id


def _state(browser, farm_id, advice_id):
    return browser.get(f"/review/assessments/{advice_id}.json", params={"farm": farm_id})


def test_owner_requests_and_assigned_advisor_answers_the_exact_stored_assessment(
    committed_session_factory, settings
):
    farm_id, parcel_id, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        params = {"farm": farm_id}

        requested = owner.post(
            f"/review/assessments/{advice_id}/request",
            params=params,
            json={"note": "Is the spray window right?"},
            headers=_registered(owner, farm_id),
        )
        assert requested.status_code == 201
        request_id = requested.json()["request"]["id"]
        assert requested.json()["request"]["note"] == "Is the spray window right?"
        assert requested.json()["open"] is True
        assert requested.json()["response"] is None
        pending = advisor.get("/review/recommendations/", params=params)
        assert pending.status_code == 200
        assert "P1" in pending.text
        assert "Is the spray window right?" in pending.text

        # The owner asked; only an assigned advisor answers.
        assert (
            owner.post(
                f"/review/requests/{request_id}/response",
                params=params,
                json={"opinion": "agree", "note": "Looks right"},
                headers=_registered(owner, farm_id),
            ).status_code
            == 403
        )
        answered = advisor.post(
            f"/review/requests/{request_id}/response",
            params=params,
            json={"opinion": "agree", "note": "Window looks right for P1."},
            headers=_registered(advisor, farm_id),
        )
        assert answered.status_code == 201
        body = answered.json()
        assert body["open"] is False
        assert body["response"]["reviewer"] == "expert"
        assert datetime.fromisoformat(body["response"]["reviewed_at"]) == NOW
        assert body["response"]["opinion"] == "agree"
        assert body["response"]["note"] == "Window looks right for P1."
        assert body["request"]["requested_by"] == "owner"

        seen = _state(owner, farm_id, advice_id)
        assert seen.status_code == 200
        assert seen.json()["response"]["reviewer"] == "expert"
        assert seen.json()["response"]["reviewed_at"] == NOW.isoformat()
        assert seen.json()["parcel"]["alias"] == "P1"
        card = owner.get(f"/advisor/parcels/{parcel_id}", params=params)
        assert card.status_code == 200
        assert f"/review/assessments/{advice_id}/card" in card.text
        check = owner.get(f"/review/assessments/{advice_id}/card", params=params)
        assert check.status_code == 200
        assert "expert" in check.text
        assert "Window looks right for P1." in check.text

        # One answer per request, and no second request while one is open (answered here).
        assert (
            advisor.post(
                f"/review/requests/{request_id}/response",
                params=params,
                json={"opinion": "disagree", "note": "Changed my mind"},
                headers=_registered(advisor, farm_id),
            ).status_code
            == 409
        )

    with committed_session_factory() as session:
        row = session.get(Advice, advice_id)
        assert row.decision == "wait"
        assert row.reasons == []
        assert row.products == []
        assert row.props == {"headline": "Stored check", "assessment_status": "evaluable"}
        assert session.query(Advice).filter(Advice.farm_id == farm_id).count() == 1


def _request_check(browser, farm_id, advice_id, note=""):
    return browser.post(
        f"/review/assessments/{advice_id}/request",
        params={"farm": farm_id},
        json={"note": note},
        headers=_registered(browser, farm_id),
    )


def _answer(browser, farm_id, request_id, note="Checked", opinion="agree"):
    return browser.post(
        f"/review/requests/{request_id}/response",
        params={"farm": farm_id},
        json={"opinion": opinion, "note": note},
        headers=_registered(browser, farm_id),
    )


def test_only_the_owner_asks_and_only_an_assigned_advisor_answers(
    committed_session_factory, settings
):
    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    _account(committed_session_factory, login="neighbour", role="advisor")
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as advisor,
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as neighbour,
        TestClient(app, base_url="https://testserver") as visitor,
    ):
        for browser, login in ((advisor, "expert"), (owner, "owner"), (neighbour, "neighbour")):
            assert _login(browser, login=login).status_code == 303
        # An advisor cannot ask on the owner's behalf; the owner cannot answer their own request.
        assert _request_check(advisor, farm_id, advice_id).status_code == 403
        asked = _request_check(owner, farm_id, advice_id, "Check please")
        assert asked.status_code == 201
        request_id = asked.json()["request"]["id"]
        assert _answer(owner, farm_id, request_id).status_code == 403
        # An advisor assigned elsewhere has no authority on this farm.
        assert _answer(neighbour, farm_id, request_id).status_code == 403
        assert (
            neighbour.get(
                f"/review/assessments/{advice_id}.json", params={"farm": farm_id}
            ).status_code
            == 403
        )
        # No member session: no review metadata at all.
        assert (
            visitor.get(
                f"/review/assessments/{advice_id}.json", params={"farm": farm_id}
            ).status_code
            == 403
        )
        assert (
            visitor.get(
                f"/review/assessments/{advice_id}/card", params={"farm": farm_id}
            ).status_code
            == 403
        )


def test_review_is_bound_to_this_farm_and_never_reveals_a_foreign_request(
    committed_session_factory, settings
):
    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    other_id, _, other_advice = _farm_with_assessment(
        committed_session_factory, owner="other-owner", advisor="other-advisor"
    )
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as other,
        TestClient(app, base_url="https://testserver") as other_advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(other, login="other-owner").status_code == 303
        assert _login(other_advisor, login="other-advisor").status_code == 303
        foreign = _request_check(other, other_id, other_advice, "Foreign note")
        assert foreign.status_code == 201
        foreign_request = foreign.json()["request"]["id"]
        # The same numeric id on the owner's farm: scoped, not found.
        assert (
            owner.get(
                f"/review/assessments/{other_advice}.json", params={"farm": farm_id}
            ).status_code
            == 404
        )
        assert (
            owner.post(
                f"/review/assessments/{other_advice}/request",
                params={"farm": farm_id},
                json={"note": ""},
                headers=_registered(owner, farm_id),
            ).status_code
            == 404
        )
        # The foreign advisor cannot answer through the owner's farm, and vice versa.
        assert _answer(other_advisor, farm_id, foreign_request).status_code == 403
        assert (
            owner.get("/review/recommendations/", params={"farm": farm_id}).text.count(
                "Foreign note"
            )
            == 0
        )
        assert (
            other.get(f"/review/assessments/{other_advice}.json", params={"farm": other_id}).json()[
                "request"
            ]["note"]
            == "Foreign note"
        )


def test_a_future_or_unknown_assessment_cannot_be_reviewed(committed_session_factory, settings):
    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    with committed_session_factory.begin() as session:
        from farmcore.core.models.canonical import Advice

        row = session.get(Advice, advice_id)
        future = Advice(
            farm_id=farm_id,
            parcel_id=row.parcel_id,
            evaluated_at=datetime(2024, 4, 11, 4, tzinfo=UTC),
            decision="spray",
            props={"headline": "Future"},
        )
        session.add(future)
        session.flush()
        future_id = future.id
    app = create_app(settings, session_factory=committed_session_factory)
    with TestClient(app, base_url="https://testserver") as owner:
        assert _login(owner, login="owner").status_code == 303
        assert _request_check(owner, farm_id, future_id).status_code == 404
        assert _request_check(owner, farm_id, 2**63).status_code == 404
        assert (
            owner.get(f"/review/assessments/{future_id}.json", params={"farm": farm_id}).status_code
            == 404
        )


def test_one_open_request_per_assessment_and_blank_answers_are_rejected(
    committed_session_factory, settings
):
    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        first = _request_check(owner, farm_id, advice_id)
        assert first.status_code == 201
        assert _request_check(owner, farm_id, advice_id).status_code == 409
        request_id = first.json()["request"]["id"]
        for payload in (
            {"opinion": "maybe", "note": "Checked"},
            {"opinion": "agree", "note": "   "},
            {"opinion": "agree", "note": "Checked", "extra": True},
        ):
            assert (
                advisor.post(
                    f"/review/requests/{request_id}/response",
                    params={"farm": farm_id},
                    json=payload,
                    headers=_registered(advisor, farm_id),
                ).status_code
                == 422
            )
        assert _answer(advisor, farm_id, request_id, "Final answer").status_code == 201
        # The check is final: no second answer, and a new request is allowed after an answer.
        assert _answer(advisor, farm_id, request_id, "Second answer").status_code == 409
        assert _request_check(owner, farm_id, advice_id, "Recheck please").status_code == 201


def test_member_cookie_mutations_need_origin_and_the_server_nonce(
    committed_session_factory, settings
):
    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with TestClient(app, base_url="https://testserver") as owner:
        assert _login(owner, login="owner").status_code == 303
        good = _registered(owner, farm_id)
        assert (
            owner.post(
                f"/review/assessments/{advice_id}/request",
                params={"farm": farm_id},
                json={"note": ""},
            ).status_code
            == 403
        )
        assert (
            owner.post(
                f"/review/assessments/{advice_id}/request",
                params={"farm": farm_id},
                json={"note": ""},
                headers={**good, "Origin": "https://foreign.invalid"},
            ).status_code
            == 403
        )
        assert (
            owner.post(
                f"/review/assessments/{advice_id}/request",
                params={"farm": farm_id},
                json={"note": ""},
                headers={"Origin": "https://testserver", "X-CSRF-Token": "forged"},
            ).status_code
            == 403
        )


def test_revoking_the_advisor_takes_effect_on_the_next_answer(committed_session_factory, settings):
    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        request_id = _request_check(owner, farm_id, advice_id).json()["request"]["id"]
        with committed_session_factory.begin() as session:
            from farmcore.core.models.identity import FarmMembership

            membership = (
                session.query(FarmMembership)
                .filter(FarmMembership.farm_id == farm_id, FarmMembership.role == "advisor")
                .one()
            )
            membership.revoked_at = NOW
        assert _answer(advisor, farm_id, request_id).status_code == 403
        with committed_session_factory() as session:
            from farmcore.core.models.canonical import Event

            assert session.query(Event).filter(Event.kind == "advice.reviewed").count() == 0


def test_concurrent_answers_leave_one_final_review(committed_session_factory, settings):
    from concurrent.futures import ThreadPoolExecutor

    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as first,
        TestClient(app, base_url="https://testserver") as second,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(first, login="expert").status_code == 303
        # A second device for the same advisor (the same account and membership).
        second.cookies.update(first.cookies)
        request_id = _request_check(owner, farm_id, advice_id).json()["request"]["id"]

        def answer(browser):
            return _answer(browser, farm_id, request_id, "Concurrent opinion")

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(answer, (first, second)))
        assert sorted(r.status_code for r in responses) == [201, 409]
    with committed_session_factory() as session:
        from farmcore.core.models.canonical import Event

        events = (
            session.query(Event)
            .filter(
                Event.res_id == advice_id,
                Event.kind.in_(("advice.review_requested", "advice.reviewed")),
            )
            .all()
        )
        assert [e.kind for e in events] == ["advice.review_requested", "advice.reviewed"]
        assert len({e.id for e in events}) == 2


def test_a_review_never_follows_a_newer_assessment(committed_session_factory, settings):
    farm_id, parcel_id, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        request_id = _request_check(owner, farm_id, advice_id, "First check").json()["request"][
            "id"
        ]
        assert _answer(advisor, farm_id, request_id, "First answer").status_code == 201
        later = datetime(2024, 4, 11, 4, tzinfo=UTC)
        with committed_session_factory.begin() as session:
            from farmcore.core.clock import set_replay
            from farmcore.core.models.canonical import Advice

            newer = Advice(
                farm_id=farm_id,
                parcel_id=parcel_id,
                evaluated_at=later,
                decision="spray",
                reasons=[],
                products=[],
                props={"headline": "Newer check", "assessment_status": "evaluable"},
            )
            session.add(newer)
            session.flush()
            newer_id = newer.id
            set_replay(session, later)
        newer_state = owner.get(
            f"/review/assessments/{newer_id}.json", params={"farm": farm_id}
        ).json()
        assert newer_state["request"] is None
        assert newer_state["response"] is None
        assert newer_state["current"] is True
        older_state = owner.get(
            f"/review/assessments/{advice_id}.json", params={"farm": farm_id}
        ).json()
        assert older_state["response"]["note"] == "First answer"
        assert older_state["current"] is False
        listed = owner.get("/review/recommendations.json", params={"farm": farm_id}).json()
        assert [r["advice_id"] for r in listed["reviews"]] == [advice_id]


def test_the_review_list_puts_the_newest_request_first(committed_session_factory, settings):
    farm_id, _, first_advice = _farm_with_assessment(committed_session_factory)
    with committed_session_factory.begin() as session:
        from farmcore.core.models.canonical import Farm
        from tests.apps.share.conftest import make_parcel

        farm = session.get(Farm, farm_id)
        parcel = make_parcel(session, farm, 2)
        second = Advice(
            farm_id=farm_id,
            parcel_id=parcel.id,
            evaluated_at=NOW,
            decision="none",
            reasons=[],
            products=[],
            props={"headline": "Second stored check"},
        )
        session.add(second)
        session.flush()
        second_advice = second.id
    app = create_app(settings, session_factory=committed_session_factory)
    with TestClient(app, base_url="https://testserver") as owner:
        assert _login(owner, login="owner").status_code == 303
        assert _request_check(owner, farm_id, first_advice).status_code == 201
        assert _request_check(owner, farm_id, second_advice).status_code == 201
        listed = owner.get("/review/recommendations.json", params={"farm": farm_id}).json()
        assert [r["advice_id"] for r in listed["reviews"]] == [second_advice, first_advice]


def test_review_events_are_bounded_by_the_farm_clock(committed_session_factory, settings):
    """Rewinding the clock hides later requests/answers and a later open request cannot block."""
    from datetime import timedelta

    from farmcore.apps.review import recommendations
    from farmcore.core.access import ScopedRepository, ensure_owner_grant
    from farmcore.core.models.canonical import Farm

    farm_id, _, first_advice = _farm_with_assessment(committed_session_factory)
    with committed_session_factory.begin() as session:
        from tests.apps.share.conftest import make_parcel

        farm = session.get(Farm, farm_id)
        parcel = make_parcel(session, farm, 2)
        second = Advice(
            farm_id=farm_id,
            parcel_id=parcel.id,
            evaluated_at=NOW,
            decision="none",
            reasons=[],
            products=[],
            props={"headline": "Second stored check"},
        )
        session.add(second)
        session.flush()
        second_advice = second.id
        first = session.get(Advice, first_advice)
        first_parcel = session.get(type(parcel), first.parcel_id)
        repo = ScopedRepository(
            session, farm_id, ensure_owner_grant(session, farm), "owner", "test"
        )
        mid = NOW + timedelta(minutes=30)
        later = NOW + timedelta(hours=2)
        seen = recommendations.request_check(
            repo, mid, first, first_parcel, note="Mid request", requested_by="owner"
        )
        recommendations.respond(
            repo,
            later,
            first,
            first_parcel,
            seen.request.id,
            opinion="agree",
            note="Later answer",
            reviewer="expert",
        )
        future = recommendations.request_check(
            repo,
            later,
            second,
            parcel,
            note="Future request",
            requested_by="owner",
        )
        assert future.open
    app = create_app(settings, session_factory=committed_session_factory)
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        with committed_session_factory.begin() as session:
            set_replay(session, NOW + timedelta(hours=1))
        first_state = _state(owner, farm_id, first_advice).json()
        assert first_state["request"]["note"] == "Mid request"
        assert first_state["response"] is None  # the later answer is hidden
        second_state = _state(owner, farm_id, second_advice).json()
        assert second_state["request"] is None  # the future request is hidden
        # A later open request must not block a request made at this clock time.
        assert _request_check(owner, farm_id, second_advice, "Present request").status_code == 201
        with committed_session_factory() as session:
            from farmcore.core.models.canonical import Event

            assert (
                session.query(Event)
                .filter(Event.kind.in_(("advice.review_requested", "advice.reviewed")))
                .count()
                == 4
            )


def test_review_validation_and_form_error_branches(committed_session_factory, settings):
    from datetime import timedelta

    from sqlalchemy import select

    from farmcore.core.clock import set_replay
    from farmcore.core.models.canonical import Event

    farm_id, _, advice_id = _farm_with_assessment(committed_session_factory)
    app = create_app(settings, session_factory=committed_session_factory)
    params = {"farm": farm_id}
    with (
        TestClient(app, base_url="https://testserver") as owner,
        TestClient(app, base_url="https://testserver") as advisor,
    ):
        assert _login(owner, login="owner").status_code == 303
        assert _login(advisor, login="expert").status_code == 303
        blank_evidence = owner.post(
            "/review/versions",
            params=params,
            json={"overrides": {"season_gate": "2024-04-10"}, "evidence": "   "},
            headers=_registered(owner, farm_id),
        )
        assert blank_evidence.status_code == 422
        unsupported = owner.post(
            "/review/propose-form",
            params=params,
            data={"parameter": "not_a_parameter", "value": "1"},
            headers=_registered(owner, farm_id),
        )
        assert unsupported.status_code == 422
        long_note = owner.post(
            f"/review/assessments/{advice_id}/request-form",
            params=params,
            data={"note": "x" * 2001},
            headers=_registered(owner, farm_id),
            follow_redirects=False,
        )
        assert long_note.status_code == 303 and "error=" in long_note.headers["location"]
        asked = owner.post(
            f"/review/assessments/{advice_id}/request-form",
            params=params,
            data={"note": "Please check"},
            headers=_registered(owner, farm_id),
            follow_redirects=False,
        )
        assert asked.status_code == 303
        with committed_session_factory() as session:
            request_id = session.scalar(
                select(Event.id).where(Event.kind == "advice.review_requested")
            )
        duplicate = owner.post(
            f"/review/assessments/{advice_id}/request-form",
            params=params,
            data={"note": "Again"},
            headers=_registered(owner, farm_id),
            follow_redirects=False,
        )
        assert duplicate.status_code == 303 and "error=" in duplicate.headers["location"]
        with TestClient(
            app,
            base_url="https://testserver",
            headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
        ) as operator:
            operator_answer = operator.post(
                f"/review/requests/{request_id}/response",
                params=params,
                json={"opinion": "agree", "note": "Operator answer"},
            )
            assert operator_answer.status_code == 201
        missing = advisor.post(
            "/review/requests/999999/response",
            params=params,
            json={"opinion": "agree", "note": "No such request"},
            headers=_registered(advisor, farm_id),
        )
        assert missing.status_code == 404
        missing_form = advisor.post(
            "/review/requests/999999/response-form",
            params=params,
            data={"opinion": "agree", "note": "No such request"},
            headers=_registered(advisor, farm_id),
        )
        assert missing_form.status_code == 404

        advisor.cookies.set("farmcore_tour", "1")
        tour_answer = advisor.post(
            f"/review/requests/{request_id}/response",
            params=params,
            json={"opinion": "agree", "note": "Tour"},
            headers=_registered(advisor, farm_id),
        )
        assert tour_answer.status_code == 403
        advisor.cookies.delete("farmcore_tour")

        with TestClient(
            app,
            base_url="https://testserver",
            headers={"X-Admin-Token": settings.admin_token.get_secret_value()},
        ) as operator:
            operator_ask = operator.post(
                f"/review/assessments/{advice_id}/request",
                params=params,
                json={"note": "Operator check"},
            )
            assert operator_ask.status_code == 201

    with committed_session_factory.begin() as session:
        row = session.get(Advice, advice_id)
        session.delete(row)
        session.flush()
        set_replay(session, NOW + timedelta(hours=1))
    with TestClient(app, base_url="https://testserver") as owner:
        assert _login(owner, login="owner").status_code == 303
        listed = owner.get("/review/recommendations.json", params=params)
        assert listed.status_code == 200
        assert listed.json()["reviews"] == []


class _PausedSecondRequestRepo:
    """A real scoped repository that pauses the route's second request lookup."""

    def __init__(self, repo, *, ready, release):
        self._repo = repo
        self.ready = ready
        self.release = release
        self.calls = 0

    def __getattr__(self, name):
        return getattr(self._repo, name)

    def execute(self, stmt, *, scope, resource):
        self.calls += 1
        if self.calls == 2:
            self.ready.set()
            assert self.release.wait(timeout=10)
        return self._repo.execute(stmt, scope=scope, resource=resource)


def test_advice_events_deleted_between_reads_answer_404(committed_session_factory, settings):
    import threading
    from types import SimpleNamespace

    from fastapi import HTTPException
    from sqlalchemy import delete
    from starlette.requests import Request

    from farmcore.apps.review import recommendations, routes
    from farmcore.core.access import ScopedRepository, ensure_owner_grant
    from farmcore.core.clock import set_replay
    from farmcore.core.models.canonical import Advice, Event, Farm
    from tests.apps.share.conftest import make_parcel

    with committed_session_factory() as session:
        farm = Farm(name="Deleted review event farm", props={})
        session.add(farm)
        session.flush()
        ensure_owner_grant(session, farm)
        parcel = make_parcel(session, farm, 1)
        advice = Advice(
            farm_id=farm.id,
            parcel_id=parcel.id,
            evaluated_at=NOW,
            decision="wait",
            reasons=[],
            products=[],
            props={"headline": "Stored check", "assessment_status": "evaluable"},
        )
        session.add(advice)
        session.flush()
        set_replay(session, NOW, settings)
        repo = ScopedRepository(session, farm.id, ensure_owner_grant(session, farm), "t", "t")
        state = recommendations.request_check(
            repo, NOW, advice, parcel, note="Check this", requested_by="owner"
        )
        request_id, farm_id = state.request.id, farm.id
        session.commit()

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": f"/review/requests/{request_id}/response",
            "query_string": b"",
            "headers": [(b"x-admin-token", settings.admin_token.get_secret_value().encode())],
            "app": SimpleNamespace(state=SimpleNamespace(settings=settings)),
            "state": {},
        }
    )
    ready, release = threading.Event(), threading.Event()
    result = {}

    def answer():
        with committed_session_factory() as session:
            farm_row = session.get(Farm, farm_id)
            repo = _PausedSecondRequestRepo(
                ScopedRepository(
                    session,
                    farm_id,
                    ensure_owner_grant(session, farm_row),
                    "operator",
                    "web",
                ),
                ready=ready,
                release=release,
            )
            try:
                routes.respond_to_request(
                    request,
                    repo,
                    request_id,
                    routes.CheckAnswer(opinion="agree", note="Checked"),
                )
            except HTTPException as exc:
                result["status"] = exc.status_code
            else:
                result["ok"] = True

    thread = threading.Thread(target=answer)
    thread.start()
    assert ready.wait(timeout=10)
    with committed_session_factory.begin() as session:
        session.execute(delete(Event).where(Event.id == request_id))
    release.set()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert result.get("status") == 404

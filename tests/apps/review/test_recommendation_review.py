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

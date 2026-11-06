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


def _farm_with_assessment(factory, *, alias="P1", decision="wait", props=None):
    """A farm whose only saved assessment is at `NOW` (the replay clock)."""
    _, farm_id = _account(factory, login="owner")
    _account(factory, login="expert", role="advisor", farm_id=farm_id)
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
